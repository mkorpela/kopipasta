"""The error taxonomy — spec §9.

Both consumers of this tool are bad at guessing. A human types the command
once a week and does not remember the flags; an agent cannot guess at all, and
will either retry a permanent failure forever or abandon a task over a blip.

So every failure carries four things, and the class is the only place they are
allowed to be decided:

    summary     what failed, in one line
    detail      why, including anything the provider said, verbatim
    hint        the next action -- a command or the config lines, not a concept
    retryable   whether trying again could possibly help

`retryable` is deliberately NOT derived from the exit code. The codes are
coarse by design (§8) and one of them covers both "the API is briefly down"
and "you named a model that does not exist" -- same class of failure to a
shell, opposite advice to a caller. Anything consuming this programmatically
should branch on `retryable`; the exit code is for shells and `set -e`.
"""

from __future__ import annotations

import difflib
import os
from typing import Any, Dict, List, Optional, Sequence

from kopipasta.interaction import EXIT_NO_HUMAN

# Spec §8. EXIT_NO_HUMAN (8) is imported rather than redeclared:
# kopipasta.interaction owns that decision, and two definitions of one exit
# code is how they drift apart.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_BACKEND = 2
EXIT_BACKEND = 3
EXIT_PATCH_PARTIAL = 4
EXIT_PATCH_FAILED = 5
EXIT_BUDGET = 6
EXIT_VERIFY = 7


class KopipastaError(Exception):
    """Base for every failure the CLI reports deliberately.

    Subclasses set `exit_code`, `slug` and `retryable`. `slug` is a stable
    machine-readable identifier: it is part of the interface and must not be
    reworded once callers branch on it, however much the prose changes.
    """

    exit_code: int = EXIT_USAGE
    slug: str = "error"
    retryable: bool = False

    def __init__(
        self,
        summary: str,
        *,
        detail: Optional[str] = None,
        hint: Optional[str] = None,
        **fields: Any,
    ) -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail
        self.hint = hint
        self.fields: Dict[str, Any] = {k: v for k, v in fields.items() if v is not None}

    def render(self) -> str:
        """The human form: what failed, why, what to do -- in that order."""
        out = [f"kopipasta: {self.summary}"]
        if self.detail:
            out += [f"  {line}" for line in self.detail.splitlines()]
        if self.hint:
            out.append("")
            out += [f"  {line}" for line in self.hint.splitlines()]
        return "\n".join(out)

    def to_json(self) -> Dict[str, Any]:
        """The machine form. `error` and `retryable` are the contract.

        Optional fields that are None are omitted rather than serialised as
        null, so a payload only carries what is actually known. Consumers must
        therefore use `.get()` for anything outside the guaranteed set
        (ok, error, exit, retryable, summary) -- absent and null mean the same
        thing here, and `.get()` treats them the same way.
        """
        payload: Dict[str, Any] = {
            "ok": False,
            "error": self.slug,
            "exit": self.exit_code,
            "retryable": self.retryable,
            "summary": self.summary,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.fields)
        return payload


# --------------------------------------------------------------------------
# Configuration and usage -- never retryable, the caller must change something
# --------------------------------------------------------------------------
class UsageError(KopipastaError):
    exit_code = EXIT_USAGE
    slug = "usage"


class InteractionRequired(KopipastaError):
    """`interaction.NoHumanAttached`, in the shape the CLI reports failures in.

    Distinct from a usage error because the fix is different: the caller needs
    a policy flag or a different invocation, not a corrected command line.
    """

    exit_code = EXIT_NO_HUMAN
    slug = "interaction_required"
    retryable = False

    def __init__(self, summary: str, hint: Optional[str] = None) -> None:
        super().__init__(
            summary,
            hint=hint or "Pass the flag that answers the question, or run it in a terminal.",
        )


class NoBackendConfigured(KopipastaError):
    """First-run experience as much as an error, so it doubles as onboarding."""

    exit_code = EXIT_NO_BACKEND
    slug = "no_backend"

    def __init__(self, config_path: str) -> None:
        super().__init__(
            "no model backend is configured.",
            detail="Nothing was found in --backend, KOPIPASTA_BACKEND, or the config file.",
            hint=(
                "Set one, in increasing order of permanence:\n"
                "  kopipasta ask --backend gemini:gemini-3.7-flash -q '...'\n"
                "  export KOPIPASTA_BACKEND=gemini:gemini-3.7-flash\n"
                f"  kopipasta --edit-config      # {config_path}"
            ),
            config_path=config_path,
        )


class UnknownProvider(KopipastaError):
    exit_code = EXIT_USAGE
    slug = "unknown_provider"

    def __init__(self, provider: str, valid: Sequence[str], source: str) -> None:
        suggestion = difflib.get_close_matches(provider, list(valid), n=1, cutoff=0.6)
        hint = f"Valid providers: {', '.join(valid)}."
        if suggestion:
            hint = f"Did you mean '{suggestion[0]}'?\n{hint}"
        super().__init__(
            f"unknown provider {provider!r}.",
            detail=f"Resolved from {source}.",
            hint=hint,
            provider=provider,
            resolved_from=source,
            valid_providers=list(valid),
        )


class MissingApiKey(KopipastaError):
    """Names where the provider came from, not just which key is absent.

    Half of all credential bugs are the wrong config winning, and
    "GEMINI_API_KEY unset" sends you to fix the key when the real problem is
    that you did not expect to be talking to Gemini at all.
    """

    exit_code = EXIT_NO_BACKEND
    slug = "no_api_key"

    def __init__(self, provider: str, env_var: str, source: str) -> None:
        super().__init__(
            f"no API key for provider {provider!r}.",
            detail=f"Resolved from {source}; {env_var} is unset.",
            hint=(
                f"export {env_var}=...\n"
                "Or switch provider:  kopipasta --edit-config\n"
                "See what resolved:   kopipasta config --show"
            ),
            provider=provider,
            missing_env=env_var,
            resolved_from=source,
        )


class ConfigInvalid(KopipastaError):
    exit_code = EXIT_USAGE
    slug = "config_invalid"

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(
            f"could not read {path}.",
            detail=detail,
            hint="Fix the file, or delete it to fall back to defaults.",
            config_path=path,
        )


class BudgetExceeded(KopipastaError):
    exit_code = EXIT_BUDGET
    slug = "budget_exceeded"

    def __init__(self, wanted: int, budget: int, demoted: Sequence[str]) -> None:
        super().__init__(
            f"selection needs ~{wanted:,} tokens, over the {budget:,} budget.",
            detail=f"{len(demoted)} file(s) would be demoted, but --strict-budget forbids it.",
            hint="Raise --budget, narrow the selection, or drop --strict-budget.",
            wanted_tokens=wanted,
            budget_tokens=budget,
            demoted=list(demoted),
        )


class EmptySelection(KopipastaError):
    """The most dangerous failure in the tool, which is why it is an error.

    A typo'd glob selects nothing, the model answers from the project structure
    alone, and the answer reads perfectly fine. No exception, no warning, and a
    plausible response produced from nothing. Reported per pattern, because
    with several selectors a bare "0 files" does not say which one was wrong.
    """

    exit_code = EXIT_USAGE
    slug = "empty_selection"

    def __init__(self, matches: Sequence[tuple], candidates: Optional[Sequence[str]] = None):
        lines: List[str] = []
        for flag, pattern, count in matches:
            line = f"{flag} {pattern}".ljust(34) + f"{count:>4} files"
            if count == 0 and candidates:
                near = _did_you_mean(pattern, candidates)
                if near:
                    line += f"   (did you mean {near}?)"
            lines.append(line)
        super().__init__(
            "no files matched.",
            detail="\n".join(lines) + "\nNothing was selected, so there is nothing to ask about.",
            hint="Check the patterns above, or use --all to select the whole project.",
            patterns=[{"flag": f, "pattern": p, "matched": c} for f, p, c in matches],
        )


def _did_you_mean(pattern: str, candidates: Sequence[str]) -> Optional[str]:
    """Nearest real path to a pattern that matched nothing.

    Compares on the basename as well as the whole path: a single-character
    typo in a filename is the common case, and whole-path similarity dilutes
    it badly in a deep tree.
    """
    base = os.path.basename(pattern)
    by_base = {os.path.basename(c): c for c in candidates}
    hit = difflib.get_close_matches(base, list(by_base), n=1, cutoff=0.7)
    if hit:
        return by_base[hit[0]]
    hit = difflib.get_close_matches(pattern, list(candidates), n=1, cutoff=0.7)
    return hit[0] if hit else None


# --------------------------------------------------------------------------
# Backend failures -- exit 3, but retryability varies and is stated per class
# --------------------------------------------------------------------------
class BackendError(KopipastaError):
    exit_code = EXIT_BACKEND
    slug = "backend_error"
    retryable = True


class AuthRejected(KopipastaError):
    """Credentials present but refused. Exit 2, not 3: nothing to retry."""

    exit_code = EXIT_NO_BACKEND
    slug = "auth_rejected"
    retryable = False

    def __init__(self, provider: str, env_var: str, provider_message: str) -> None:
        super().__init__(
            f"{provider} rejected the credentials.",
            detail=f"Provider said: {provider_message}",
            hint=f"Check {env_var}, and that the key is entitled to this model.",
            provider=provider,
            missing_env=env_var,
        )


class ModelRejected(BackendError):
    """Exit 3 like other backend failures, but retrying will never help.

    This is exactly why `retryable` is not derived from the exit code.
    """

    slug = "model_rejected"
    retryable = False

    def __init__(self, provider: str, model: str, provider_message: str, source: str) -> None:
        KopipastaError.__init__(
            self,
            f"{provider} rejected the model {model!r}.",
            detail=f"Resolved from {source}. Provider said: {provider_message}",
            hint="kopipasta config --show     # check which model resolved\nkopipasta --edit-config",
            provider=provider,
            model=model,
            resolved_from=source,
        )


class RateLimited(BackendError):
    slug = "rate_limited"
    retryable = True

    def __init__(self, provider: str, retry_after_s: Optional[float], provider_message: str) -> None:
        wait = f" Retry after {retry_after_s:g}s." if retry_after_s else ""
        KopipastaError.__init__(
            self,
            f"{provider} rate-limited the request.",
            detail=f"Provider said: {provider_message}{wait}",
            hint="Retrying is expected to work.",
            provider=provider,
            retry_after_s=retry_after_s,
        )


class BackendTimeout(BackendError):
    slug = "timeout"
    retryable = True

    def __init__(self, provider: str, timeout_s: float) -> None:
        KopipastaError.__init__(
            self,
            f"{provider} did not respond within {timeout_s:g}s.",
            hint="Raise timeout_s in the config, or narrow the selection.",
            provider=provider,
            timeout_s=timeout_s,
        )


class DeadlineExceeded(BackendError):
    """`--deadline` caps the whole invocation, not one call.

    Separate from a backend timeout because nothing failed on the provider's
    side: we ran out of the caller's clock, possibly during selection or
    rendering. Retrying is pointless without raising the deadline, so it says
    so rather than leaving a harness to loop.
    """

    slug = "deadline_exceeded"
    retryable = False

    def __init__(self, elapsed_s: float, deadline_s: float, stage: str) -> None:
        KopipastaError.__init__(
            self,
            f"the {deadline_s:g}s deadline elapsed during {stage}.",
            detail=f"{elapsed_s:.1f}s spent before it did.",
            hint="Raise --deadline, or narrow the selection so there is less to assemble.",
            deadline_s=deadline_s,
            elapsed_s=round(elapsed_s, 1),
            stage=stage,
        )


class ResponseTruncated(BackendError):
    """Never report a truncated answer as success.

    Measured (findings §2.9): a finish-reason guard that only fired on *empty*
    text let a MAX_TOKENS stop through as ok=true with a null result -- under an
    enforced schema, JSON ending mid-string. Reasoning tokens also spend the
    output budget, so a generous limit is not a guarantee.
    """

    slug = "truncated"
    retryable = False

    def __init__(self, provider: str, finish_reason: str, max_tokens: int, chars: int) -> None:
        KopipastaError.__init__(
            self,
            f"{provider} stopped early ({finish_reason}); the answer is incomplete.",
            detail=f"Got {chars:,} characters against a {max_tokens:,}-token limit. "
            "Reasoning tokens also spend that budget.",
            hint="Raise max_tokens in the config, or ask a narrower question.",
            provider=provider,
            finish_reason=finish_reason,
            max_tokens=max_tokens,
        )


class SchemaInvalid(BackendError):
    slug = "schema_invalid"
    retryable = True

    def __init__(self, provider: str, detail: str, response_path: Optional[str] = None) -> None:
        KopipastaError.__init__(
            self,
            f"{provider} returned a response that does not match the expected schema.",
            detail=detail,
            hint="Resampling may work. The raw response is kept for inspection."
            if response_path
            else "Resampling may work.",
            provider=provider,
            response=response_path,
        )


class PatchNotParseable(KopipastaError):
    """It tried to emit a patch and the format was wrong.

    Deliberately not `BackendActedAsAgent`. That one means the backend never
    attempted a patch at all, and its fix — disable the backend's file and
    shell tools — is useless here: a raw API call has no tools to disable.
    Sending a caller to reconfigure a backend that behaved correctly costs it
    the one thing it cannot get back, which is the next attempt.

    Retryable, because the usual cause is a missing fence and asking again
    often produces one. Exit 5 rather than 3: spec §6 puts "misconfigured
    backend" and "bad patch" on different codes, and this is the second.
    """

    slug = "unparseable_patch"
    retryable = True
    exit_code = EXIT_PATCH_FAILED

    def __init__(self, backend: str, excerpt: str) -> None:
        KopipastaError.__init__(
            self,
            "the response looks like a patch, but none of it parsed.",
            detail=f"It said: {excerpt}",
            hint="Most often a code block with no ``` fence around it — the "
            "fence is\nwhat marks where a change ends. Re-run the same "
            "question, or apply the\nartifact by hand after fixing the "
            "formatting.",
            backend=backend,
        )


class BackendActedAsAgent(BackendError):
    """The `claude -p` failure: handed a file and a task, it reached for its
    own edit tool and blocked on a permission prompt instead of emitting a
    patch. Diagnosed separately because "misconfigured backend" and "the model
    wrote a bad patch" send you to completely different places.
    """

    slug = "backend_not_a_completion"
    retryable = False

    def __init__(self, backend: str, excerpt: str) -> None:
        KopipastaError.__init__(
            self,
            "the backend behaved as an agent, not a completion.",
            detail=f"It returned no code blocks. It said: {excerpt}",
            hint="Run the backend with its file and shell tools disabled, e.g.\n"
            "  exec:claude -p --disallowedTools \"Edit,Write,Read,Bash,Glob,Grep,Task\"",
            backend=backend,
        )
