"""stdout is the artifact; everything else is narration (spec 8, 11.2b).

Before this, `kopipasta > prompt.txt` produced a file beginning "Generated
prompt:", containing 80 dashes, a list of included files, and a coffee emoji
banner. The prompt was in there somewhere.
"""

import io
import subprocess
import sys

import pytest
from rich.console import Console

from kopipasta.output import emit, stdout_reserved_for_output


def test_narration_goes_to_stderr_and_the_artifact_to_stdout(capsys):
    with stdout_reserved_for_output():
        print("Generated prompt:")
        emit("THE ARTIFACT")
        print("-" * 80)

    captured = capsys.readouterr()
    assert captured.out == "THE ARTIFACT\n"
    assert "Generated prompt:" in captured.err
    assert "-" * 80 in captured.err


def test_rich_consoles_built_before_the_swap_are_redirected_too(capsys):
    """The app constructs its Console in __init__, before main() runs. rich
    resolves Console.file at write time, which is what makes this work."""
    console = Console()

    with stdout_reserved_for_output():
        console.print("narration from a pre-existing console")
        emit("THE ARTIFACT")

    captured = capsys.readouterr()
    assert captured.out == "THE ARTIFACT\n"
    assert "narration from a pre-existing console" in captured.err


def test_stdout_is_restored_afterwards(capsys):
    original = sys.stdout
    with stdout_reserved_for_output():
        assert sys.stdout is not original
    assert sys.stdout is original


def test_stdout_is_restored_even_if_the_run_raises():
    original = sys.stdout
    with pytest.raises(ValueError):
        with stdout_reserved_for_output():
            raise ValueError("boom")
    assert sys.stdout is original


def test_the_artifact_is_not_double_newlined(capsys):
    with stdout_reserved_for_output():
        emit("already ends in a newline\n")
    assert capsys.readouterr().out == "already ends in a newline\n"


def test_emit_outside_the_context_still_writes_to_stdout(capsys):
    """Callers must not have to care whether the context is active."""
    emit("THE ARTIFACT")
    assert capsys.readouterr().out == "THE ARTIFACT\n"


def test_nesting_does_not_strand_the_outer_artifact_stream(capsys):
    """Clearing the stream on the inner exit would leave the outer context
    emitting its artifact to stderr."""
    with stdout_reserved_for_output() as outer:
        with stdout_reserved_for_output():
            pass
        from kopipasta import output

        assert output.artifact_stream() is outer
        emit("THE ARTIFACT")

    assert capsys.readouterr().out == "THE ARTIFACT\n"


def test_the_artifact_stream_is_made_encoding_safe():
    """The artifact carries the user's file contents, so it is the stream most
    likely to meet a character cp1252 cannot encode. main._configure_platform
    reconfigures sys.stdout, which by then is stderr, so it cannot fix this."""
    real = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    original = sys.stdout
    sys.stdout = real
    try:
        with stdout_reserved_for_output():
            emit("caf\u00e9 \u2615 \u4f60\u597d")  # would raise under cp1252/strict
    finally:
        sys.stdout = original


def test_narration_survives_a_stream_that_cannot_encode_it():
    """Redirected stderr on Windows is cp1252, which cannot encode the
    completion banner's emoji. A crash there would be baffling and would be
    caused entirely by decoration."""
    narration = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    original = sys.stderr
    sys.stderr = narration
    try:
        with stdout_reserved_for_output():
            print("Kopipasta Complete! \u2615\U0001f35d")
    finally:
        sys.stderr = original


def test_redirecting_the_artifact_no_longer_disables_the_tui(monkeypatch):
    """`kopipasta > prompt.txt` at a terminal used to be refused, because
    human_attached() saw a non-tty stdout. The keyboard and the display were
    both still there; only the artifact had been redirected."""
    from kopipasta.interaction import human_attached

    class Tty(io.StringIO):
        def isatty(self):
            return True

    class Piped(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("KOPIPASTA_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stderr", Tty())
    monkeypatch.setattr(sys, "stdout", Piped())

    assert human_attached() is False
    with stdout_reserved_for_output():
        assert human_attached() is True


def test_a_fully_piped_run_is_still_headless(monkeypatch):
    """The mirror: when nothing is a terminal, the redirect must not fake one."""
    from kopipasta.interaction import human_attached

    class Piped(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("KOPIPASTA_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", Piped())
    monkeypatch.setattr(sys, "stderr", Piped())
    monkeypatch.setattr(sys, "stdout", Piped())

    with stdout_reserved_for_output():
        assert human_attached() is False


# -- the real binary -------------------------------------------------------


def run_cli(args, tmp_path):
    return subprocess.run(
        [sys.executable, "-m", "kopipasta.main", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )


def test_help_still_goes_to_stdout(tmp_path):
    """`kopipasta --help | less` is a reasonable thing to type: for --help the
    text IS the requested output. Redirecting it broke this, which is why the
    contract has an exception rather than a blanket rule."""
    result = run_cli(["--help"], tmp_path)
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
    assert result.stderr == ""


def test_a_usage_error_still_goes_to_stderr(tmp_path):
    """The mirror of the above: argparse's own errors are not output."""
    result = run_cli(["--nonexistent-flag"], tmp_path)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "unrecognized" in result.stderr.lower()


def test_a_dash_h_after_a_double_dash_does_not_disable_the_redirect(tmp_path):
    """The first cut scanned argv for -h. `kopipasta -- -h` names a FILE
    called -h, but the scan saw the token and turned narration back onto
    stdout for the whole run, corrupting the artifact. Letting argparse
    decide what counts as a flag is the only reliable answer.

    (`-t -h` does not discriminate: argparse rejects it either way.)
    """
    result = run_cli(["--", "-h"], tmp_path)
    assert result.stdout == "", "narration reached stdout"
    assert result.stderr.strip() != "", "the run should still have narrated"


def test_refusals_keep_stdout_empty(tmp_path):
    """An agent redirecting stdout to a file must get an empty file on
    failure, not an error message it might mistake for a prompt."""
    for args in (["--edit-template"], ["pack"], ["--nonexistent-flag"]):
        result = run_cli(args, tmp_path)
        assert result.returncode != 0
        assert result.stdout == "", f"{args} wrote {result.stdout!r} to stdout"
        assert result.stderr.strip() != ""
