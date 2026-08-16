"""Modes: the template and the schema must not drift apart — spec §10.

Where the provider enforces the schema, *the schema wins*. A template asking
for fields the schema does not carry loses them silently: same input, quietly
worse output, no error, and it only shows up when you switch backends. That
happened — `why` and `confidence` disappeared off every cited file (findings,
trap 3) — so the agreement is asserted rather than maintained by care.
"""

import pytest

from kopipasta.core import modes
from kopipasta.core.errors import UsageError


def required_fields(schema, prefix=""):
    """Every required key in a schema, including nested object properties."""
    out = set()
    for name in schema.get("required", []):
        out.add(name)
        child = (schema.get("properties") or {}).get(name, {})
        if child.get("type") == "object":
            out |= required_fields(child, name)
        if child.get("type") == "array":
            item = child.get("items") or {}
            if item.get("type") == "object":
                out |= required_fields(item, name)
    return out


@pytest.mark.parametrize("name", modes.MODE_NAMES)
def test_every_required_schema_field_is_named_in_the_template(name):
    mode = modes.get(name)
    if not mode.schema:
        return
    missing = {f for f in required_fields(mode.schema) if f not in mode.instructions}
    assert not missing, f"--mode {name} enforces {missing} but never asks for it"


@pytest.mark.parametrize("name", modes.MODE_NAMES)
def test_every_mode_gives_permission_to_admit_ignorance(name):
    """A confident claim about a file the model never read is a guess wearing
    a score, so every template has to make "I did not see it" an option."""
    text = modes.get(name).instructions.lower()
    assert "missing" in text or "nobody can answer" in text


@pytest.mark.parametrize("name", modes.MODE_NAMES)
def test_no_mode_invites_line_numbers_as_citations(name):
    mode = modes.get(name)
    if mode.expects_code:
        return  # a patch cites nothing; it replaces text
    assert "line number" in mode.instructions.lower()


def test_structured_modes_can_render_themselves_for_a_human():
    for name in modes.MODE_NAMES:
        mode = modes.get(name)
        if mode.schema:
            assert mode.summary is not None, f"--mode {name} has no human rendering"


def test_triage_renders_the_answer_a_caller_asked_for():
    out = modes.TRIAGE.summary(
        {
            "hypothesis": "expiry is checked twice",
            "relevant_files": [
                {"path": "auth.py", "why": "owns expiry", "confidence": 0.9}
            ],
            "missing_context": ["tests/"],
            "suggested_selection": ["auth.py"],
        }
    )
    assert "expiry is checked twice" in out
    assert "0.90  auth.py" in out
    assert "tests/" in out
    assert "--from-file" in out  # the loop back into the next call


def test_a_summary_survives_a_provider_that_returns_junk_in_a_valid_shape():
    """The schema guarantees the keys, not that every item is an object."""
    assert (
        modes.TRIAGE.summary({"relevant_files": ["auth.py", None], "hypothesis": ""})
        == ""
    )


def test_aliases_resolve_to_the_same_mode():
    assert modes.get("default") is modes.get("answer")


def test_an_unknown_mode_names_the_real_ones():
    with pytest.raises(UsageError) as exc:
        modes.get("triaje")
    assert "triage" in exc.value.detail


def test_a_user_template_replaces_the_words_and_keeps_the_shape(tmp_path, monkeypatch):
    """Templates stay user-editable (spec §10). The schema does not travel
    with them: an edited template that no longer matches would be silently
    overruled by the provider, which is the drift this file exists to stop."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    directory = tmp_path / "kopipasta" / "modes"
    directory.mkdir(parents=True)
    (directory / "triage.md").write_text("Only answer in Finnish.")
    mode = modes.get("triage")
    assert mode.instructions == "Only answer in Finnish."
    assert mode.schema is modes.TRIAGE_SCHEMA
    assert mode.summary is not None
