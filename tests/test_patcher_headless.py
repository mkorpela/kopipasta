"""Spec 9 / 11.2: with no human attached, the patcher's confirmations become
policy rather than questions.

`apply_patches` is the most dangerous function to leave unguarded, because it
is the one that writes to the worktree. It is also *not* TUI-only: twelve test
modules call it directly, and the headless `apply` verb in 3 will too. Both
prompts already default to False and skip on decline, so the conservative
headless answer is the answer a careful human would have given anyway.

One shape constraint worth remembering: this must not RAISE for the missing
human. The per-file body of apply_patches is wrapped in a broad
`except Exception`, which would swallow a refusal and misreport it as a
corrupt patch. Injecting the decision is the only thing that survives that.
"""

import pytest

from kopipasta.patcher import apply_patches, parse_llm_output


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr("kopipasta.patcher.human_attached", lambda: False)


@pytest.fixture
def with_human(monkeypatch):
    monkeypatch.setattr("kopipasta.patcher.human_attached", lambda: True)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


DELETE_OUTPUT = "```python\n# FILE: doomed.py\n<<<DELETE>>>\n```"


def _shrink_output(name):
    return f"```python\n# FILE: {name}\nb\n```"


# -- deletes ---------------------------------------------------------------


def test_delete_is_refused_with_no_human_and_no_flag(headless, workdir, capsys):
    doomed = workdir / "doomed.py"
    doomed.write_text("print('x')\n")

    apply_patches(parse_llm_output(DELETE_OUTPUT))

    assert doomed.exists(), "a model's say-so deleted a file with nobody watching"
    assert "--allow-delete" in capsys.readouterr().err


def test_delete_proceeds_when_explicitly_permitted(headless, workdir):
    doomed = workdir / "doomed.py"
    doomed.write_text("print('x')\n")

    modified = apply_patches(parse_llm_output(DELETE_OUTPUT), allow_delete=True)

    assert not doomed.exists()
    assert any("doomed.py" in m for m in modified)


def test_the_flag_does_not_silently_prompt_instead(headless, workdir, monkeypatch):
    """--allow-delete means 'may delete', and with no human it must not reach
    a prompt at all — that would be the stall it exists to prevent."""

    def explode(*a, **k):
        raise AssertionError("click.confirm reached with no human attached")

    monkeypatch.setattr("kopipasta.patcher.click.confirm", explode)
    (workdir / "doomed.py").write_text("print('x')\n")
    apply_patches(parse_llm_output(DELETE_OUTPUT), allow_delete=True)


def test_no_prompt_is_attempted_when_declining_either(headless, workdir, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("click.confirm reached with no human attached")

    monkeypatch.setattr("kopipasta.patcher.click.confirm", explode)
    (workdir / "doomed.py").write_text("print('x')\n")
    apply_patches(parse_llm_output(DELETE_OUTPUT))


def test_a_present_human_is_still_asked_even_with_the_flag(
    with_human, workdir, monkeypatch
):
    """--allow-delete says 'this run may delete', not 'delete without telling
    me'. A human who says no still wins."""
    asked = {"n": 0}

    def say_no(*a, **k):
        asked["n"] += 1
        return False

    monkeypatch.setattr("kopipasta.patcher.click.confirm", say_no)
    doomed = workdir / "doomed.py"
    doomed.write_text("print('x')\n")

    apply_patches(parse_llm_output(DELETE_OUTPUT), allow_delete=True)

    assert asked["n"] == 1
    assert doomed.exists()


# -- the shrink / hallucination guard --------------------------------------


def test_suspicious_overwrite_is_skipped_with_no_human(headless, workdir, capsys):
    target = workdir / "big.py"
    target.write_text("a" * 1000)

    apply_patches(parse_llm_output(_shrink_output("big.py")))

    assert target.read_text() == "a" * 1000, "a 99% file shrink landed unreviewed"
    assert "--force" in capsys.readouterr().err


def test_force_overrides_the_shrink_guard(headless, workdir):
    target = workdir / "big.py"
    target.write_text("a" * 1000)

    apply_patches(parse_llm_output(_shrink_output("big.py")), force=True)

    assert target.read_text().strip() == "b"


def test_force_does_not_also_permit_deletes(headless, workdir):
    """The two flags are separate on purpose: overwriting a file you can still
    recover from git is not the same risk as removing it."""
    doomed = workdir / "doomed.py"
    doomed.write_text("print('x')\n")

    apply_patches(parse_llm_output(DELETE_OUTPUT), force=True)

    assert doomed.exists()


def test_allow_delete_does_not_also_force_overwrites(headless, workdir):
    target = workdir / "big.py"
    target.write_text("a" * 1000)

    apply_patches(parse_llm_output(_shrink_output("big.py")), allow_delete=True)

    assert target.read_text() == "a" * 1000


# -- the shape constraint --------------------------------------------------


def test_refusal_is_not_reported_as_a_patch_error(headless, workdir, capsys):
    """Raising NoHumanAttached here would be swallowed by the broad
    `except Exception` around the per-file body and surface as
    'Error processing ...', sending the caller to debug the patch."""
    (workdir / "doomed.py").write_text("print('x')\n")

    apply_patches(parse_llm_output(DELETE_OUTPUT))

    assert "Error processing" not in capsys.readouterr().out


def test_ordinary_patches_still_apply_headlessly(headless, workdir):
    """The guard must only affect the destructive branches. A normal patch
    needs no human and must not have acquired one."""
    target = workdir / "plain.py"
    target.write_text("old\n")

    apply_patches(
        parse_llm_output("```python\n# FILE: plain.py\nnew content here\n```")
    )

    assert "new content here" in target.read_text()
