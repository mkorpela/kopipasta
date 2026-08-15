#!/usr/bin/env python3
"""Raw-model backends for the context oracle — the layer *below* agent CLIs.

Zero new dependencies: `requests` is already a kopipasta dependency, and every
one of these APIs is a single JSON POST for the non-streaming case.

    exec:<command>                 any CLI: stdin -> stdout (an agent, tools off)
    claude-cli:<model|->           claude -p, with real usage + enforced schema
    anthropic:<model>              native /v1/messages    — explicit cache breakpoints
    gemini:<model>                 native :generateContent — responseSchema, 1M+ ctx
    openai:<model>                 /v1/chat/completions   — OpenAI, OpenRouter, vLLM,
                                                            Groq, LM Studio, and Gemini
                                                            via its compat endpoint

Two things the oracle needs that generic "just use an OpenAI-compatible URL"
advice loses, and which is why the two native adapters exist:

  1. CACHE CONTROL. The repo payload is a stable prefix reused across turns.
     Anthropic lets you place the breakpoint explicitly (cache_control);
     Gemini needs the cachedContents resource; the compat layer exposes
     neither. On BOTH providers the reuse has to be asked for: measured,
     Gemini's implicit caching gave 0% on "same repo, different question"
     (findings §2.9) and explicit cachedContents gave 99.9%. Note the
     asymmetry in what that costs — Anthropic's ephemeral breakpoint is a
     flag on a request, Gemini's cache is a RENTED resource billed per
     token-hour until its TTL expires. GeminiBackend therefore owns a
     lifecycle: always an explicit short TTL, close() to hand it back, and
     an atexit sweep so a crash cannot leave the meter running.
  2. SERVER-ENFORCED SCHEMA. Triage mode wants guaranteed JSON, not begging.
     Gemini responseSchema and OpenAI json_schema are enforced by the API.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0
    # Tokens written into the cache on this call. For CLI backends this lumps
    # the harness system prompt together with our payload and does NOT
    # separate them — do not report it as "our" input. Measure the harness
    # floor by running an empty prompt and subtracting.
    cache_creation_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


class BackendError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# exec: — an agent CLI, driven as a completion (tools MUST be off; see spec §6)
# ---------------------------------------------------------------------------
class ExecBackend:
    def __init__(self, command: str, timeout: int = 900):
        self.command, self.timeout = command, timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        try:
            p = subprocess.run(
                self.command, shell=True, input=prefix + "\n\n" + suffix,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(f"timed out after {self.timeout}s")
        if p.returncode != 0:
            raise BackendError((p.stderr or "non-zero exit")[-800:])
        # No usage accounting available — that is the price of this backend.
        return Completion(text=p.stdout, model=self.command)


# ---------------------------------------------------------------------------
# claude-cli: — exec:, but using what the CLI actually exposes.
#
# `claude -p --output-format json` returns real usage and cost, and
# `--json-schema` enforces structured output server-side. So the two things
# plain exec: gives up are NOT inherent to CLI-backed oracles — they were
# just flags we had not read. Still zero key custody.
#
# What it does NOT give you: control over the cache breakpoint, freedom from
# the harness's own ~34k-token system prompt, or clean attribution — the CLI
# reports our payload and its own prompt together under cache_creation, with
# input_tokens counting only the uncached delta.
# ---------------------------------------------------------------------------
class ClaudeCliBackend:
    TOOLS_OFF = "Edit,Write,Read,Bash,Glob,Grep,Task,WebFetch,WebSearch,NotebookEdit,TodoWrite"

    def __init__(self, model: str = "", timeout: int = 900, binary: str = "claude"):
        self.model, self.timeout, self.binary = model, timeout, binary

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        cmd = [self.binary, "-p", "--output-format", "json",
               "--disallowedTools", self.TOOLS_OFF]
        if self.model:
            cmd += ["--model", self.model]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        try:
            p = subprocess.run(cmd, input=prefix + "\n\n" + suffix,
                               capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise BackendError(f"timed out after {self.timeout}s")
        if p.returncode != 0:
            raise BackendError((p.stderr or "non-zero exit")[-800:])
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise BackendError(f"expected --output-format json, got: {p.stdout[:300]}")
        if d.get("is_error"):
            raise BackendError(str(d.get("result"))[:600])

        if schema:
            so = d.get("structured_output")
            text = json.dumps(so) if so is not None else str(d.get("result", ""))
        else:
            text = str(d.get("result", ""))

        u = d.get("usage") or {}
        models = list((d.get("modelUsage") or {}).keys())
        return Completion(
            text=text,
            # Same summation rule as AnthropicBackend — see the note there.
            input_tokens=(u.get("input_tokens", 0)
                          + u.get("cache_read_input_tokens", 0)
                          + u.get("cache_creation_input_tokens", 0)),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
            cost_usd=d.get("total_cost_usd", 0.0) or 0.0,
            model=",".join(models) or self.model,
            raw=d,
        )


# ---------------------------------------------------------------------------
# anthropic: — cache_control marks the end of the stable repo prefix
# ---------------------------------------------------------------------------
class AnthropicBackend:
    def __init__(self, model: str, base_url: Optional[str] = None, timeout: int = 900):
        self.model = model
        self.base = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        content = [
            # The repo payload. Everything up to here is cached and reused
            # verbatim on the next turn of this session.
            {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": suffix},
        ]
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if schema:
            # Anthropic has no response_format; a single forced tool is the
            # equivalent, and is enforced server-side the same way.
            body["tools"] = [{"name": "emit", "description": "Return the result.",
                              "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "emit"}

        d = _post(f"{self.base}/v1/messages", body, timeout=self.timeout, headers={
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })

        if schema:
            text = ""
            for blk in d.get("content", []):
                if blk.get("type") == "tool_use":
                    text = json.dumps(blk.get("input", {}))
                    break
        else:
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")

        u = d.get("usage", {})
        # Anthropic's `input_tokens` counts ONLY tokens that were neither read
        # from nor written to the cache. A 20k-token cached prefix shows up as
        # input_tokens=19. Summing all three is the only way to get "how big
        # was my input" — reporting input_tokens alone makes a cached call look
        # like it sent nothing.
        return Completion(
            text=text,
            input_tokens=(u.get("input_tokens", 0)
                          + u.get("cache_read_input_tokens", 0)
                          + u.get("cache_creation_input_tokens", 0)),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
            model=d.get("model", self.model),
            raw=d,
        )


# ---------------------------------------------------------------------------
# gemini: — native. The 1M+ context is the reason this adapter exists.
#
# MEASURED (findings §2.9): implicit caching does not serve this access
# pattern. A stable prefix with a *varying question* missed 7/7; only an exact
# repeat of a whole earlier request hit (5/5, always the same block-aligned
# 12,263 tokens). An exact repeat is worthless to an oracle — you already have
# that answer. So the prefix economics need the explicit `cachedContents`
# resource, which measured 99.9% reuse across three different suffixes with no
# warm-up turn.
#
# That resource is RENTED, not free: Google bills storage per token-hour for
# the whole TTL, whether or not turn 2 ever arrives. A leaked cache is a meter
# running with nobody watching, so every cache this adapter creates carries an
# explicit, short, clamped TTL, is deleted in `close()`, and is registered for
# an atexit sweep in case we die before that. Never rely on the server default.
# ---------------------------------------------------------------------------

# Caches alive right now, so an unexpected exit can still hand back the rental.
# atexit is the last line of defence, not the intended path — close() is.
_LIVE_GEMINI_CACHES: "set[tuple[str, str, str]]" = set()  # (base, key, name)


def _delete_gemini_cache(base: str, key: str, name: str) -> None:
    try:
        requests.delete(f"{base}/{name}", timeout=30,
                        headers={"x-goog-api-key": key})
    except requests.RequestException:
        pass  # Best effort; the TTL is the backstop that makes this safe to swallow.
    _LIVE_GEMINI_CACHES.discard((base, key, name))


@atexit.register
def _sweep_gemini_caches() -> None:
    for base, key, name in list(_LIVE_GEMINI_CACHES):
        _delete_gemini_cache(base, key, name)


class GeminiBackend:
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    # Short by default and hard-capped. The cache exists to make the *next few
    # turns* of one session cheap; it is not storage. If a session outlives the
    # TTL the next turn re-creates it — paying twice for compute beats paying
    # rent on a cache nobody came back for.
    #
    # That re-creation is NOT automatic, and assuming it was is a bug this
    # adapter shipped with: an expired name kept being sent, and the provider
    # answers `403 CachedContent not found`. Verified against the live API with
    # a 15s TTL. A short TTL bounds the cost leak and creates a correctness
    # cliff; both have to be handled, so expiry is tracked below.
    DEFAULT_TTL_S = 300
    MAX_TTL_S = 3600

    def __init__(self, model: str, base_url: Optional[str] = None, timeout: int = 900,
                 cache: bool = True, cache_ttl_s: int = DEFAULT_TTL_S):
        self.model = model
        self.base = (base_url or os.environ.get("GEMINI_BASE_URL") or self.BASE).rstrip("/")
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        self.timeout = timeout
        self.cache_enabled = cache
        # Clamped at both ends: 0/negative would mean "server decides" (unbounded
        # rent), and an hour is already far longer than any oracle session.
        self.cache_ttl_s = max(1, min(int(cache_ttl_s), self.MAX_TTL_S))
        self._cache_name: Optional[str] = None
        self._cache_key: Optional[str] = None      # hash of the prefix it holds
        self._cache_tokens = 0
        self._cache_deadline = 0.0                 # monotonic; 0 == nothing held
        self._created_on_last_call = False
        self.cache_disabled_reason = ""

    # Stop trusting a cache slightly before it expires: the request still has
    # to travel. Proportional so that a deliberately tiny TTL does not end up
    # entirely inside the margin.
    @property
    def _expiry_margin_s(self) -> float:
        return min(10.0, self.cache_ttl_s * 0.2)

    def _forget_cache(self) -> None:
        """Drop the handle WITHOUT deleting. For a cache already gone."""
        if self._cache_name:
            _LIVE_GEMINI_CACHES.discard((self.base, self.key, self._cache_name))
        self._cache_name = self._cache_key = None
        self._cache_tokens = 0
        self._cache_deadline = 0.0

    # -- explicit cache lifecycle -------------------------------------------
    @property
    def headers(self) -> Dict[str, str]:
        # Header, not ?key= — query params leak into proxy and access logs.
        return {"x-goog-api-key": self.key, "content-type": "application/json"}

    def _ensure_cache(self, prefix: str) -> Optional[str]:
        """Return a cachedContents name holding `prefix`, creating it if needed.

        Returns None when caching is off, unavailable, or the payload is below
        the provider's minimum — all of which are *fallbacks, not errors*: the
        call still works, it just pays full price.
        """
        if not self.cache_enabled:
            return None
        digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]

        self._created_on_last_call = False
        still_valid = (self._cache_name is not None
                       and time.monotonic() < self._cache_deadline - self._expiry_margin_s)
        if still_valid and self._cache_key == digest:
            return self._cache_name
        if self._cache_name:
            if still_valid:
                # Prefix changed — the old rental is dead weight, hand it back
                # now rather than letting it run out its TTL unused.
                self.close()
            else:
                # Already expired server-side. There is nothing to hand back,
                # and DELETEing it would just be a wasted 403.
                self._forget_cache()

        body = {
            "model": f"models/{self.model}",
            "contents": [{"role": "user", "parts": [{"text": prefix}]}],
            # ALWAYS set, never defaulted. This is the leak stop.
            "ttl": f"{self.cache_ttl_s}s",
            # Makes an orphan identifiable as ours in cachedContents.list.
            "displayName": f"kopipasta-{digest[:16]}",
        }
        try:
            d = _post(f"{self.base}/cachedContents", body,
                      timeout=self.timeout, headers=self.headers)
        except BackendError as e:
            # The dominant cause is "payload below the model's minimum
            # cacheable size" (4,096 tokens on Gemini 3.x, 2,048 on 2.5).
            # Detecting that by trying costs one round trip and needs no
            # per-model table that would rot. Degrade, do not fail.
            self.cache_enabled = False
            self.cache_disabled_reason = str(e)[:200]
            return None

        self._cache_name = d.get("name")
        self._cache_key = digest
        self._cache_tokens = (d.get("usageMetadata") or {}).get("totalTokenCount", 0)
        # Local clock only. The server's `expireTime` is authoritative but
        # needs RFC3339 parsing and a trustworthy local clock to compare
        # against; this deadline exists purely to avoid a wasted round trip.
        # Correctness is guaranteed by the 403 retry in complete(), not here.
        self._cache_deadline = time.monotonic() + self.cache_ttl_s
        self._created_on_last_call = True
        if self._cache_name:
            _LIVE_GEMINI_CACHES.add((self.base, self.key, self._cache_name))
        return self._cache_name

    def close(self) -> None:
        """Hand back the rental. Safe to call repeatedly, and idempotent."""
        if self._cache_name:
            _delete_gemini_cache(self.base, self.key, self._cache_name)
        self._forget_cache()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @classmethod
    def reap_orphans(cls, base_url: Optional[str] = None) -> int:
        """Delete every cache this tool left behind. For cleanup after a crash.

        TTLs mean orphans expire on their own; this makes the wait optional.
        """
        base = (base_url or os.environ.get("GEMINI_BASE_URL") or cls.BASE).rstrip("/")
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        try:
            r = requests.get(f"{base}/cachedContents", timeout=60,
                             headers={"x-goog-api-key": key})
            r.raise_for_status()
            items = r.json().get("cachedContents") or []
        except (requests.RequestException, ValueError):
            return 0
        n = 0
        for it in items:
            if str(it.get("displayName", "")).startswith("kopipasta-"):
                _delete_gemini_cache(base, key, it.get("name", ""))
                n += 1
        return n

    # -- completion ----------------------------------------------------------
    @staticmethod
    def _cache_is_gone(err: str) -> bool:
        """Did this call fail *because* the cached prefix vanished?

        403 rather than 404, and the phrasing is shared with real permission
        errors, so both parts are required — retrying a genuine auth failure
        would just double the latency before the same error.
        """
        return "CachedContent not found" in err

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192,
                 _retry: bool = True) -> Completion:
        gen: Dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_tokens}
        if schema:
            # Enforced by the API. This is what upgrades triage mode from
            # "please return JSON" to a guarantee.
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = schema

        name = self._ensure_cache(prefix)
        # Tokens billed as a cache *write* on this call — nonzero only on a
        # turn that actually created an entry. Anthropic reports this natively;
        # Gemini reports it on the cachedContents resource, so we carry it
        # across by hand. Without it, `cache_creation_tokens` is structurally
        # always 0 for Gemini and no cache experiment can tell success from
        # failure.
        #
        # This asks _ensure_cache what it DID rather than inferring it from
        # whether a handle existed before and after. Inferring gets the TTL
        # re-create wrong — a turn that silently paid for a second write looked
        # free — which is the §2.7 mistake (a real cost hidden by a reporting
        # bug) repeated one layer down.
        created = self._cache_tokens if self._created_on_last_call else 0

        if name:
            # The prefix now lives in the cache resource; sending it again
            # would defeat the entire point.
            body: Dict[str, Any] = {
                "contents": [{"role": "user", "parts": [{"text": suffix}]}],
                "cachedContent": name,
                "generationConfig": gen,
            }
        else:
            body = {
                "contents": [{"role": "user", "parts": [{"text": prefix}, {"text": suffix}]}],
                "generationConfig": gen,
            }

        try:
            d = _post(f"{self.base}/models/{self.model}:generateContent", body,
                      timeout=self.timeout, headers=self.headers)
        except BackendError as e:
            # The cache expired between our deadline check and the request —
            # clock skew, a paused process, a server-side eviction. This is the
            # branch that makes correctness independent of the local clock:
            # forget the dead handle and go again from scratch, exactly once.
            if name and _retry and self._cache_is_gone(str(e)):
                self._forget_cache()
                return self.complete(prefix, suffix, schema=schema,
                                     max_tokens=max_tokens, _retry=False)
            raise

        cands = d.get("candidates") or []
        text = ""
        if cands:
            text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))

        u = d.get("usageMetadata", {})
        reason = cands[0].get("finishReason") if cands else None
        if reason not in (None, "STOP"):
            # Checked regardless of whether text came back. Guarding only the
            # empty case — which is what this did — lets a TRUNCATED answer
            # through as a success, and truncation is the dangerous one: under
            # `responseSchema` the caller gets JSON that stops mid-string,
            # fails to parse, and reads as "no answer" rather than "the answer
            # was cut off". Found by dogfooding: a triage run reported ok=true
            # with a null result.
            detail = f"gemini stopped early: {reason}"
            if reason == "MAX_TOKENS":
                # Thinking tokens are billed against maxOutputTokens, so the
                # budget left for the actual answer is whatever reasoning did
                # not consume. Naming both numbers turns a confusing failure
                # ("I asked for 8192 and got 318") into an obvious one.
                detail += (
                    f" — max_tokens={max_tokens} covers reasoning AND answer;"
                    f" this call spent {u.get('thoughtsTokenCount', 0)} on"
                    f" reasoning and {u.get('candidatesTokenCount', 0)} on the"
                    f" answer. Raise max_tokens."
                )
            raise BackendError(detail)

        return Completion(
            text=text,
            # NB: cachedContentTokenCount is *included* in promptTokenCount, so
            # unlike Anthropic these must NOT be summed.
            input_tokens=u.get("promptTokenCount", 0),
            # Reasoning tokens are billed as output and consume the same
            # budget, so a caller that reports only candidatesTokenCount
            # understates what the turn cost — the same class of mistake as
            # Anthropic's input_tokens in §2.7.
            output_tokens=(u.get("candidatesTokenCount", 0)
                           + u.get("thoughtsTokenCount", 0)),
            cached_tokens=u.get("cachedContentTokenCount", 0),
            cache_creation_tokens=created,
            model=self.model,
            raw=d,
        )


# ---------------------------------------------------------------------------
# openai: — the widest-reach shape. Gemini also speaks this (see GEMINI_COMPAT).
# ---------------------------------------------------------------------------
GEMINI_COMPAT = "https://generativelanguage.googleapis.com/v1beta/openai/"  # trailing / matters


class OpenAICompatBackend:
    def __init__(self, model: str, base_url: Optional[str] = None, timeout: int = 900):
        self.model = model
        self.base = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        body: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "user", "content": prefix + "\n\n" + suffix}],
        }
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "oracle_result", "strict": True, "schema": schema}}

        d = _post(f"{self.base}/chat/completions", body, timeout=self.timeout, headers={
            "authorization": f"Bearer {self.key}", "content-type": "application/json"})

        choices = d.get("choices") or []
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        u = d.get("usage", {})
        return Completion(
            text=text or "",
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            model=d.get("model", self.model),
            raw=d,
        )


# ---------------------------------------------------------------------------
def _post(url: str, body: Dict[str, Any], *, headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise BackendError(f"transport: {e}") from e
    if r.status_code >= 400:
        raise BackendError(f"HTTP {r.status_code}: {r.text[:600]}")
    try:
        return dict(r.json())
    except ValueError as e:
        raise BackendError(f"non-JSON response: {r.text[:300]}") from e


def build(spec: str, *, base_url: Optional[str] = None, timeout: int = 900,
          cache: bool = True, cache_ttl_s: int = GeminiBackend.DEFAULT_TTL_S):
    """`exec:cmd` | `anthropic:model` | `gemini:model` | `openai:model`

    `cache` / `cache_ttl_s` only affect `gemini:`, the one backend where the
    cache is a rented resource with a lifetime we own.
    """
    kind, _, rest = spec.partition(":")
    if not rest:
        raise BackendError(f"backend needs a target: '{spec}' (e.g. gemini:gemini-3-pro)")
    if kind == "exec":
        return ExecBackend(rest, timeout)
    if kind == "claude-cli":
        return ClaudeCliBackend("" if rest == "-" else rest, timeout)
    if kind == "anthropic":
        return AnthropicBackend(rest, base_url, timeout)
    if kind == "gemini":
        return GeminiBackend(rest, base_url, timeout, cache=cache, cache_ttl_s=cache_ttl_s)
    if kind in ("openai", "openai-compat"):
        return OpenAICompatBackend(rest, base_url, timeout)
    if kind == "gemini-compat":
        return OpenAICompatBackend(rest, base_url or GEMINI_COMPAT, timeout)
    raise BackendError(f"unknown backend kind: {kind}")
