"""Spec 11.1b(3)/(4): a subcommand must never fall through to the TUI.

The 3 dispatch rule ("known verb? dispatch; otherwise treat it as a path") is
itself an accidental-TUI generator. `kopipasta pak --all` is a typo, but `pak`
is not on disk, so before this change it became a skipped path and the tool
either exited 0 having silently done nothing, or — with any real path also on
the command line — opened the interactive selector.

The asymmetry these tests pin down: a wrong command line exits 1, a missing
human exits 8. Different codes because the fix differs, and an agent has to be
able to branch on that without parsing prose.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kopipasta.interaction import EXIT_NO_HUMAN
from kopipasta.main import KopipastaApp, UsageError, VerbRequested


def _parse(argv):
    """Drive the real dispatch without touching sys.argv."""
    app = KopipastaApp(argv)
    app._parse_args()
    return app


# -- bare words that are not verbs and not paths ---------------------------


@pytest.mark.parametrize("word", ["pak", "asq", "nosuchverb", "sesion"])
def test_unknown_bare_word_is_a_usage_error(word):
    with pytest.raises(UsageError, match="unknown command"):
        _parse([word])


def test_the_error_names_the_known_subcommands(monkeypatch):
    """A refusal that does not say what IS allowed just costs another round trip."""
    with pytest.raises(UsageError) as e:
        _parse(["pak"])
    for verb in ("pack", "ask", "apply"):
        assert verb in str(e.value)


def test_unknown_word_beats_a_real_path_beside_it(tmp_path, monkeypatch):
    """The dangerous case: a typo'd verb PLUS a real path opened the selector."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real.py").write_text("x = 1\n")
    with pytest.raises(UsageError):
        _parse(["pak", "real.py"])


# -- things that must still be treated as paths ----------------------------


@pytest.mark.parametrize(
    "arg",
    [
        "src/",
        "./thing",
        "../thing",
        "a/b/c.py",
        "main.py",
        "*.py",
        "~/notes",
        "https://example.com/x",
    ],
)
def test_pathlike_arguments_are_never_rejected(arg, tmp_path, monkeypatch):
    """Breaking a real invocation of a published tool is a worse bug than the
    one being fixed. Anything a user could plausibly have meant as a path must
    survive dispatch untouched."""
    monkeypatch.chdir(tmp_path)
    app = _parse([arg, "-t", "task"])
    assert app.subcommand is None
    assert app.args.inputs[0] == arg


def test_an_existing_extensionless_file_is_a_path_not_a_command(tmp_path, monkeypatch):
    """`Makefile`, `LICENSE`, `Dockerfile` are bare words AND real files."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Makefile").write_text("all:\n")
    app = _parse(["Makefile", "-t", "task"])
    assert app.subcommand is None
    assert app.args.inputs == ["Makefile"]


def test_no_arguments_still_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = _parse(["-t", "task"])
    assert app.args.inputs == ["."]
    assert app.subcommand is None


# -- the tui alias, 11.1b(4) ------------------------------------------------


def test_tui_is_consumed_as_a_subcommand(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = _parse(["tui", "-t", "task"])
    assert app.subcommand == "tui"
    # Consumed, not left behind to be misread as a filename.
    assert app.args.inputs == ["."]


def test_tui_still_forwards_its_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    app = _parse(["tui", "a.py"])
    assert app.subcommand == "tui"
    assert app.args.inputs == ["a.py"]


def test_specced_but_unimplemented_verbs_say_so(tmp_path, monkeypatch):
    """A verb that is specced and unbuilt says so. "Not yet" and "no such
    thing" send the caller to different places, and neither is a filename."""
    monkeypatch.chdir(tmp_path)
    for verb in ("pack", "patch", "apply", "map", "session"):
        with pytest.raises(UsageError, match="not implemented yet"):
            _parse([verb])


@pytest.mark.parametrize("verb", ["ask", "config"])
def test_implemented_verbs_are_dispatched_before_the_legacy_parser(verb, tmp_path, monkeypatch):
    """`ask -e file -q "..."` is not a command line the TUI's parser can be
    taught. It has to be intercepted before argparse sees it, or every verb
    becomes a usage error from the wrong parser."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(VerbRequested) as e:
        _parse([verb, "-q", "why", "--json"])
    assert e.value.verb == verb
    assert e.value.argv == ["-q", "why", "--json"]


def test_a_verb_flag_is_never_read_as_a_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    with pytest.raises(VerbRequested) as e:
        _parse(["ask", "--all", "-x", "src"])
    assert e.value.argv == ["--all", "-x", "src"]


# -- end to end: the exit codes are the contract ---------------------------


def _run(args, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env.pop("KOPIPASTA_NONINTERACTIVE", None)
    env.pop("CI", None)
    return subprocess.run(
        [sys.executable, "-m", "kopipasta.main"] + args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_typo_exits_1_without_ever_drawing_the_tui(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = _run(["pak", "-t", "task"], tmp_path)
    assert r.returncode == 1, r.stderr
    assert "unknown command" in r.stderr
    # Nothing rendered. A usage error must not cost the caller a tree dump.
    assert len(r.stdout) < 2_000, f"stdout was {len(r.stdout)} bytes"


def test_usage_error_and_missing_human_have_different_exit_codes(tmp_path):
    """Exit 1 means 'fix your command line'; exit 8 means 'there is no
    terminal'. Collapsing them would tell an agent to retry the wrong fix."""
    (tmp_path / "a.py").write_text("x = 1\n")
    assert _run(["pak", "-t", "task"], tmp_path).returncode == 1
    assert _run(["tui", "-t", "task"], tmp_path).returncode == EXIT_NO_HUMAN


def test_tui_alias_does_not_bypass_the_human_guard(tmp_path):
    """Naming the TUI explicitly is not consent to hang. This is precisely why
    'move the TUI behind a subcommand' was rejected as the primary fix: a
    script that types `kopipasta tui` must still terminate."""
    (tmp_path / "a.py").write_text("x = 1\n")
    r = _run(["tui", "-t", "task"], tmp_path)
    assert r.returncode == EXIT_NO_HUMAN
    assert "needs an interactive terminal" in r.stderr
    assert len(r.stdout) < 50_000
