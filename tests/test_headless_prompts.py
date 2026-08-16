"""Spec 11.2: every remaining blocking prompt must handle "nobody is there".

The selector loop was the loud instance of this bug; it was not the only one.
These are the other blocking reads in the package, and the point of the tests
is that the *policy differs by question*:

  - No safe default  -> refuse, exit 8, name the flag that avoids the question.
    ("Which files?", "What is the task?", "Full page or snippet?")
  - Safe default     -> apply it, narrate on stderr, keep running.
    ("How should I handle this detected secret?" -> mask it.)

Failing fast on everything would be easy and wrong: it would make kopipasta
unusable in CI the moment a .env existed, buying no safety at all, since
masking already leaks nothing.
"""

import io

import pytest

from kopipasta.interaction import NoHumanAttached, use_default_without_human
from kopipasta.main import KopipastaApp
from kopipasta.prompt import handle_env_variables


# These fixtures pin the guard's VERDICT rather than faking ttys. Faking the
# streams fights capsys, which replaces sys.stdout after the fixture runs and
# silently turns every "human present" test into a headless one. The tty
# detection itself is covered in test_interaction.py; what is under test here
# is the policy each call site applies once the verdict is known.
@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr("kopipasta.interaction.human_attached", lambda: False)


@pytest.fixture
def with_human(monkeypatch):
    monkeypatch.setattr("kopipasta.interaction.human_attached", lambda: True)


# -- the helper itself ------------------------------------------------------


def test_default_helper_is_a_no_op_when_a_human_is_present(with_human, capsys):
    assert use_default_without_human("Something", "doing the safe thing") is False
    assert capsys.readouterr().err == ""


def test_default_helper_narrates_on_stderr_not_stdout(headless, capsys):
    """Under the 8 output contract stdout is data. A policy decision
    announced on stdout would corrupt --json for the caller."""
    assert use_default_without_human("Something", "doing the safe thing") is True
    out = capsys.readouterr()
    assert out.out == ""
    assert "doing the safe thing" in out.err


# -- env var masking: safe default exists, so it must NOT fail --------------


SECRET = "sk-live-abcdef0123456789abcdef"


def test_secrets_are_masked_not_leaked_when_nobody_can_be_asked(headless, capsys):
    content = f"API_KEY={SECRET}\n"
    out = handle_env_variables(content, {"API_KEY": SECRET})
    assert SECRET not in out, "a secret reached the payload with no human to approve it"
    assert "*" * len(SECRET) in out


def test_masking_headlessly_does_not_raise(headless):
    """Refusing here would make kopipasta unusable in CI for zero safety gain."""
    handle_env_variables(f"API_KEY={SECRET}\n", {"API_KEY": SECRET})


def test_the_masking_decision_is_announced(headless, capsys):
    handle_env_variables(f"API_KEY={SECRET}\n", {"API_KEY": SECRET})
    assert "masking" in capsys.readouterr().err.lower()


def test_no_prompt_is_attempted_at_all(headless, monkeypatch):
    """Not just 'does not hang' — it must never reach the blocking read."""

    def explode(*a, **k):
        raise AssertionError("input() was called with no human attached")

    monkeypatch.setattr("builtins.input", explode)
    handle_env_variables(f"API_KEY={SECRET}\n", {"API_KEY": SECRET})


def test_an_explicit_human_decision_is_still_honoured(with_human, monkeypatch):
    """The guard must not quietly override a real user who said 'keep'."""
    monkeypatch.setattr("builtins.input", lambda *a: "k")
    out = handle_env_variables(f"API_KEY={SECRET}\n", {"API_KEY": SECRET})
    assert SECRET in out


def test_stdin_dying_mid_prompt_falls_back_to_masking(with_human, monkeypatch):
    """isatty() lied, or the terminal went away between check and read. An
    EOFError inside `while True` must not become a spin or a traceback."""

    def eof(*a, **k):
        raise EOFError()

    monkeypatch.setattr("builtins.input", eof)
    out = handle_env_variables(f"API_KEY={SECRET}\n", {"API_KEY": SECRET})
    assert SECRET not in out


# -- task description: no safe default, so it must refuse -------------------


def test_task_prompt_refuses_and_names_the_flag(headless):
    from rich.console import Console

    from kopipasta.prompt import get_task_from_user_interactive

    with pytest.raises(NoHumanAttached, match="--task"):
        get_task_from_user_interactive(Console(file=io.StringIO()))


def test_task_prompt_refuses_before_printing_anything(headless):
    """Checked before the first render, so nothing is drawn into a pipe."""
    from rich.console import Console

    from kopipasta.prompt import get_task_from_user_interactive

    buf = io.StringIO()
    with pytest.raises(NoHumanAttached):
        get_task_from_user_interactive(Console(file=buf))
    assert buf.getvalue() == ""


# -- large web content: no safe default, but flags remove the question ------


LARGE = "y" * 20_000


def _app(argv):
    app = KopipastaApp(argv)
    app._parse_args()
    return app


def _fake_fetch(monkeypatch, url):
    monkeypatch.setattr(
        "kopipasta.main.fetch_web_content",
        lambda u: ((u, False, None, "text"), LARGE, LARGE[:100]),
    )


def test_large_url_refuses_rather_than_guessing(headless, monkeypatch):
    """full vs snippet changes what the model sees. A wrong guess produces a
    plausible wrong answer, which is worse than an obvious failure."""
    url = "https://example.com/big"
    _fake_fetch(monkeypatch, url)
    app = _app([url, "-t", "task"])
    with pytest.raises(NoHumanAttached, match="--url-full"):
        app._handle_web_input(url)


@pytest.mark.parametrize(
    "flag,expect_snippet", [("--url-full", False), ("--url-snippet", True)]
)
def test_url_flags_answer_the_question_up_front(
    headless, monkeypatch, flag, expect_snippet
):
    url = "https://example.com/big"
    _fake_fetch(monkeypatch, url)
    app = _app([url, "-t", "task", flag])
    app._handle_web_input(url)

    file_tuple, content = app.web_contents[url]
    assert file_tuple[1] is expect_snippet
    assert (len(content) < 1000) is expect_snippet


def test_url_flags_are_mutually_exclusive():
    """Refused as a usage error — exit 1, not argparse's 2.

    This used to assert only that *something* exited. Spec §8 gives 2 its own
    meaning ("no usable backend"), so the code is the part worth pinning: an
    agent told 2 goes looking for an API key it already has.
    """
    from kopipasta.core.errors import EXIT_USAGE, UsageError

    with pytest.raises(UsageError) as e:
        _app(["https://example.com/x", "--url-full", "--url-snippet"])
    assert e.value.exit_code == EXIT_USAGE
    assert "--url-snippet" in str(e.value)


# -- editor launches: a terminal editor on a pipe is the same bug ----------


def test_opening_the_template_in_an_editor_refuses(headless, monkeypatch):
    """$EDITOR defaults to vim. Launching it onto a pipe blocks forever with
    nobody able to type ':q' — the original bug in a different costume.
    Found by dogfooding the oracle at this repo, not by the greps."""
    import kopipasta.prompt as p

    def explode(*a, **k):
        raise AssertionError("an editor was launched with no human attached")

    monkeypatch.setattr(p.subprocess, "call", explode)
    monkeypatch.setattr(p.os, "startfile", explode, raising=False)

    with pytest.raises(NoHumanAttached, match="editor"):
        p.open_template_in_editor()


def test_opening_the_profile_in_an_editor_refuses(headless, monkeypatch):
    import kopipasta.config as c

    def explode(*a, **k):
        raise AssertionError("an editor was launched with no human attached")

    monkeypatch.setattr(c.subprocess, "call", explode)
    monkeypatch.setattr(c.os, "startfile", explode, raising=False)

    with pytest.raises(NoHumanAttached, match="editor"):
        c.open_profile_in_editor()


def test_the_file_still_exists_so_it_can_be_edited_directly(headless, monkeypatch):
    """Refusing to launch an editor must not also refuse to create the file —
    otherwise the advice in the error message is a lie."""
    import kopipasta.config as c

    monkeypatch.setattr(c.subprocess, "call", lambda *a, **k: None)
    monkeypatch.setattr(c.os, "startfile", lambda *a, **k: None, raising=False)

    with pytest.raises(NoHumanAttached):
        c.open_profile_in_editor()

    assert c.get_global_profile_path().exists()


def test_small_urls_never_ask_at_all(headless, monkeypatch):
    """The prompt only exists for large content; a small page must stay
    headless-safe with no flags."""
    url = "https://example.com/small"
    monkeypatch.setattr(
        "kopipasta.main.fetch_web_content",
        lambda u: ((u, False, None, "text"), "tiny", "tiny"),
    )
    app = _app([url, "-t", "task"])
    app._handle_web_input(url)
    assert app.web_contents[url][1] == "tiny"
