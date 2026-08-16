"""The model backends — spec §6.

    none[:file]             no model at all: the payload is the answer
    exec:<command>          any CLI: stdin -> stdout
    claude-cli:<model|->    claude -p, with real usage accounting and a schema
    anthropic:<model>       POST /v1/messages
    gemini:<model>          POST /v1beta/models/{m}:generateContent
    openai:<model>          POST /v1/chat/completions  (also OpenRouter, vLLM, ...)

No vendor SDKs: every one of these is a single JSON POST in the non-streaming
case, over the `requests` dependency the project already has.

Two properties separate the native adapters from "just point it at an
OpenAI-compatible URL", and both are the reason this file is not shorter:

**Cache control.** The repo payload is a stable prefix reused across turns.
Anthropic places the breakpoint explicitly; Gemini needs the `cachedContents`
resource; the compat layer exposes neither. On both providers the reuse has to
be *asked for* — measured, Gemini's implicit caching gave 0% on "same repo,
new question" on one machine and 74.3% on six runs of eight on another, while
the explicit cache gave 99.9% on every run (findings §2.9). An optimisation
that arrives on most calls is harder to budget against than one that never
arrives.

**Cached-token accounting.** Without it a cache experiment cannot tell success
from failure, and the numbers are provider-specific in ways that invert the
answer: Anthropic's `input_tokens` *excludes* cache traffic (a 20k cached
prefix reports 19), while Gemini's `promptTokenCount` *includes* it. Summing
is right for one and double-counts for the other.

Everything here raises from `kopipasta.core.errors`, so a caller can tell a
retryable blip from a permanent misconfiguration without parsing prose.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from kopipasta.core.config import PROVIDER_KEY_ENV, BackendConfig
from kopipasta.core.errors import (
    AuthRejected,
    BackendError,
    BackendTimeout,
    KopipastaError,
    ModelRejected,
    RateLimited,
    ResponseTruncated,
    UsageError,
)


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    #: Tokens billed as a cache *write* on this call. For CLI backends this
    #: lumps the harness system prompt in with our payload and does not
    #: separate them — never report it as "our" input.
    cache_creation_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# HTTP, and turning a provider's refusal into the right kind of failure
# --------------------------------------------------------------------------
def _classify(
    provider: str, model: str, status: int, body: str, source: str
) -> Exception:
    """Map an HTTP failure onto the taxonomy — spec §9.

    The provider's own words are always carried through verbatim: paraphrasing
    an upstream error destroys the one detail that identifies it. What we add
    is the diagnosis, and above all whether retrying could possibly help.
    """
    key_env = PROVIDER_KEY_ENV.get(provider) or ""
    excerpt = body.strip()[:600]
    lowered = excerpt.lower()
    if status in (401, 403) and "cachedcontent" not in lowered:
        return AuthRejected(provider, key_env or "the provider credential", excerpt)
    if status == 429:
        return RateLimited(provider, None, excerpt)
    if status in (400, 404) and ("model" in lowered or "not found" in lowered):
        return ModelRejected(provider, model, excerpt, source)
    if status >= 500:
        return BackendError(
            f"{provider} returned HTTP {status}.",
            detail=f"Provider said: {excerpt}",
            hint="Server-side; retrying is reasonable.",
            provider=provider,
        )
    return BackendError(
        f"{provider} rejected the request (HTTP {status}).",
        detail=f"Provider said: {excerpt}",
        hint="Check the model name and the request size.",
        provider=provider,
    )


class _CacheGone(RuntimeError):
    """Internal: the cached prefix vanished between the check and the call."""


def _post(
    url: str,
    body: Dict[str, Any],
    *,
    headers: Dict[str, str],
    timeout: float,
    provider: str,
    model: str,
    source: str,
) -> Dict[str, Any]:
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.Timeout:
        raise BackendTimeout(provider, timeout) from None
    except requests.RequestException as exc:
        raise BackendError(
            f"could not reach {provider}.",
            detail=str(exc),
            hint="Network or DNS. Retrying is reasonable.",
            provider=provider,
        ) from exc
    if r.status_code >= 400:
        if "CachedContent not found" in r.text:
            raise _CacheGone(r.text[:200])
        raise _classify(provider, model, r.status_code, r.text, source)
    try:
        return dict(r.json())
    except ValueError as exc:
        raise BackendError(
            f"{provider} returned a non-JSON body.",
            detail=r.text[:300],
            provider=provider,
        ) from exc


# --------------------------------------------------------------------------
# none: — the pipeline with the model removed
# --------------------------------------------------------------------------
class NoneBackend:
    """Assembles everything, calls nothing, hands the payload back.

    This is what makes the rest of the tool testable without a key, a network
    or a bill: selection, budget, rendering, the session record and the whole
    output contract run exactly as they do for real, and the "response" is the
    prompt that would have been sent. `none:<file>` answers with a canned
    response instead, which is how the parsing and apply paths get exercised.
    """

    def __init__(self, target: str = "", **_: Any) -> None:
        self.provider = "none"
        self.model = "none"
        self.canned = target.strip()
        # A canned answer is an answer: the run should parse it, cite from it
        # and report it like any other. Only the echo case is a dry run, and
        # calling both by the same name would hide a real result behind a flag
        # that says nothing happened.
        self.dry_run = not self.canned

    def complete(self, prefix: str, suffix: str, **_: Any) -> Completion:
        if self.canned:
            try:
                with open(self.canned, "r", encoding="utf-8") as fh:
                    return Completion(text=fh.read(), model="none")
            except OSError as exc:
                raise UsageError(
                    f"could not read the canned response {self.canned!r}.",
                    detail=str(exc),
                    hint="none:<file> answers with the contents of that file.",
                ) from exc
        return Completion(text=f"{prefix}\n{suffix}", model="none")

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# exec: — an agent CLI, driven as a completion (tools MUST be off; spec §6)
# --------------------------------------------------------------------------
class ExecBackend:
    def __init__(self, command: str, *, timeout: float = 900, **_: Any) -> None:
        self.provider = "exec"
        self.command = command
        self.model = command
        self.timeout = timeout

    def complete(self, prefix: str, suffix: str, **_: Any) -> Completion:
        try:
            p = subprocess.run(
                self.command,
                shell=True,
                input=f"{prefix}\n\n{suffix}",
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendTimeout("exec", self.timeout) from None
        except OSError as exc:
            raise BackendError(
                f"could not run the backend command {self.command!r}.",
                detail=str(exc),
                hint="Check the command exists and is executable.",
                provider="exec",
            ) from exc
        if p.returncode != 0:
            raise BackendError(
                f"the backend command exited {p.returncode}.",
                detail=(p.stderr or "no stderr").strip()[-800:],
                hint="Run the command by hand with the same input to see what it wants.",
                provider="exec",
            )
        # No usage accounting is available. That is the price of this backend,
        # and it is stated rather than faked with an estimate.
        return Completion(text=p.stdout, model=self.command)

    def close(self) -> None:
        return None


class ClaudeCliBackend:
    """`claude -p`, using what the CLI actually exposes.

    `--output-format json` returns real usage and cost and `--json-schema`
    enforces structured output, so the two things plain `exec:` gives up are
    not inherent to CLI-backed oracles. What it cannot give you is control of
    the cache breakpoint or clean attribution: the CLI reports our payload and
    its own ~34k-token system prompt together under cache creation.
    """

    TOOLS_OFF = (
        "Edit,Write,Read,Bash,Glob,Grep,Task,WebFetch,WebSearch,NotebookEdit,TodoWrite"
    )

    def __init__(
        self, model: str = "", *, timeout: float = 900, binary: str = "claude", **_: Any
    ):
        self.provider = "claude-cli"
        self.model = "" if model in ("", "-") else model
        self.timeout = timeout
        self.binary = binary

    def complete(
        self, prefix: str, suffix: str, *, schema: Optional[dict] = None, **_: Any
    ) -> Completion:
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--disallowedTools",
            self.TOOLS_OFF,
        ]
        if self.model:
            cmd += ["--model", self.model]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        try:
            p = subprocess.run(
                cmd,
                input=f"{prefix}\n\n{suffix}",
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendTimeout(self.provider, self.timeout) from None
        except OSError as exc:
            raise BackendError(
                f"could not run {self.binary!r}.",
                detail=str(exc),
                hint="Install the Claude CLI, or use a different backend.",
                provider=self.provider,
            ) from exc
        if p.returncode != 0:
            raise BackendError(
                f"{self.binary} exited {p.returncode}.",
                detail=(p.stderr or "no stderr").strip()[-800:],
                provider=self.provider,
            )
        try:
            d = json.loads(p.stdout)
        except ValueError as exc:
            raise BackendError(
                f"{self.binary} did not honour --output-format json.",
                detail=p.stdout[:300],
                provider=self.provider,
            ) from exc
        if d.get("is_error"):
            raise BackendError(
                f"{self.binary} reported an error.",
                detail=str(d.get("result"))[:600],
                provider=self.provider,
            )

        if schema and d.get("structured_output") is not None:
            text = json.dumps(d["structured_output"])
        else:
            text = str(d.get("result", ""))

        u = d.get("usage") or {}
        models = list((d.get("modelUsage") or {}).keys())
        return Completion(
            text=text,
            # Same summation rule as Anthropic — see the note there.
            input_tokens=(
                u.get("input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
            ),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
            cost_usd=d.get("total_cost_usd", 0.0) or 0.0,
            model=",".join(models) or self.model or "claude",
            raw=d,
        )

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# anthropic: — cache_control marks the end of the stable repo prefix
# --------------------------------------------------------------------------
class AnthropicBackend:
    def __init__(
        self,
        model: str,
        *,
        base_url: Optional[str] = None,
        timeout: float = 900,
        source: str = "",
        **_: Any,
    ) -> None:
        self.provider = "anthropic"
        self.model = model
        self.base = (
            base_url
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com"
        ).rstrip("/")
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout
        self.source = source

    def complete(
        self,
        prefix: str,
        suffix: str,
        *,
        schema: Optional[dict] = None,
        max_tokens: int = 8192,
        **_: Any,
    ) -> Completion:
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        # Everything up to here is the repo payload, cached and
                        # reused verbatim on the next turn of this session.
                        {
                            "type": "text",
                            "text": prefix,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": suffix},
                    ],
                }
            ],
        }
        if schema:
            # Anthropic has no response_format; a single forced tool is the
            # equivalent and is enforced server-side the same way.
            body["tools"] = [
                {
                    "name": "emit",
                    "description": "Return the result.",
                    "input_schema": schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "emit"}

        d = _post(
            f"{self.base}/v1/messages",
            body,
            timeout=self.timeout,
            provider=self.provider,
            model=self.model,
            source=self.source,
            headers={
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

        if schema:
            text = ""
            for blk in d.get("content", []):
                if blk.get("type") == "tool_use":
                    text = json.dumps(blk.get("input", {}))
                    break
        else:
            text = "".join(
                b.get("text", "")
                for b in d.get("content", [])
                if b.get("type") == "text"
            )

        stop = d.get("stop_reason")
        if stop == "max_tokens":
            raise ResponseTruncated(self.provider, stop, max_tokens, len(text))

        u = d.get("usage", {})
        return Completion(
            text=text,
            # `input_tokens` counts only tokens neither read from nor written
            # to the cache, so a 20k cached prefix reports 19. Summing all
            # three is the only way to answer "how big was my input".
            input_tokens=(
                u.get("input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
            ),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
            model=d.get("model", self.model),
            raw=d,
        )

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# gemini: — native, because the cache here is a resource with a lifetime
# --------------------------------------------------------------------------
#: Caches alive right now, so an unexpected exit can still hand back the
#: rental. atexit is the last line of defence, not the intended path.
_LIVE_GEMINI_CACHES: "set[Tuple[str, str, str]]" = set()

#: Every cache this tool creates is named `kopipasta-<label>-<digest>`, so an
#: orphan is identifiable at a glance in the provider's console — and so a
#: sweep can tell one project's caches from another's.
CACHE_PREFIX = "kopipasta-"


def _cache_label(label: str) -> str:
    """The project part of a display name, made safe to put in one."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(label or ""))[:48]


def _delete_gemini_cache(base: str, key: str, name: str) -> None:
    try:
        requests.delete(f"{base}/{name}", timeout=30, headers={"x-goog-api-key": key})
    except requests.RequestException:
        pass  # Best effort; the TTL is the backstop that makes this safe to swallow.
    _LIVE_GEMINI_CACHES.discard((base, key, name))


@atexit.register
def _sweep_gemini_caches() -> None:
    for base, key, name in list(_LIVE_GEMINI_CACHES):
        _delete_gemini_cache(base, key, name)


class GeminiBackend:
    """Explicit `cachedContents`, owned for its whole lifetime.

    The cache is RENTED: Google bills storage per token-hour for the full TTL
    whether or not turn 2 ever arrives, so a leaked cache is a meter running
    with nobody watching — the failure mode is a slow bill, which is the kind
    nobody notices. Hence: an always-explicit clamped TTL, `close()` to hand
    it back, deletion of a superseded prefix, an atexit sweep, a `kopipasta-`
    display name so an orphan is identifiable, and `reap_orphans()`.

    A short TTL trades a cost risk for an availability risk rather than
    removing risk: an expired name produces `403 CachedContent not found`, so
    expiry is handled by retrying without the cache — keyed on the message,
    not the status, which is shared with genuine auth failures.
    """

    BASE = "https://generativelanguage.googleapis.com/v1beta"
    DEFAULT_TTL_S = 300
    MAX_TTL_S = 3600

    def __init__(
        self,
        model: str,
        *,
        base_url: Optional[str] = None,
        timeout: float = 900,
        cache: bool = False,
        cache_ttl_s: int = DEFAULT_TTL_S,
        source: str = "",
        label: str = "",
        **_: Any,
    ) -> None:
        self.provider = "gemini"
        self.model = model
        # Which project rented it. One API key serves every repo on the
        # machine, and the cache list is per key, so without this a sweep run
        # in repo A cannot tell repo B's live lease from its own orphan — and
        # deletes it. See `reap_orphans`.
        self.label = _cache_label(label)
        self.base = (base_url or os.environ.get("GEMINI_BASE_URL") or self.BASE).rstrip(
            "/"
        )
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY", ""
        )
        self.timeout = timeout
        self.source = source
        # Caching is off unless asked for: a one-shot question that will never
        # have a turn 2 would pay rent for nothing.
        self.cache_enabled = cache
        self.cache_ttl_s = max(1, min(int(cache_ttl_s), self.MAX_TTL_S))
        self._cache_name: Optional[str] = None
        self._cache_key: Optional[str] = None
        self._cache_tokens = 0
        self._cache_deadline = 0.0
        self._created_on_last_call = False
        self._adopted = False
        #: True once an owner outside this process is responsible for the
        #: cache, at which point close() must NOT delete it.
        self.handed_over = False
        self.cache_disabled_reason = ""

    @property
    def headers(self) -> Dict[str, str]:
        # Header, not ?key= — query params leak into proxy and access logs.
        return {"x-goog-api-key": self.key, "content-type": "application/json"}

    @property
    def _expiry_margin_s(self) -> float:
        # Stop trusting a cache slightly before it expires: the request still
        # has to travel. Proportional, so a tiny TTL is not entirely margin.
        return min(10.0, self.cache_ttl_s * 0.2)

    @staticmethod
    def digest(prefix: str) -> str:
        return hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]

    def display_name(self, digest: str) -> str:
        label = f"{self.label}-" if self.label else ""
        return f"{CACHE_PREFIX}{label}{digest[:16]}"

    def adopt(
        self, *, name: str, digest: str, expires_in_s: float, tokens: int = 0
    ) -> None:
        """Take over a cache created by an earlier process — spec §7.

        This is what makes turn 2 of a session cheap when turn 2 is a separate
        invocation. The handle is a hint: if the provider disagrees, the 403
        path in `complete` rebuilds without it.
        """
        self._cache_name = name
        self._cache_key = digest
        self._cache_tokens = tokens
        self._cache_deadline = time.monotonic() + max(0.0, expires_in_s)
        self._adopted = True
        self.cache_enabled = True

    def handle(self) -> Optional[Dict[str, Any]]:
        """What to persist so the next turn can adopt it. None if there is none."""
        if not self._cache_name or not self._cache_key:
            return None
        return {
            "name": self._cache_name,
            "digest": self._cache_key,
            "tokens": self._cache_tokens,
            "ttl_s": self.cache_ttl_s,
            # Whether this process created the cache or inherited it. The
            # difference decides who owns its expiry.
            "adopted": self._adopted,
        }

    def hand_over(self) -> None:
        """Transfer ownership to the session record; stop the atexit sweep."""
        if self._cache_name:
            _LIVE_GEMINI_CACHES.discard((self.base, self.key, self._cache_name))
        self.handed_over = True

    def _forget_cache(self) -> None:
        """Drop the handle WITHOUT deleting. For a cache already gone."""
        if self._cache_name:
            _LIVE_GEMINI_CACHES.discard((self.base, self.key, self._cache_name))
        self._cache_name = self._cache_key = None
        self._cache_tokens = 0
        self._cache_deadline = 0.0

    def _ensure_cache(self, prefix: str) -> Optional[str]:
        """A cachedContents name holding `prefix`, creating one if needed.

        Returns None when caching is off, unavailable, or the payload is under
        the provider's minimum — all fallbacks, not errors: the call still
        works, it just pays full price.
        """
        if not self.cache_enabled:
            return None
        digest = self.digest(prefix)
        self._created_on_last_call = False
        still_valid = self._cache_name is not None and (
            time.monotonic() < self._cache_deadline - self._expiry_margin_s
        )
        if still_valid and self._cache_key == digest:
            return self._cache_name
        if self._cache_name:
            if still_valid and not self._adopted:
                self.close()  # prefix changed: hand back the dead weight now
            else:
                self._forget_cache()

        body = {
            "model": f"models/{self.model}",
            "contents": [{"role": "user", "parts": [{"text": prefix}]}],
            "ttl": f"{self.cache_ttl_s}s",  # ALWAYS set. This is the leak stop.
            "displayName": self.display_name(digest),
        }
        try:
            d = _post(
                f"{self.base}/cachedContents",
                body,
                timeout=self.timeout,
                provider=self.provider,
                model=self.model,
                source=self.source,
                headers=self.headers,
            )
        except (BackendError, _CacheGone) as exc:
            # Dominant cause: payload below the model's minimum cacheable size
            # (4,096 tokens on Gemini 3.x). Detecting that by trying costs one
            # round trip and needs no per-model table that would rot.
            self.cache_enabled = False
            self.cache_disabled_reason = str(exc)[:200]
            return None

        self._cache_name = d.get("name")
        self._cache_key = digest
        self._cache_tokens = (d.get("usageMetadata") or {}).get("totalTokenCount", 0)
        self._cache_deadline = time.monotonic() + self.cache_ttl_s
        self._created_on_last_call = True
        self._adopted = False
        if self._cache_name:
            _LIVE_GEMINI_CACHES.add((self.base, self.key, self._cache_name))
        return self._cache_name

    def close(self) -> None:
        """Hand back the rental. Idempotent, and a no-op after hand_over()."""
        if self._cache_name and not self.handed_over:
            _delete_gemini_cache(self.base, self.key, self._cache_name)
        self._forget_cache()

    @classmethod
    def list_caches(cls, base_url: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every cachedContents resource this key can see. Ours are prefixed."""
        base, key = cls._endpoint(base_url)
        try:
            r = requests.get(
                f"{base}/cachedContents", timeout=60, headers={"x-goog-api-key": key}
            )
            r.raise_for_status()
            return list(r.json().get("cachedContents") or [])
        except (requests.RequestException, ValueError):
            return []

    @classmethod
    def reap_orphans(
        cls,
        base_url: Optional[str] = None,
        *,
        keep: Iterable[str] = (),
        label: Optional[str] = None,
    ) -> int:
        """Hand back every cache this tool left rented and nobody is using.

        Two filters, and both exist because of something that actually
        happened. `keep` is the set of resource names a live session is
        renting: a named session deliberately leaves its cache alive so turn 2
        can reuse it, so an unattended sweep deleted live leases and each
        following turn silently paid full price. `label` scopes the sweep to
        one project: the cache list is per API key, not per repo, so without it
        a sweep run in repo A cannot distinguish repo B's live lease from its
        own orphan.

        `label=None` means every project, which is what you want after a crash
        and nowhere else.
        """
        base, key = cls._endpoint(base_url)
        protected = {str(k) for k in keep}
        wanted = f"{CACHE_PREFIX}{_cache_label(label)}-" if label else CACHE_PREFIX
        n = 0
        for it in cls.list_caches(base_url):
            name = str(it.get("name", ""))
            if not str(it.get("displayName", "")).startswith(wanted):
                continue
            if name in protected:
                continue
            _delete_gemini_cache(base, key, name)
            n += 1
        return n

    @classmethod
    def _endpoint(cls, base_url: Optional[str] = None) -> Tuple[str, str]:
        base = (base_url or os.environ.get("GEMINI_BASE_URL") or cls.BASE).rstrip("/")
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        return base, key

    def count_tokens(self, text: str) -> Optional[int]:
        """The provider's own count — spec §5. Free, and the only exact answer.

        A heuristic calibrated on this repo's payloads is within ~8%, which is
        fine for deciding what to demote and not fine for deciding to refuse a
        run outright. Returns None when the endpoint will not answer: an exact
        number is an improvement on the estimate, never a precondition for
        running, and anything genuinely wrong with the credentials or the model
        will fail again immediately with a proper error.
        """
        try:
            d = _post(
                f"{self.base}/models/{self.model}:countTokens",
                {"contents": [{"role": "user", "parts": [{"text": text}]}]},
                timeout=min(self.timeout, 120.0),
                provider=self.provider,
                model=self.model,
                source=self.source,
                headers=self.headers,
            )
        except (KopipastaError, _CacheGone):
            return None
        return int(d.get("totalTokens") or 0) or None

    def complete(
        self,
        prefix: str,
        suffix: str,
        *,
        schema: Optional[dict] = None,
        max_tokens: int = 8192,
        _retry: bool = True,
        **_: Any,
    ) -> Completion:
        gen: Dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_tokens}
        if schema:
            # Enforced by the API. This is what upgrades a mode from "please
            # return JSON" to a guarantee.
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = schema

        name = self._ensure_cache(prefix)
        # Tokens billed as a cache write on this call — nonzero only on a turn
        # that actually created an entry. Asked of _ensure_cache rather than
        # inferred, because inference gets the TTL re-create wrong and a turn
        # that silently paid for a second write would look free.
        created = self._cache_tokens if self._created_on_last_call else 0

        if name:
            # The prefix lives in the cache resource now; sending it again
            # would defeat the entire point.
            body: Dict[str, Any] = {
                "contents": [{"role": "user", "parts": [{"text": suffix}]}],
                "cachedContent": name,
                "generationConfig": gen,
            }
        else:
            body = {
                "contents": [
                    {"role": "user", "parts": [{"text": prefix}, {"text": suffix}]}
                ],
                "generationConfig": gen,
            }

        try:
            d = _post(
                f"{self.base}/models/{self.model}:generateContent",
                body,
                timeout=self.timeout,
                provider=self.provider,
                model=self.model,
                source=self.source,
                headers=self.headers,
            )
        except _CacheGone:
            # The cache expired between our deadline check and the request —
            # clock skew, a paused process, a server-side eviction. This is the
            # branch that makes correctness independent of the local clock.
            if not (name and _retry):
                raise BackendError(
                    "the cached prefix disappeared and could not be rebuilt.",
                    hint="Retry; the cache will be recreated.",
                    provider=self.provider,
                ) from None
            self._forget_cache()
            return self.complete(
                prefix, suffix, schema=schema, max_tokens=max_tokens, _retry=False
            )

        cands = d.get("candidates") or []
        text = ""
        if cands:
            text = "".join(
                p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])
            )

        u = d.get("usageMetadata", {})
        reason = cands[0].get("finishReason") if cands else None
        if reason not in (None, "STOP"):
            # Checked regardless of whether text came back. Guarding only the
            # empty case lets a TRUNCATED answer through as success, and under
            # `responseSchema` that is JSON stopping mid-string, which reads
            # downstream as "no answer" rather than "cut off".
            if reason == "MAX_TOKENS":
                raise ResponseTruncated(self.provider, reason, max_tokens, len(text))
            raise BackendError(
                f"gemini stopped early: {reason}.",
                detail=f"Returned {len(text):,} characters.",
                hint="Rephrasing usually helps; a safety stop will not change on retry.",
                provider=self.provider,
                finish_reason=reason,
            )

        return Completion(
            text=text,
            # cachedContentTokenCount is *included* in promptTokenCount, so
            # unlike Anthropic these must NOT be summed.
            input_tokens=u.get("promptTokenCount", 0),
            # Reasoning tokens are billed as output and spend the same budget,
            # so reporting only candidatesTokenCount understates the turn.
            output_tokens=(
                u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
            ),
            cached_tokens=u.get("cachedContentTokenCount", 0),
            cache_creation_tokens=created,
            model=self.model,
            raw=d,
        )


def release_lease(record: Dict[str, Any], *, base_url: Optional[str] = None) -> bool:
    """Hand back the cache a session's `cache.json` is renting. Spec §6.

    Called when a session is deleted. Without it the only record of the
    resource name goes with the directory, and the meter keeps running until
    the TTL expires with nothing on disk to say what is being paid for.

    Returns True when we asked the provider to release something. Providers
    whose caches cost nothing to abandon return False: there is no rental.
    """
    if not isinstance(record, dict) or record.get("provider") != "gemini":
        return False
    name = str(record.get("name") or "")
    if not name:
        return False
    base, key = GeminiBackend._endpoint(base_url)
    _delete_gemini_cache(base, key, name)
    return True


# --------------------------------------------------------------------------
# openai: — the widest-reach shape. Gemini also speaks it (see GEMINI_COMPAT).
# --------------------------------------------------------------------------
GEMINI_COMPAT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"  # trailing / matters
)


class OpenAICompatBackend:
    def __init__(
        self,
        model: str,
        *,
        base_url: Optional[str] = None,
        timeout: float = 900,
        provider: str = "openai",
        source: str = "",
        **_: Any,
    ) -> None:
        self.provider = provider
        self.model = model
        default = (
            GEMINI_COMPAT
            if provider == "gemini-compat"
            else "https://api.openai.com/v1"
        )
        self.base = (base_url or os.environ.get("OPENAI_BASE_URL") or default).rstrip(
            "/"
        )
        self.key = os.environ.get("OPENAI_API_KEY", "")
        if provider == "gemini-compat":
            self.key = os.environ.get("GEMINI_API_KEY") or self.key
        self.timeout = timeout
        self.source = source

    def complete(
        self,
        prefix: str,
        suffix: str,
        *,
        schema: Optional[dict] = None,
        max_tokens: int = 8192,
        **_: Any,
    ) -> Completion:
        body: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "user", "content": f"{prefix}\n\n{suffix}"}],
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "oracle_result",
                    "strict": True,
                    "schema": schema,
                },
            }

        d = _post(
            f"{self.base}/chat/completions",
            body,
            timeout=self.timeout,
            provider=self.provider,
            model=self.model,
            source=self.source,
            headers={
                "authorization": f"Bearer {self.key}",
                "content-type": "application/json",
            },
        )

        choices = d.get("choices") or []
        text = (choices[0].get("message", {}).get("content") if choices else "") or ""
        finish = choices[0].get("finish_reason") if choices else None
        if finish == "length":
            raise ResponseTruncated(self.provider, finish, max_tokens, len(text))
        u = d.get("usage", {})
        return Completion(
            text=text,
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get(
                "cached_tokens", 0
            ),
            model=d.get("model", self.model),
            raw=d,
        )

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
def build(
    cfg: BackendConfig,
    *,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    cache: bool = False,
    cache_ttl_s: Optional[int] = None,
    label: str = "",
):
    """Config -> a live backend. The only place a provider name becomes code."""
    kwargs: Dict[str, Any] = {
        "base_url": base_url,
        "timeout": float(timeout if timeout is not None else cfg.timeout_s),
        "source": cfg.sources.get("provider", "unknown"),
    }
    provider, model = cfg.provider, cfg.model

    if provider == "none":
        return NoneBackend(model)
    if provider == "exec":
        if not model:
            raise UsageError(
                "the exec backend needs a command.",
                hint="--backend 'exec:claude -p --disallowedTools Edit,Write,Bash'",
            )
        return ExecBackend(model, timeout=kwargs["timeout"])
    if provider == "claude-cli":
        return ClaudeCliBackend(model, timeout=kwargs["timeout"])
    if not model:
        raise UsageError(
            f"the {provider} backend needs a model.",
            detail=f"Resolved from {cfg.sources.get('provider', 'unknown')}.",
            hint=f'--backend {provider}:<model>, or set model = "..." in the config file.',
        )
    if provider == "anthropic":
        return AnthropicBackend(model, **kwargs)
    if provider == "gemini":
        return GeminiBackend(
            model,
            cache=cache,
            cache_ttl_s=cfg.cache_ttl_s if cache_ttl_s is None else cache_ttl_s,
            label=label,
            **kwargs,
        )
    if provider in ("openai", "openai-compat", "gemini-compat"):
        return OpenAICompatBackend(model, provider=provider, **kwargs)
    raise UsageError(  # pragma: no cover - resolve_backend rejects these first
        f"no adapter for provider {provider!r}.",
        hint="kopipasta config --show",
    )
