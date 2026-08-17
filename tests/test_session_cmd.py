"""`kopipasta session` — reading and retiring the record. Spec §7, §8.

The sessions under test are made by running `ask` through the `none` backend,
so these exercise the real files a real run writes rather than fixtures shaped
like them. Two behaviours here are worth more than the rest:

- **`rm` hands the provider cache back before deleting the directory.** The
  resource name lives only in that directory, so the other order leaves a
  rented cache with nothing on disk to say what is being paid for.
- **`diff` compares recorded hashes against the files on disk.** It is the
  only thing in the toolchain that can say "the answer you are holding is
  about a version of this file that no longer exists".
"""

import json
import os
from pathlib import Path

import pytest

from kopipasta.core import ask as askmod
from kopipasta.core import session_cmd
from kopipasta.core.errors import EXIT_OK, EXIT_USAGE
from kopipasta.core.session import CACHE_FILE, Session, list_sessions, session_dir


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "src" / "main.py").write_text("def main():\n    pass\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    for var in (
        "KOPIPASTA_BACKEND",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    from kopipasta import file as filemod

    filemod._is_ignored_cache.clear()
    filemod._is_binary_cache.clear()
    filemod._gitignore_cache.clear()
    return tmp_path


def ask(*argv):
    assert askmod.run(["--backend", "none", *argv]) == EXIT_OK


def run(*argv, expect=EXIT_OK):
    code = session_cmd.run(list(argv))
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


def run_json(capsys, *argv, expect=EXIT_OK):
    capsys.readouterr()  # discard whatever the fixture's own `ask` runs printed
    run(*argv, "--json", expect=expect)
    return json.loads(capsys.readouterr().out)


def key(project):
    """The project label a real cache's display name carries."""
    from kopipasta.cache import get_project_key

    return get_project_key(str(project))


def lease(project, session_id, **over):
    """Write the cache handle a real Gemini turn would have left behind."""
    import time

    s_dir = Path(session_dir(str(project), session_id))
    s_dir.mkdir(parents=True, exist_ok=True)
    path = s_dir / CACHE_FILE
    path.write_text(
        json.dumps(
            {
                "provider": "gemini",
                "model": "gemini-3.7-flash",
                "digest": "d" * 32,
                "name": f"cachedContents/{session_id}",
                "tokens": 16329,
                "ttl_s": 300,
                "expires_at": time.time() + 300,
                **over,
            }
        )
    )


# -- ls ---------------------------------------------------------------------


def test_no_sessions_is_not_an_error(project, capsys):
    """A fresh repo has no conversations. That is the normal state, and an
    agent that treats a non-zero exit as a failure must not see one."""
    run("ls")
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "no sessions" in captured.err


def test_ls_reports_turns_usage_and_which_one_is_current(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    ask("-e", "src/calc.py", "--session", "two", "-q", "b")
    data = run_json(capsys, "ls")
    assert [s["id"] for s in data["sessions"]] == ["one", "two"]
    assert data["current"] == "two"
    assert [s["current"] for s in data["sessions"]] == [False, True]
    assert all(s["turns"] == 1 for s in data["sessions"])


def test_ls_shows_what_a_session_is_still_renting(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    lease(project, "one")
    data = run_json(capsys, "ls")
    cache = data["sessions"][0]["cache"]
    assert cache["tokens"] == 16329 and cache["expired"] is False


# -- show -------------------------------------------------------------------


def test_show_follows_current_and_reports_pointers_not_payloads(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "why is addition wrong")
    data = run_json(capsys, "show")
    assert data["id"] == "one"
    turn = data["turns"][0]
    assert turn["question"].startswith("why is addition wrong")
    assert turn["request"] == f"{data['path']}/001-request.md"
    assert turn["response"] == f"{data['path']}/001-response.md"
    # The payload stays in the file. Putting a 4,000-token answer in a listing
    # would put it back into the context this tool exists to protect.
    assert len(turn["answer"]) <= session_cmd.HEAD_CHARS + 1


def test_show_reports_the_context_the_next_turn_would_inherit(project, capsys):
    ask("-e", "src/calc.py", "-m", "src/main.py", "--session", "one", "-q", "a")
    data = run_json(capsys, "show", "one")
    assert data["context"] == {"turn": 1, "files": 2, "roles": {"edit": 1, "map": 1}}


def test_an_unknown_id_names_the_ones_that_exist(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    data = run_json(capsys, "show", "nope", expect=EXIT_USAGE)
    assert data["error"] == "usage"
    assert "one" in data["detail"]


def test_current_pointing_nowhere_is_a_usage_error_not_a_crash(project, capsys):
    data = run_json(capsys, "show", expect=EXIT_USAGE)
    assert "no 'current' session" in data["summary"]


# -- diff -------------------------------------------------------------------


def test_diff_finds_the_files_that_moved_under_the_session(project, capsys):
    ask("-e", "src/calc.py", "-m", "src/main.py", "--session", "one", "-q", "a")
    (project / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (project / "src" / "main.py").unlink()
    data = run_json(capsys, "diff", "one")
    assert data["fresh"] == 0
    assert sorted((s["path"], s["state"]) for s in data["stale"]) == [
        ("src/calc.py", "changed"),
        ("src/main.py", "gone"),
    ]


def test_diff_on_an_untouched_tree_says_so(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    data = run_json(capsys, "diff", "one")
    assert data["stale"] == [] and data["fresh"] == 1


def test_a_stale_session_is_reported_not_refused(project, capsys):
    """Reported, never fatal: staleness is information the caller acts on, and
    there is no exit code in spec §8 that means it."""
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    (project / "src" / "calc.py").write_text("changed\n")
    run("diff", "one")
    assert "have moved since turn 1" in capsys.readouterr().err


# -- rm ---------------------------------------------------------------------


def test_rm_never_guesses_its_target(project, capsys):
    """A destructive verb that defaults to a racy pointer is a footgun."""
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    data = run_json(capsys, "rm", expect=EXIT_USAGE)
    assert "needs an id, or --all" in data["summary"]
    assert Path(session_dir(str(project), "one")).is_dir()


def test_rm_deletes_the_session_and_clears_a_dangling_current(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    assert Session.read_current(str(project)) == "one"
    s_dir = Path(session_dir(str(project), "one"))
    data = run_json(capsys, "rm", "one")
    assert data["removed"] == ["one"] and data["current_cleared"] is True
    assert not s_dir.exists()
    assert Session.read_current(str(project)) is None


def test_rm_hands_the_rented_cache_back_before_deleting_the_record(
    project, capsys, monkeypatch
):
    """The name lives only in the directory being deleted, so the other order
    leaves a meter running with nothing on disk to say what it is for."""
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    lease(project, "one")
    released = []
    monkeypatch.setattr(
        "kopipasta.core.backend.release_lease",
        lambda rec, **kw: released.append(rec["name"]) is None,
    )
    data = run_json(capsys, "rm", "one")
    assert released == ["cachedContents/one"]
    assert data["released"][0]["tokens"] == 16329


def test_rm_does_not_call_the_provider_about_an_expired_lease(
    project, capsys, monkeypatch
):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    lease(project, "one", expires_at=0)
    monkeypatch.setattr(
        "kopipasta.core.backend.release_lease",
        lambda rec, **kw: pytest.fail("expired leases cost nothing to abandon"),
    )
    data = run_json(capsys, "rm", "one")
    assert data["released"] == [] and data["removed"] == ["one"]


def test_rm_all_takes_every_session(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    ask("-e", "src/calc.py", "--session", "two", "-q", "b")
    data = run_json(capsys, "rm", "--all")
    assert sorted(data["removed"]) == ["one", "two"]
    assert list_sessions(str(project)) == []


def test_an_id_that_is_a_path_is_refused(project, capsys):
    """`rm` deletes a tree, so the id whitelist is what stops `rm ../..`."""
    for bad in ("../../etc", "..", "C:sessions", "a/b"):
        data = run_json(capsys, "rm", bad, expect=EXIT_USAGE)
        assert data["error"] == "usage"


# -- reap -------------------------------------------------------------------


def test_reap_keeps_what_a_session_is_renting(project, capsys, monkeypatch):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    lease(project, "one")
    mine = key(project)
    monkeypatch.setattr(
        "kopipasta.core.backend.GeminiBackend.list_caches",
        classmethod(
            lambda cls, base_url=None: [
                {"name": "cachedContents/one", "displayName": f"kopipasta-{mine}-aaaa"},
                {
                    "name": "cachedContents/dead",
                    "displayName": f"kopipasta-{mine}-bbbb",
                },
                {
                    "name": "cachedContents/other",
                    "displayName": "kopipasta-elsewhere-cccc",
                },
            ]
        ),
    )
    swept = {}
    monkeypatch.setattr(
        "kopipasta.core.backend.GeminiBackend.reap_orphans",
        classmethod(
            lambda cls, base_url=None, *, keep=(), label=None: (
                swept.update(keep=list(keep), label=label) or 1
            )
        ),
    )
    data = run_json(capsys, "reap")
    assert data["held_by_sessions"] == [
        {"session": "one", "expires_in_s": data["held_by_sessions"][0]["expires_in_s"]}
    ]
    # The live lease is named in `keep`, and the sweep is scoped to this project.
    assert swept["keep"] == ["cachedContents/one"]
    assert swept["label"]


def test_no_invocation_of_reap_can_sweep_another_project(project, capsys, monkeypatch):
    """Found by pointing the oracle at this file an hour after writing it.

    `reap` once took an --all-projects flag. A lease lives in the project that
    took it, so that sweep read *this* project's leases and passed them as the
    keep-list for a machine-wide delete — destroying a cache another repo was
    holding mid-conversation. That is the money bug this whole function exists
    to prevent, one scope up, and the flag could not be made safe: nothing on
    this machine knows what another project has leased.

    So the guard is on the primitive, not the flag. `label=None` is the
    machine-wide sweep, and no command-line path may produce it.
    """
    monkeypatch.setattr(
        "kopipasta.core.backend.GeminiBackend.list_caches",
        classmethod(lambda cls, base_url=None: []),
    )
    seen = []
    monkeypatch.setattr(
        "kopipasta.core.backend.GeminiBackend.reap_orphans",
        classmethod(
            lambda cls, base_url=None, *, keep=(), label=None: seen.append(label) or 0
        ),
    )
    run_json(capsys, "reap")
    assert seen == [key(project)], "the sweep must always carry this project's label"

    parser_flags = session_cmd.build_parser().format_help() + _reap_help()
    assert "--all-projects" not in parser_flags


def _reap_help() -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        session_cmd.build_parser().parse_args(["reap", "--help"])
    return buf.getvalue()


def test_reap_dry_run_deletes_nothing(project, capsys, monkeypatch):
    monkeypatch.setattr(
        "kopipasta.core.backend.GeminiBackend.list_caches",
        classmethod(
            lambda cls, base_url=None: [
                {
                    "name": "cachedContents/dead",
                    "displayName": f"kopipasta-{key(project)}-b",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "kopipasta.core.backend.GeminiBackend.reap_orphans",
        classmethod(lambda cls, **kw: pytest.fail("--dry-run must not delete")),
    )
    data = run_json(capsys, "reap", "--dry-run")
    assert data["reaped"] == 0 and data["would_reap"] == 1


# -- the output contract, spec §8 ------------------------------------------


def test_a_bare_session_command_says_what_the_subcommands_are(project, capsys):
    data = run_json(capsys, expect=EXIT_USAGE)
    assert "subcommand" in data["summary"]
    assert "session ls" in data["hint"]


def test_json_is_a_single_object_on_stdout(project, capsys):
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    capsys.readouterr()
    run("ls", "--json")
    captured = capsys.readouterr()
    json.loads(captured.out)  # parses whole; narration never lands mid-object
    assert "kopipasta:" not in captured.out


# -- relocatable state directory -------------------------------------------


def test_ls_and_show_report_relative_path_when_inside_project(project, capsys):
    """When state is within the project worktree, path is relative with forward slashes."""
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")
    data_ls = run_json(capsys, "ls")
    path_ls = data_ls["sessions"][0]["path"]
    assert not os.path.isabs(path_ls)
    assert "\\" not in path_ls
    assert path_ls.endswith("sessions/one")

    data_show = run_json(capsys, "show", "one")
    assert data_show["path"] == path_ls
    assert data_show["turns"][0]["request"] == f"{path_ls}/001-request.md"


def test_ls_and_show_fallback_to_absolute_path_on_relpath_failure(
    project, capsys, monkeypatch
):
    """Cross-drive or invalid relpath falls back cleanly to the absolute path."""
    ask("-e", "src/calc.py", "--session", "one", "-q", "a")

    def broken_relpath(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", broken_relpath)

    data_ls = run_json(capsys, "ls")
    path_ls = data_ls["sessions"][0]["path"]
    assert os.path.isabs(path_ls)
    assert path_ls.endswith("sessions/one")

    data_show = run_json(capsys, "show", "one")
    assert data_show["path"] == path_ls
    assert os.path.isabs(data_show["turns"][0]["request"])


def test_ls_and_show_with_relocated_state_dir(project, capsys, monkeypatch):
    """State relocated to .git/kopipasta still works for ls and show."""
    monkeypatch.setenv("KOPIPASTA_STATE_DIR", "git")
    ask("-e", "src/calc.py", "--session", "relocated", "-q", "hello")
    data_ls = run_json(capsys, "ls")
    assert len(data_ls["sessions"]) == 1
    session = data_ls["sessions"][0]
    assert session["id"] == "relocated"
    assert ".git" in session["path"]

    data_show = run_json(capsys, "show", "relocated")
    assert data_show["id"] == "relocated"
    assert data_show["path"] == session["path"]
    assert data_show["turns"][0]["request"].startswith(session["path"])


def test_no_sessions_message_names_consulted_directory(project, capsys, monkeypatch):
    """The empty-state message tells the user exactly which directory was checked."""
    monkeypatch.setenv("KOPIPASTA_STATE_DIR", "git")
    run("ls")
    err = capsys.readouterr().err
    assert "no sessions in .git/kopipasta/sessions/." in err


def test_ls_lists_legacy_and_default_sessions_without_duplicates(project, capsys):
    """`session ls` lists sessions from legacy .kopipasta/ and default without duplicates."""
    ask("-e", "src/calc.py", "--session", "new-sess", "-q", "a")
    ask(
        "--state-dir",
        "repo",
        "-e",
        "src/calc.py",
        "--session",
        "legacy-sess",
        "-q",
        "b",
    )
    # Duplicate directory in legacy location to verify deduplication
    legacy_dup = project / ".kopipasta" / "sessions" / "new-sess"
    legacy_dup.mkdir(parents=True, exist_ok=True)

    data = run_json(capsys, "ls")
    session_ids = [s["id"] for s in data["sessions"]]
    assert session_ids == ["legacy-sess", "new-sess"]
    assert len(session_ids) == len(set(session_ids))


def test_show_works_for_legacy_session(project, capsys):
    """`session show <id>` displays details for a session in the legacy location."""
    ask(
        "--state-dir",
        "repo",
        "-e",
        "src/calc.py",
        "--session",
        "legacy-one",
        "-q",
        "why is calc needed",
    )
    data = run_json(capsys, "show", "legacy-one")
    assert data["id"] == "legacy-one"
    assert data["path"] == ".kopipasta/sessions/legacy-one"
    turn = data["turns"][0]
    assert turn["question"].startswith("why is calc needed")
    assert turn["request"] == ".kopipasta/sessions/legacy-one/001-request.md"
    assert turn["response"] == ".kopipasta/sessions/legacy-one/001-response.md"
