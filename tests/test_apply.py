"""`kopipasta apply`, end to end — spec §8 exit codes and §11 patch safety.

Every test drives the real verb through `run()`, so the output contract, the
exit codes and the git interaction are exercised together. The exit codes are
the interface here more than anywhere else in the tool: 4 and 5 are the
difference between "retry is safe" and "you have a mess to clean up", and a
caller that cannot tell them apart will either lose work or refuse to proceed
after a failure that touched nothing.

`git` is real in these tests rather than mocked. The worktree check is the
entire safety model — "a clean worktree is the undo" — and mocking the thing
that decides whether the undo exists would test the mock.
"""

import json
import shutil
import subprocess

import pytest

from kopipasta.core import apply as applymod
from kopipasta.core.errors import (
    EXIT_OK,
    EXIT_PATCH_FAILED,
    EXIT_PATCH_PARTIAL,
    EXIT_USAGE,
    EXIT_VERIFY,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

ORIGINAL = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"

CLEAN_PATCH = """
Here is the change.

### app.py

```python
<<<<
def a():
    return 1
====
def a():
    return 100
>>>>
```
"""

HALF_MATCHING_PATCH = """
### app.py

```python
<<<<
def a():
    return 1
====
def a():
    return 100
>>>>
<<<<
def nowhere_to_be_found():
    raise SystemExit
====
def nowhere_to_be_found():
    pass
>>>>
```
"""

NOTHING_MATCHES = """
### app.py

```python
<<<<
def nowhere_to_be_found():
    raise SystemExit
====
def nowhere_to_be_found():
    pass
>>>>
```
"""


def _git(cwd, *args):
    subprocess.run(
        [shutil.which("git"), *args], cwd=str(cwd), check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo, committed clean.

    `.kopipasta/` is ignored here exactly as `Session._ensure_dir` arranges in
    production — session artifacts are records, not source, and leaving them
    untracked would make every session dirty its own worktree and lock the
    caller out of `apply`.
    """
    work = tmp_path / "repo"
    work.mkdir()
    (work / "app.py").write_text(ORIGINAL)
    (work / "ref.py").write_text("REFERENCE = 1\n")
    (work / ".gitignore").write_text(".kopipasta/\n")
    if shutil.which("git"):
        _git(work, "init", "-q")
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "t")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "init")
    monkeypatch.chdir(work)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    return work


def write_patch(repo, text, name="patch.md"):
    """Written outside the worktree: a patch file dropped inside it would
    make the repo dirty and `apply` would rightly refuse to run."""
    path = repo.parent / name
    path.write_text(text)
    return str(path)


def run(*argv, expect=EXIT_OK):
    code = applymod.run(list(argv))
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


def run_json(capsys, *argv, expect=EXIT_OK):
    run(*argv, "--json", expect=expect)
    return json.loads(capsys.readouterr().out)


# -- the happy path ---------------------------------------------------------


@needs_git
def test_a_clean_patch_applies_and_exits_0(repo, capsys):
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH))
    assert data["ok"] is True
    assert data["applied"] == ["app.py"]
    assert "return 100" in (repo / "app.py").read_text()


@needs_git
def test_stdin_is_a_target(repo, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(CLEAN_PATCH))
    data = run_json(capsys, "-")
    assert data["applied"] == ["app.py"]


# -- exit 4 vs exit 5, the distinction that justifies the verb --------------


@needs_git
def test_a_half_matching_patch_exits_4_and_says_which_file(repo, capsys):
    """Spec §8: partially applied. The file was written, so the worktree is
    dirty and the caller has cleanup to do — this must never be exit 0."""
    data = run_json(
        capsys, write_patch(repo, HALF_MATCHING_PATCH), expect=EXIT_PATCH_PARTIAL
    )
    assert data["ok"] is False
    assert data["partial"] == ["app.py"]
    assert data["changed"] is True
    assert data["hunks"]["app.py"] == {"applied": 1, "total": 2}
    assert "return 100" in (repo / "app.py").read_text()


@needs_git
def test_a_patch_that_matches_nothing_exits_5_and_leaves_the_worktree_alone(
    repo, capsys
):
    """Spec §8: fully failed, worktree untouched — so a retry is safe."""
    data = run_json(
        capsys, write_patch(repo, NOTHING_MATCHES), expect=EXIT_PATCH_FAILED
    )
    assert data["failed"] == ["app.py"]
    assert data["changed"] is False
    assert (repo / "app.py").read_text() == ORIGINAL


@needs_git
def test_the_two_failure_codes_are_not_the_same_number(repo, capsys):
    """Pinned as its own test because collapsing them is the tempting
    simplification, and it is the one that loses work."""
    assert EXIT_PATCH_PARTIAL != EXIT_PATCH_FAILED
    run(write_patch(repo, NOTHING_MATCHES), "--json", expect=EXIT_PATCH_FAILED)
    capsys.readouterr()
    run(write_patch(repo, HALF_MATCHING_PATCH), "--json", expect=EXIT_PATCH_PARTIAL)


# -- the clean worktree is the undo ----------------------------------------


@needs_git
def test_a_dirty_worktree_is_refused(repo, capsys):
    (repo / "app.py").write_text(ORIGINAL + "# uncommitted\n")
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH), expect=EXIT_USAGE)
    assert data["error"] == "dirty_worktree"
    assert "app.py" in data["files"]
    assert "# uncommitted" in (repo / "app.py").read_text(), (
        "refused, but still patched"
    )


@needs_git
def test_dirty_ok_overrides_it(repo, capsys):
    (repo / "ref.py").write_text("REFERENCE = 2\n")
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH), "--dirty-ok")
    assert data["ok"] is True
    assert "return 100" in (repo / "app.py").read_text()


@needs_git
def test_the_refusal_names_the_files_and_the_way_out(repo, capsys):
    (repo / "app.py").write_text(ORIGINAL + "# uncommitted\n")
    run(write_patch(repo, CLEAN_PATCH), expect=EXIT_USAGE)
    err = capsys.readouterr().err
    assert "app.py" in err
    assert "--dirty-ok" in err


def test_a_missing_git_is_narrated_not_fatal(repo, capsys, monkeypatch):
    """No repo means no undo. That is worth saying rather than pretending
    the worktree was clean."""
    monkeypatch.setattr(applymod, "dirty_files", lambda root: None)
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH))
    assert data["ok"] is True


# -- --dry-run --------------------------------------------------------------


@needs_git
def test_dry_run_touches_nothing_but_reports_the_same_verdict(repo, capsys):
    data = run_json(
        capsys,
        write_patch(repo, HALF_MATCHING_PATCH),
        "--dry-run",
        expect=EXIT_PATCH_PARTIAL,
    )
    assert data["dry_run"] is True
    assert data["partial"] == ["app.py"]
    assert (repo / "app.py").read_text() == ORIGINAL, "dry run wrote to disk"


@needs_git
def test_dry_run_does_not_need_a_clean_worktree(repo, capsys):
    """It cannot damage anything, so refusing would be pure friction."""
    (repo / "app.py").write_text(ORIGINAL + "# uncommitted\n")
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH), "--dry-run")
    assert data["ok"] is True
    assert "# uncommitted" in (repo / "app.py").read_text()


# -- --verify and --revert-on-fail -----------------------------------------


@needs_git
def test_a_failing_verify_exits_7(repo, capsys):
    data = run_json(
        capsys, write_patch(repo, CLEAN_PATCH), "--verify", "exit 3", expect=EXIT_VERIFY
    )
    assert data["verify"]["exit"] == 3
    # Not reverted: the caller did not ask for that.
    assert "return 100" in (repo / "app.py").read_text()


@needs_git
def test_a_passing_verify_exits_0(repo, capsys):
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH), "--verify", "exit 0")
    assert data["ok"] is True
    assert data["verify"]["exit"] == 0


@needs_git
def test_revert_on_fail_puts_the_file_back(repo, capsys):
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["app.py"]
    assert (repo / "app.py").read_text() == ORIGINAL


@needs_git
def test_revert_on_fail_removes_a_file_it_created(repo, capsys):
    """`git checkout --` cannot restore a file that was never tracked."""
    patch = write_patch(repo, "```python\n# FILE: brand_new.py\nprint('hi')\n```")
    data = run_json(
        capsys, patch, "--verify", "exit 1", "--revert-on-fail", expect=EXIT_VERIFY
    )
    assert data["reverted"] == ["brand_new.py"]
    assert not (repo / "brand_new.py").exists()


@needs_git
def test_revert_refuses_to_discard_work_it_did_not_do(repo, capsys):
    """The dangerous case: --dirty-ok plus --revert-on-fail. Reverting a file
    the caller had already modified would destroy uncommitted work to tidy up
    after a failed test run — a worse outcome than leaving the patch."""
    (repo / "app.py").write_text(ORIGINAL + "# precious\n")
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["revert_declined"] == ["app.py"]
    assert data["reverted"] == []
    assert "# precious" in (repo / "app.py").read_text()


@needs_git
def test_revert_matches_paths_the_way_git_spells_them(repo, capsys):
    """The same file, spelled two ways, is still the same file.

    `git status --porcelain` says `app.py`; a model writes whatever it likes,
    and `./app.py` is common. Comparing them raw made the "was it already
    dirty?" check miss, so --dirty-ok with --revert-on-fail ran
    `git checkout -- ./app.py` over uncommitted work. Found by dogfooding this
    file at the oracle.
    """
    (repo / "app.py").write_text(ORIGINAL + "# precious\n")
    dotted = CLEAN_PATCH.replace("### app.py", "### ./app.py")
    data = run_json(
        capsys,
        write_patch(repo, dotted),
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )

    assert data["reverted"] == [], "reverted a file the caller had modified"
    assert data["revert_declined"] == ["./app.py"]
    assert "# precious" in (repo / "app.py").read_text()


@needs_git
def test_a_session_with_nothing_editable_still_enforces_the_zone(repo, capsys):
    """`editable or None` collapsed two different answers into one.

    "No record to enforce against" and "the record says nothing was editable"
    are opposites: the first cannot be checked, the second must be. Returning
    None for both let a triage session — all -r and -m, no -e — patch any file
    in the repo.
    """
    d = repo / ".kopipasta" / "sessions" / "s1"
    d.mkdir(parents=True)
    (d / "selection.json").write_text(
        json.dumps(
            {"1": {"files": {"ref.py": {"role": "ref", "hash": "y"}}, "demoted": []}}
        )
    )
    (d / "001-response.md").write_text(
        """
### ref.py

```python
<<<<
REFERENCE = 1
====
REFERENCE = 99
>>>>
```
"""
    )
    data = run_json(capsys, "--session", "s1", expect=EXIT_PATCH_FAILED)
    assert data["skipped"] == ["ref.py"]
    assert (repo / "ref.py").read_text() == "REFERENCE = 1\n"


def test_revert_on_fail_without_verify_is_a_usage_error(repo, capsys):
    data = run_json(
        capsys, write_patch(repo, CLEAN_PATCH), "--revert-on-fail", expect=EXIT_USAGE
    )
    assert data["error"] == "usage"


# -- the editable zone, spec §11 -------------------------------------------


@needs_git
def test_a_patch_outside_the_sessions_editable_set_is_refused(repo, capsys):
    _write_session(repo, "s1", editable=["app.py"])
    patch = """
### ref.py

```python
<<<<
REFERENCE = 1
====
REFERENCE = 99
>>>>
```
"""
    (repo / ".kopipasta" / "sessions" / "s1" / "001-response.md").write_text(patch)
    data = run_json(capsys, "--session", "s1", expect=EXIT_PATCH_FAILED)
    assert data["skipped"] == ["ref.py"]
    assert (repo / "ref.py").read_text() == "REFERENCE = 1\n", (
        "read-only file was edited"
    )


@needs_git
def test_a_new_file_is_allowed_even_though_it_is_not_in_the_set(repo, capsys):
    """The guard exists to stop an -r file being rewritten, and such a file
    exists by definition. Refusing creations would make "add a module"
    impossible."""
    _write_session(repo, "s1", editable=["app.py"])
    (repo / ".kopipasta" / "sessions" / "s1" / "001-response.md").write_text(
        "```python\n# FILE: brand_new.py\nprint('hi')\n```"
    )
    data = run_json(capsys, "--session", "s1")
    assert data["applied"] == ["brand_new.py"]


@needs_git
def test_any_file_lifts_the_restriction(repo, capsys):
    _write_session(repo, "s1", editable=["app.py"])
    (repo / ".kopipasta" / "sessions" / "s1" / "001-response.md").write_text(
        """
### ref.py

```python
<<<<
REFERENCE = 1
====
REFERENCE = 99
>>>>
```
"""
    )
    data = run_json(capsys, "--session", "s1", "--any-file")
    assert data["applied"] == ["ref.py"]


# -- resolving the target ---------------------------------------------------


@needs_git
def test_current_resolves_to_the_latest_response(repo, capsys):
    _write_session(repo, "s1", editable=["app.py"])
    d = repo / ".kopipasta" / "sessions" / "s1"
    (d / "001-response.md").write_text(NOTHING_MATCHES)
    (d / "002-response.md").write_text(CLEAN_PATCH)
    (repo / ".kopipasta" / "current").write_text("s1\n")

    data = run_json(capsys, "current")
    assert data["session"] == "s1"
    assert data["applied"] == ["app.py"]


def test_current_with_no_session_is_a_usage_error(repo, capsys):
    data = run_json(capsys, "current", expect=EXIT_USAGE)
    assert data["error"] == "usage"
    assert "ask" in data["hint"]


def test_a_missing_file_is_a_usage_error(repo, capsys):
    data = run_json(capsys, "nosuchfile.md", expect=EXIT_USAGE)
    assert data["error"] == "usage"


def test_a_response_with_no_patches_is_reported_as_such(repo, capsys):
    """Distinct from "the patch failed": nothing was even attempted, and the
    fix is to look at the answer, not at the files."""
    path = write_patch(repo, "I looked at this and found no changes were needed.")
    data = run_json(capsys, path, expect=EXIT_USAGE)
    assert data["error"] == "no_patches"


# -- the output contract, spec §8 ------------------------------------------


@needs_git
def test_json_mode_puts_exactly_one_object_on_stdout(repo, capsys):
    run(write_patch(repo, CLEAN_PATCH), "--json")
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)  # would fail on any stray narration
    assert parsed["ok"] is True
    assert "Applying" not in captured.out, "the patcher narrates to stdout"


@needs_git
def test_stdout_stays_empty_on_failure_without_json(repo, capsys):
    run(write_patch(repo, NOTHING_MATCHES), expect=EXIT_PATCH_FAILED)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "app.py" in captured.err


# -- helpers ----------------------------------------------------------------


def _write_session(repo, session_id, editable):
    d = repo / ".kopipasta" / "sessions" / session_id
    d.mkdir(parents=True)
    (d / "selection.json").write_text(
        json.dumps(
            {
                "1": {
                    "files": {
                        **{p: {"role": "edit", "hash": "x"} for p in editable},
                        "ref.py": {"role": "ref", "hash": "y"},
                    },
                    "demoted": [],
                }
            }
        )
    )
    (d / "001-response.md").write_text("")
    return d


# -- the guard protects the undo, not the tidiness of the tree --------------


@needs_git
def test_dirt_the_patch_does_not_touch_does_not_block(repo, capsys):
    """The undo is `git checkout` of the files the patch wrote.

    An uncommitted change to a file the patch never touches cannot be harmed
    by that, so refusing over it costs the caller a run and protects nothing.
    """
    (repo / "ref.py").write_text("REFERENCE = 999\n")
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH))
    assert data["ok"] is True
    assert "return 100" in (repo / "app.py").read_text()
    # ...and the bystander is left exactly as it was.
    assert (repo / "ref.py").read_text() == "REFERENCE = 999\n"


@needs_git
def test_dirt_the_patch_does_touch_still_blocks(repo, capsys):
    """The case the guard is actually for: reverting this would destroy
    uncommitted work."""
    (repo / "app.py").write_text(ORIGINAL + "# my uncommitted work\n")
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH), expect=EXIT_USAGE)
    assert data["error"] == "dirty_worktree"
    assert data["files"] == ["app.py"]
    assert "# my uncommitted work" in (repo / "app.py").read_text()


@needs_git
def test_unrelated_dirt_is_still_said_out_loud(repo, capsys):
    """Not blocking is not the same as not mentioning. Applying into a tree
    that already had changes is worth knowing about."""
    (repo / "ref.py").write_text("REFERENCE = 999\n")
    run(write_patch(repo, CLEAN_PATCH))
    err = capsys.readouterr().err
    assert "ref.py" in err
    assert "not touched by this patch" in err


@needs_git
def test_ask_then_apply_works_on_the_first_run_in_a_clean_repo(repo, capsys, tmp_path):
    """The flagship two-step, from a clean tree, with nothing done in between.

    `ask` writes `.kopipasta/` and appends it to `.gitignore` on first use —
    so the tool dirtied the worktree and then refused to apply because the
    worktree was dirty. The documented workflow failed on its own first run,
    and the suggested fix (`git stash`) would have stashed kopipasta's own
    bookkeeping.
    """
    from kopipasta.core import ask as askmod

    # The fixture pre-seeds the ignore rule; a real project has not heard of
    # kopipasta yet, and that first write is the whole point of this test.
    (repo / ".gitignore").write_text("__pycache__/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "no kopipasta rule yet")

    canned = tmp_path / "canned-patch.md"
    canned.write_text(CLEAN_PATCH)
    assert (
        askmod.run(
            [
                "--backend",
                f"none:{canned}",
                "-e",
                "app.py",
                "--mode",
                "patch",
                "-q",
                "make a() return 100",
                "--json",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()

    assert ".kopipasta/" in (repo / ".gitignore").read_text()
    data = run_json(capsys, "current")
    assert data["ok"] is True
    assert data["applied"] == ["app.py"]
    assert "return 100" in (repo / "app.py").read_text()
