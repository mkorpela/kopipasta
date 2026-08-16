"""The error taxonomy — spec §9.

The contract these tests defend: `error` is a stable slug callers may branch
on, `retryable` says whether trying again could help, and every message names
the exact thing to change rather than a concept.
"""

import json

import pytest

from kopipasta.core import errors as e
from kopipasta.interaction import EXIT_NO_HUMAN

ALL_ERRORS = [
    e.UsageError("x"),
    e.NoBackendConfigured("/cfg/config.toml"),
    e.UnknownProvider("gemni", ("gemini", "anthropic"), "cfg"),
    e.MissingApiKey("gemini", "GEMINI_API_KEY", "cfg [ask]"),
    e.ConfigInvalid("/cfg/config.toml", "bad"),
    e.BudgetExceeded(576_000, 400_000, ["a.py"]),
    e.EmptySelection([("-e", "nope.py", 0)]),
    e.BackendError("x"),
    e.AuthRejected("gemini", "GEMINI_API_KEY", "invalid key"),
    e.ModelRejected("gemini", "gemini-9", "not found", "cfg [ask]"),
    e.RateLimited("gemini", 30.0, "quota"),
    e.BackendTimeout("gemini", 900),
    e.ResponseTruncated("gemini", "MAX_TOKENS", 8192, 318),
    e.SchemaInvalid("gemini", "missing 'hypothesis'"),
    e.BackendActedAsAgent("exec:claude -p", "The edit tool needs permission"),
]


@pytest.mark.parametrize("err", ALL_ERRORS, ids=lambda x: type(x).__name__)
def test_every_error_carries_the_full_contract(err):
    payload = err.to_json()
    assert payload["ok"] is False
    assert isinstance(payload["error"], str) and payload["error"]
    assert isinstance(payload["retryable"], bool)
    assert payload["exit"] == err.exit_code
    json.dumps(payload)  # must survive serialisation
    assert err.render().startswith("kopipasta: ")


def test_slugs_are_unique():
    """Slugs are the machine interface; a collision makes them useless."""
    slugs = [x.to_json()["error"] for x in ALL_ERRORS]
    assert len(slugs) == len(set(slugs)), sorted(slugs)


def test_exit_codes_match_the_spec_table():
    assert e.UsageError("x").exit_code == 1
    assert e.NoBackendConfigured("c").exit_code == 2
    assert e.MissingApiKey("p", "V", "s").exit_code == 2
    assert e.BackendError("x").exit_code == 3
    assert e.BudgetExceeded(1, 0, []).exit_code == 6
    assert (
        EXIT_NO_HUMAN == 8
    )  # owned by interaction.py, listed in errors for completeness


def test_retryable_is_not_derived_from_the_exit_code():
    """Exit 3 covers both a transient outage and a model name that will never
    exist. Same code to a shell, opposite advice to a caller."""
    transient = e.RateLimited("gemini", 5, "slow down")
    permanent = e.ModelRejected("gemini", "gemini-9", "not found", "cfg")
    assert transient.exit_code == permanent.exit_code == 3
    assert transient.retryable is True
    assert permanent.retryable is False


def test_auth_rejection_is_not_retryable_and_is_not_exit_3():
    err = e.AuthRejected("gemini", "GEMINI_API_KEY", "API key not valid")
    assert err.exit_code == 2
    assert err.retryable is False


def test_provider_text_is_quoted_verbatim():
    """Paraphrasing upstream errors destroys the detail that identifies them."""
    upstream = (
        "API key not valid. Please pass a valid API key. [reason: API_KEY_INVALID]"
    )
    err = e.AuthRejected("gemini", "GEMINI_API_KEY", upstream)
    assert upstream in err.render()


def test_rate_limit_surfaces_retry_after():
    assert "30s" in e.RateLimited("gemini", 30.0, "quota").render()
    # Unknown optional fields are omitted, not serialised as null (see to_json),
    # so `.get()` is the documented access pattern for anything optional.
    assert e.RateLimited("gemini", None, "quota").to_json().get("retry_after_s") is None
    assert "retry_after_s" not in e.RateLimited("gemini", None, "quota").to_json()
    assert e.RateLimited("gemini", 30.0, "q").to_json()["retry_after_s"] == 30.0


def test_truncation_is_a_failure_not_a_warning():
    """Findings §2.9: a MAX_TOKENS stop with partial text passed as ok=true
    with a null result. Under an enforced schema that is JSON ending
    mid-string."""
    err = e.ResponseTruncated("gemini", "MAX_TOKENS", 8192, 318)
    assert err.to_json()["ok"] is False
    assert "max_tokens" in err.render()
    assert "Reasoning tokens" in err.render()


def test_agent_backend_diagnosis_names_the_flag_that_fixes_it():
    err = e.BackendActedAsAgent(
        "exec:claude -p", "The edit tool needs permission approval"
    )
    assert err.to_json()["error"] == "backend_not_a_completion"
    assert "--disallowedTools" in err.render()
    assert err.retryable is False


def test_empty_selection_reports_per_pattern():
    """A bare '0 files' does not say which of several selectors was wrong."""
    err = e.EmptySelection(
        [("-e", "kopipasta/pacher.py", 0), ("-m", "kopipasta/*.py", 16)],
        candidates=["kopipasta/patcher.py", "kopipasta/prompt.py"],
    )
    out = err.render()
    assert "kopipasta/pacher.py" in out and "0 files" in out
    assert "kopipasta/*.py" in out and "16 files" in out
    assert "did you mean kopipasta/patcher.py?" in out


def test_did_you_mean_matches_on_basename_not_whole_path():
    """Whole-path similarity dilutes a one-character filename typo in a deep
    tree, which is the common case."""
    assert (
        e._did_you_mean("a/b/c/d/e/patcherr.py", ["z/y/x/w/v/patcher.py"])
        == "z/y/x/w/v/patcher.py"
    )


def test_did_you_mean_stays_quiet_when_nothing_is_close():
    assert e._did_you_mean("totally_unrelated.rs", ["kopipasta/patcher.py"]) is None


def test_empty_selection_survives_having_no_candidates():
    err = e.EmptySelection([("-e", "nope.py", 0)], candidates=None)
    assert "nope.py" in err.render()


def test_message_shape_is_what_then_why_then_next_action():
    err = e.MissingApiKey("gemini", "GEMINI_API_KEY", "cfg [ask]")
    lines = [ln for ln in err.render().splitlines() if ln.strip()]
    assert lines[0].startswith("kopipasta: no API key")
    assert "GEMINI_API_KEY is unset" in lines[1]
    assert any("export GEMINI_API_KEY" in ln for ln in lines[2:])


def test_extra_fields_reach_the_json_but_none_are_none():
    err = e.BudgetExceeded(576_000, 400_000, ["a.py", "b.py"])
    payload = err.to_json()
    assert payload["wanted_tokens"] == 576_000
    assert payload["demoted"] == ["a.py", "b.py"]
    assert None not in payload.values()
