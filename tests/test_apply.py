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
import os
import shutil
import subprocess
import sys

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
def test_both_verbs_count_patches_with_the_same_two_names(repo, capsys):
    """`ask` proposes and `apply` applies, and the envelope has to let a
    caller tell those apart without knowing which verb it came from.

    A bare `"patches": N` from `ask` was read as "N patches applied" while the
    worktree was untouched. Two fields of the same type, spelled the same way
    in both verbs, is the whole fix.
    """
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH))
    assert data["patches_proposed"] == 1
    assert data["patches_applied"] == 1
    assert "patches" not in data


@needs_git
def test_a_partial_apply_counts_no_file_as_applied(repo, capsys):
    """`patches_applied` is a count of files that landed whole. A file with
    one of two hunks in it has not landed."""
    data = run_json(
        capsys, write_patch(repo, HALF_MATCHING_PATCH), expect=EXIT_PATCH_PARTIAL
    )
    assert data["patches_proposed"] == 1
    assert data["patches_applied"] == 0
    assert data["partial"] == ["app.py"]


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


# -- the verify reader must never lose the diagnostics ---------------------


HEAVY = "\u2716 eslint\n\u23af\u23af\u23af vitest\n"


def _emit_utf8(text: str) -> str:
    """A shell command that writes `text` as raw utf-8 bytes.

    Not `print()`: a child Python's stdout is the locale encoding when piped,
    so on Windows it would die with UnicodeEncodeError before we ever got to
    test the *decoding* side. Bytes on the wire is what a real `eslint` or
    `vitest` puts there.
    """
    payload = repr(text.encode("utf-8"))
    return (
        f"{sys.executable} -c "
        f'"import sys; sys.stdout.buffer.write({payload}); sys.stdout.flush()"'
    )


@needs_git
def test_verify_output_survives_bytes_the_platform_codepage_cannot_decode(repo, capsys):
    """Field report 2.1, the blocker.

    `text=True` alone decodes with `locale.getpreferredencoding()` — cp1252 on
    a Windows box. Every verify command whose output contains a box-drawing
    rule or a heavy multiplication X killed the reader thread, and the
    envelope came back `{"exit": 1, "output": ""}`: a failure reported with
    zero diagnostics, at exactly the moment the diagnostics matter. Failing
    output is the output most likely to contain such a character.
    """
    data = run_json(
        capsys, write_patch(repo, CLEAN_PATCH), "--verify", _emit_utf8(HEAVY)
    )
    assert data["verify"]["exit"] == 0
    assert "\u2716 eslint" in data["verify"]["output"]
    assert "\u23af\u23af\u23af vitest" in data["verify"]["output"]


@needs_git
def test_a_failing_verify_still_carries_its_unicode_diagnostics(repo, capsys):
    """The case that actually cost the field run: the output is lost precisely
    when the exit code is non-zero and someone needs to read it."""
    cmd = _emit_utf8(HEAVY) + " && exit 1"
    data = run_json(
        capsys, write_patch(repo, CLEAN_PATCH), "--verify", cmd, expect=EXIT_VERIFY
    )
    assert data["verify"]["exit"] == 1
    assert "\u2716 eslint" in data["verify"]["output"]
    assert data["detail"], "a verify failure with no detail is the worst outcome"


@needs_git
def test_a_verify_command_that_prints_an_undecodable_byte_is_not_fatal(repo, capsys):
    """Not every stream is valid utf-8 either — a lone 0x9d happens. Replace
    it; there is no case where crashing beats a replacement character."""
    cmd = (
        f"{sys.executable} -c "
        f"\"import sys; sys.stdout.buffer.write(b'before\\x9dafter')\""
    )
    data = run_json(capsys, write_patch(repo, CLEAN_PATCH), "--verify", cmd)
    assert data["verify"]["exit"] == 0
    assert "before" in data["verify"]["output"]
    assert "after" in data["verify"]["output"]


# -- --format-cmd: the formatter runs before the gate does -----------------


def _rewrite(text: str) -> str:
    """A stand-in formatter: rewrites whatever files it is handed."""
    payload = repr(text)
    return (
        f"{sys.executable} -c "
        f"\"import sys; [open(p, 'w').write({payload}) for p in sys.argv[1:]]\" {{files}}"
    )


@needs_git
def test_format_cmd_runs_between_applying_and_verifying(repo, capsys):
    """Field report 2.3.

    All four patch turns in the field run failed `prettier --check`, in a repo
    whose gate opens with `prettier --check .`. That is a guaranteed red on
    turn 1, every time, over whitespace — and the model cannot be prompted out
    of it reliably. The reported workaround was to fold `prettier --write`
    into the verify command, which works but makes the *verifier* mutate the
    tree; that is the one thing a verifier must not do.
    """
    formatted = "def a():\n    return 100  # formatted\n\n\ndef b():\n    return 2\n"
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--format-cmd",
        _rewrite(formatted),
        "--verify",
        "exit 0",
    )
    assert data["format"]["exit"] == 0
    assert (repo / "app.py").read_text(encoding="utf-8") == formatted


@needs_git
def test_format_cmd_only_ever_sees_the_files_this_run_wrote(repo, capsys):
    """`{files}` is the whole reason this is a flag and not a documented wrap.

    `prettier --write .` after a patch reformats the caller's unrelated
    uncommitted work, and --revert-on-fail would then put back only the files
    the patch touched — leaving the rest reformatted with nothing recording
    that it happened.
    """
    (repo / "ref.py").write_text("REFERENCE = 999\n", encoding="utf-8")
    run(
        write_patch(repo, CLEAN_PATCH),
        "--format-cmd",
        _rewrite("CLOBBERED\n"),
        "--json",
    )
    data = json.loads(capsys.readouterr().out)
    assert data["format"]["files"] == ["app.py"]
    assert (repo / "ref.py").read_text(encoding="utf-8") == "REFERENCE = 999\n"


@needs_git
def test_a_dry_run_never_reaches_the_formatter(repo, capsys):
    """It writes; --dry-run promises nothing is written."""
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--dry-run",
        "--format-cmd",
        _rewrite("CLOBBERED\n"),
    )
    assert "format" not in data
    assert (repo / "app.py").read_text(encoding="utf-8") == ORIGINAL


@needs_git
def test_a_failing_formatter_is_reported_and_does_not_replace_the_verdict(repo, capsys):
    """A formatter that cannot run is worth knowing about, but it is not the
    gate. Exit 7 has to keep meaning "--verify failed", or the caller cannot
    tell which of the two commands it needs to go and look at."""
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--format-cmd",
        "exit 2",
        "--verify",
        "exit 0",
    )
    assert data["ok"] is True
    assert data["format"]["exit"] == 2


@needs_git
def test_a_failing_formatter_says_so_out_loud(repo, capsys):
    run(
        write_patch(repo, CLEAN_PATCH),
        "--format-cmd",
        "exit 2",
    )
    assert "--format-cmd" in capsys.readouterr().err


@needs_git
def test_format_cmd_without_files_to_format_is_skipped(repo, capsys):
    """Nothing applied means nothing to format, and `prettier --write` with an
    empty file list formats the entire repository."""
    data = run_json(
        capsys,
        write_patch(repo, NOTHING_MATCHES),
        "--format-cmd",
        _rewrite("CLOBBERED\n"),
        expect=EXIT_PATCH_FAILED,
    )
    assert "format" not in data
    assert (repo / "app.py").read_text(encoding="utf-8") == ORIGINAL


@needs_git
def test_a_path_the_model_invented_cannot_become_a_shell_command(repo, capsys):
    """`{files}` is spliced into a `shell=True` command line, so the paths in
    it are attacker-controlled in the only sense that matters here: they come
    from the model's response, not from the caller.

    On POSIX `shlex.quote` is exactly right. On Windows there is no correct
    escape — `cmd.exe` expands `%VAR%` and honours `&` inside quotes it has
    already stripped — so a path carrying a metacharacter is refused rather
    than escaped, and the patch is left applied and unformatted.
    """
    marker = repo / "pwned.txt"
    hostile = "a & b.py" if os.name == "nt" else "a; touch pwned.txt; x.py"
    patch = write_patch(repo, f"```python\n# FILE: {hostile}\nx = 1\n```")
    data = run_json(capsys, patch, "--format-cmd", "echo {files}")

    assert data["applied"] == [hostile], "the patch itself should still land"
    assert not marker.exists(), "the formatter ran a command out of a filename"
    if os.name == "nt":
        assert data["format"]["exit"] == 126
        assert "refused" in data["format"]["output"]
    else:
        # shlex.quote is genuinely correct here, so it runs, harmlessly.
        assert data["format"]["exit"] == 0


def test_the_windows_refusal_knows_which_characters_it_cannot_survive():
    """Pinned separately so the rule is checked on every platform, not only
    on the one where it fires."""
    for ch in "&|<>^%":
        assert ch in applymod.CMD_METACHARACTERS
    for ch in "-_.()[]{}~# ":
        assert ch not in applymod.CMD_METACHARACTERS, (
            "ordinary characters in ordinary filenames must not be refused"
        )


@needs_git
def test_the_formatters_output_survives_a_unicode_console(repo, capsys):
    """Same reader, same bug class as --verify. It gets the same fix."""
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--format-cmd",
        _emit_utf8(HEAVY),
    )
    assert "\u2716 eslint" in data["format"]["output"]


# -- the hint must describe what happened, not what was asked for ----------


@needs_git
def test_the_hint_says_restored_when_it_did_restore(repo, capsys):
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["app.py"]
    assert "Restored" in data["hint"]
    assert "app.py" in data["hint"]
    assert "declined" not in data["hint"]


@needs_git
def test_without_revert_on_fail_the_hint_says_the_patch_is_still_there(repo, capsys):
    data = run_json(
        capsys, write_patch(repo, CLEAN_PATCH), "--verify", "exit 1", expect=EXIT_VERIFY
    )
    assert "still applied" in data["hint"]
    assert "estored" not in data["hint"]


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


@needs_git
def test_revert_restores_uncommitted_work_to_the_callers_version(repo, capsys):
    """The snapshot restores the caller's pre-patch bytes, not HEAD.

    Under git checkout, reverting a dirty file would have blown away the
    caller's uncommitted work to clean up after a failed verify. The snapshot
    records the caller's uncommitted bytes and restores them exactly, so
    --revert-on-fail puts the caller's version back instead of declining or
    resetting to HEAD.
    """
    caller_version = ORIGINAL + "# uncommitted caller work\n"
    (repo / "app.py").write_text(caller_version)
    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["app.py"]
    assert data["revert_declined"] == []
    assert (repo / "app.py").read_text() == caller_version


@needs_git
def test_revert_matches_dotted_paths_against_snapshot_keys(repo, capsys):
    """A patch declaring `./app.py` must find the snapshot keyed `app.py`.

    Paths from the model may carry leading `./` prefixes. Both sides are
    normalised so the snapshot entry is resolved and the file is restored
    rather than declined with RESTORE_FAILED.
    """
    dotted = CLEAN_PATCH.replace("### app.py", "### ./app.py")
    data = run_json(
        capsys,
        write_patch(repo, dotted),
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["./app.py"]
    assert data["revert_declined"] == []
    assert (repo / "app.py").read_text() == ORIGINAL


@needs_git
def test_revert_on_fail_restores_untracked_pre_existing_file(repo, capsys):
    """The field failure that motivated byte-level snapshots.

    An untracked pre-existing file cannot be restored by `git checkout -- <path>`
    because git does not track it. Under git-based revert it was recorded as
    GIT_REFUSED and left patched. The byte snapshot restores it cleanly.
    """
    untracked_content = "def helper():\n    return 'original untracked'\n"
    (repo / "helper.py").write_text(untracked_content)

    patch = write_patch(
        repo,
        """
### helper.py

```python
<<<<
def helper():
    return 'original untracked'
====
def helper():
    return 'modified untracked'
>>>>
```
""",
    )
    data = run_json(
        capsys,
        patch,
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["helper.py"]
    assert data["revert_declined"] == []
    assert (repo / "helper.py").read_text() == untracked_content


def test_revert_on_fail_works_outside_git_repository(tmp_path, capsys, monkeypatch):
    """Without a git repository, snapshot revert still restores touched files."""
    work = tmp_path / "plain_dir"
    work.mkdir()
    (work / "app.py").write_text(ORIGINAL)
    monkeypatch.chdir(work)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")

    patch_file = tmp_path / "patch.md"
    patch_file.write_text(CLEAN_PATCH)

    data = run_json(
        capsys,
        str(patch_file),
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["app.py"]
    assert data["revert_declined"] == []
    assert (work / "app.py").read_text() == ORIGINAL


@needs_git
def test_revert_preserves_crlf_line_endings_byte_for_byte(repo, capsys):
    """The snapshot is binary, so an undo cannot silently normalise newlines.

    Worth pinning on its own: every file in a Windows checkout with
    `core.autocrlf=true` is CRLF on disk, so a revert that wrote back LF would
    show the entire file as modified in `git diff` after an undo that was
    supposed to leave no trace. Reading and writing text with the default
    newline handling would do exactly that; bytes are what prevent it.
    """
    raw_bytes = b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n"
    (repo / "app.py").write_bytes(raw_bytes)

    patch = write_patch(
        repo,
        """
### app.py

```python
<<<<
def b():
    return 2
====
def b():
    return 200
>>>>
```
""",
    )
    data = run_json(
        capsys,
        patch,
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["app.py"]
    assert data["revert_declined"] == []
    assert (repo / "app.py").read_bytes() == raw_bytes


@needs_git
def test_revert_on_fail_restores_file_deleted_by_patch(repo, capsys):
    """A file deleted by --allow-delete has its original bytes restored on fail."""
    (repo / "doomed.py").write_text("goodbye world\n")
    patch = write_patch(repo, "### doomed.py\n\n```python\n<<<DELETE>>>\n```\n")
    data = run_json(
        capsys,
        patch,
        "--allow-delete",
        "--dirty-ok",
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == ["doomed.py"]
    assert data["revert_declined"] == []
    assert (repo / "doomed.py").read_text() == "goodbye world\n"


@needs_git
def test_the_hint_does_not_claim_a_restoration_that_was_declined(
    repo, capsys, monkeypatch
):
    """Field report 2.2.

    `hint` was an unconditional string keyed off the *flag*, not off the
    *outcome*. Declining is correct behaviour when a restore fails, but anyone
    reading the console rather than parsing the JSON walked away believing the
    tree had been restored when it had not — and took their next action
    against a state they had mismodelled. A tool that reports a restoration it
    did not perform is the one failure mode running it again cannot catch.
    """
    real_open = open

    def fail_wb(file, mode="r", *args, **kwargs):
        if "wb" in mode and str(file).endswith("app.py"):
            raise OSError("simulated restore failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_wb)

    data = run_json(
        capsys,
        write_patch(repo, CLEAN_PATCH),
        "--verify",
        "exit 1",
        "--revert-on-fail",
        expect=EXIT_VERIFY,
    )
    assert data["reverted"] == []
    assert data["revert_declined"] == ["app.py"]
    assert data["revert_declined_why"]["app.py"] == applymod.RESTORE_FAILED
    assert "have been restored" not in data["hint"]
    assert "Restored" not in data["hint"]
    assert "declined" in data["hint"]
    assert "app.py" in data["hint"] or "1 file" in data["hint"]
    assert "still applied" in data["hint"]


@needs_git
def test_a_file_that_is_not_valid_utf8_is_patched_not_refused(repo, capsys):
    """One legacy byte must not make a file unpatchable.

    This used to fail the whole file with `'utf-8' codec can't decode byte
    0xff`, so a cp1252 em-dash or a latin-1 accent left anywhere in a module
    put it permanently out of reach. Tolerance belongs in the parser: the
    read and write are paired through `surrogateescape`, so bytes that are not
    valid UTF-8 decode to lone surrogates and re-encode to exactly themselves.

    The two halves are asserted separately on purpose. That the patch applied
    is the fix; that the byte survived is what makes the fix safe. A cp1252 or
    latin-1 fallback would also have "worked" here while writing back
    different bytes, which is silent corruption and worse than the crash.
    """
    raw_bytes = b"def a():\n    return '\xff'\n\n\ndef b():\n    return 2\n"
    (repo / "app.py").write_bytes(raw_bytes)

    patch = write_patch(
        repo,
        "### app.py\n\n```python\n<<<<\ndef b():\n    return 2\n"
        "====\ndef b():\n    return 200\n>>>>\n```\n",
    )
    data = run_json(capsys, patch, "--dirty-ok")

    assert data["applied"] == ["app.py"]
    after = (repo / "app.py").read_bytes()
    assert b"return 200" in after, "the region the patch names must be updated"
    assert b"return '\xff'" in after, (
        "the byte it could not decode must survive the round trip untouched"
    )
