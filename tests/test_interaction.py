"""Guards against the worst failure mode for a tool an agent shells out to:
a prompt with nobody there to answer it.

Before the guard, `kopipasta . -t task < /dev/null` rendered the whole tree
into the pipe, failed on /dev/tty, and then spun: ~450 redraws/sec, 5.5 MB of
output in 10 seconds, forever. click.pause is a no-op without a tty, so the
"recover and continue" path had nothing throttling it.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kopipasta.interaction import (
    EXIT_NO_HUMAN,
    NoHumanAttached,
    human_attached,
    require_human,
)


class _FakeStream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_human_attached_false_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("KOPIPASTA_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStream(False))
    monkeypatch.setattr(sys, "stdout", _FakeStream(True))
    assert human_attached() is False


def test_human_attached_true_only_when_both_streams_are_ttys(monkeypatch):
    monkeypatch.delenv("KOPIPASTA_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStream(True))
    monkeypatch.setattr(sys, "stdout", _FakeStream(True))
    assert human_attached() is True


@pytest.mark.parametrize("var", ["KOPIPASTA_NONINTERACTIVE", "CI"])
def test_env_override_wins_over_a_real_tty(monkeypatch, var):
    """A harness may allocate a pty; the env var must still force refusal."""
    monkeypatch.delenv("KOPIPASTA_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStream(True))
    monkeypatch.setattr(sys, "stdout", _FakeStream(True))
    monkeypatch.setenv(var, "1")
    assert human_attached() is False


def test_closed_streams_are_treated_as_no_human(monkeypatch):
    monkeypatch.delenv("KOPIPASTA_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("CI", raising=False)

    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", Closed())
    monkeypatch.setattr(sys, "stdout", Closed())
    assert human_attached() is False


def test_require_human_raises_with_the_hint(monkeypatch):
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    with pytest.raises(NoHumanAttached) as exc:
        require_human("Interactive file selection", "Set FOO=1.")
    assert "Interactive file selection" in str(exc.value)
    assert "Set FOO=1." in str(exc.value)


def test_selector_refuses_before_drawing_anything(monkeypatch, tmp_path):
    """The guard must fire before the first render, not after."""
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    from kopipasta.tree_selector import TreeSelector

    selector = TreeSelector([], str(tmp_path))
    selector.console = MagicMock()

    with patch.object(selector, "build_tree") as build_tree:
        with pytest.raises(NoHumanAttached):
            selector.run([str(tmp_path)])

    build_tree.assert_not_called()
    selector.console.print.assert_not_called()


def test_selector_loop_aborts_instead_of_spinning(monkeypatch, tmp_path):
    """An OSError from getchar must terminate, not loop forever.

    This is the exact shape of the original bug: no /dev/tty -> OSError ->
    caught -> redraw -> OSError -> ...
    """
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    from kopipasta import tree_selector as ts

    selector = ts.TreeSelector([], str(tmp_path))
    selector.console = MagicMock()

    calls = {"n": 0}

    def exploding_getchar():
        calls["n"] += 1
        raise OSError(6, "No such device or address", "/dev/tty")

    monkeypatch.setattr(ts.click, "getchar", exploding_getchar)
    monkeypatch.setattr(ts, "require_human", lambda *a, **k: None)  # past the gate

    with pytest.raises(NoHumanAttached):
        selector.run([str(tmp_path)])

    assert calls["n"] == 1, f"getchar retried {calls['n']}x; it must not loop"


def test_selector_loop_gives_up_after_repeated_failures(monkeypatch, tmp_path):
    """Non-OSError failures get a few retries, then a hard stop."""
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    from kopipasta import tree_selector as ts

    selector = ts.TreeSelector([], str(tmp_path))
    selector.console = MagicMock()

    calls = {"n": 0}

    def exploding_getchar():
        calls["n"] += 1
        raise ValueError("something recurring")

    monkeypatch.setattr(ts.click, "getchar", exploding_getchar)
    monkeypatch.setattr(ts.click, "pause", lambda *a, **k: None)
    monkeypatch.setattr(ts, "require_human", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="consecutive failures"):
        selector.run([str(tmp_path)])

    assert calls["n"] == 3, f"expected 3 attempts before abort, got {calls['n']}"


def test_end_to_end_piped_invocation_exits_fast_and_quietly(tmp_path):
    """The regression test that matters: pipe into kopipasta, get a bounded,
    fast, non-zero exit rather than an unbounded spin."""
    (tmp_path / "a.py").write_text("print('x')\n")
    repo_root = Path(__file__).resolve().parent.parent

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    env.pop("KOPIPASTA_NONINTERACTIVE", None)
    env.pop("CI", None)

    result = subprocess.run(
        [sys.executable, "-m", "kopipasta.main", "-t", "task", "."],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == EXIT_NO_HUMAN, (
        f"expected exit {EXIT_NO_HUMAN}, got {result.returncode}"
    )
    # Before the fix this was ~5.5 MB in 10s and climbing.
    assert len(result.stdout) < 50_000, f"stdout was {len(result.stdout)} bytes"
    assert "needs an interactive terminal" in result.stderr
