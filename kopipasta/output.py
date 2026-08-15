"""stdout is the artifact. Everything else is narration and belongs on stderr.

Auditing every `print()` in this package would be necessary but not
sufficient: `rich`, `click` and anything else on the path write to stdout too
and have no such contract. So rather than chase call sites, the whole run
executes with `sys.stdout` pointed at `sys.stderr`, and the one thing that is
genuinely output is written to the saved real handle (spec 11.2b).

For a human nothing changes — both streams land on the same terminal. For
`kopipasta > prompt.txt` it is the difference between a usable artifact and a
file that starts with "Generated prompt:" and ends with a coffee emoji.
"""

import argparse
import contextlib
import json as _json
import sys
from typing import IO, Any, Iterator, Optional

_artifact_stream: Optional[IO[str]] = None


def _make_narration_safe(stream: Optional[IO[str]]) -> None:
    """Narration must never be able to kill the run.

    The completion banner contains emoji and the prompt body contains
    whatever is in the user's files. Redirected stderr on Windows is cp1252,
    which cannot encode either, and the resulting UnicodeEncodeError would
    surface as a crash with no obvious cause.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass


@contextlib.contextmanager
def stdout_reserved_for_output() -> Iterator[IO[str]]:
    """Point sys.stdout at stderr for the duration; yield the real stdout.

    rich resolves `Console.file` at write time, so consoles built before this
    runs are redirected too — which matters, since the app builds one in its
    constructor.

    Re-entrant: an inner scope yields the stream the outer one already saved.
    That lets a verb establish the contract for itself, so it holds however it
    was reached, without a second scope stealing the handle.
    """
    global _artifact_stream
    previous = _artifact_stream
    if previous is not None:
        # Already reserved by an outer scope. Re-capturing here would save the
        # *redirected* stdout — which by now is stderr — as the artifact
        # stream, and every artifact for the rest of the run would go to
        # stderr while the exit code said everything was fine. Found by
        # dogfooding: `kopipasta ask --json > out.json` produced an empty file
        # and exit 0 once the verb started establishing the contract itself.
        yield previous
        return
    saved = sys.stdout
    _make_narration_safe(sys.stderr)
    # The artifact carries the user's file contents, so it is the stream most
    # likely to meet a character cp1252 cannot encode. main._configure_platform
    # reconfigures `sys.stdout`, which by then is stderr — it can no longer
    # reach the real handle, so it is fixed up here where the handle is known.
    _make_narration_safe(saved)
    _artifact_stream = saved
    sys.stdout = sys.stderr
    try:
        yield saved
    finally:
        sys.stdout = saved
        _artifact_stream = previous


def artifact_stream() -> IO[str]:
    """The real stdout while narration is redirected; plain stdout otherwise."""
    return _artifact_stream if _artifact_stream is not None else sys.stdout


def emit(text: str) -> None:
    """Write the artifact to the real stdout, wherever narration is pointed."""
    stream = artifact_stream()
    try:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")
        stream.flush()
    except (BrokenPipeError, ValueError):
        # `kopipasta ask ... | head` closes the pipe under us. That is the
        # reader's decision, not a failure of the run, and a traceback here
        # would bury whatever the caller actually wanted to see.
        pass


def emit_json(payload: Any) -> None:
    """The artifact as a single JSON object — spec §8.

    Written through `emit`, so library narration on the way (`.gitignore
    detected.`, patcher progress) lands on stderr and cannot appear mid-object
    and make it unparseable.
    """
    emit(_json.dumps(payload, indent=2, default=str))


def narrate(text: str) -> None:
    """One line of narration. Never the artifact, always stderr."""
    try:
        print(text, file=sys.stderr)
    except (BrokenPipeError, ValueError):
        pass  # See emit(): a closed reader must not become a failed run.


class HelpToStdoutParser(argparse.ArgumentParser):
    """`kopipasta ask --help | less` must work, so help is output, not narration.

    Pre-scanning argv for `-h` instead would misfire on a task string of "-h"
    or a file named `--help`, and would disable the redirect for the entire
    run. Letting argparse decide what is a flag is the only reliable answer.

    It also owns the exit code for a bad command line. argparse's own
    `error()` exits **2**, and spec §8 reserves 2 for "no usable backend — no
    key, no command": every mistyped flag was telling the caller its
    credentials were missing. An agent has only that table to reason from, so
    it would go looking for an API key over a typo — the one recovery that
    cannot possibly work. Usage errors are exit 1.

    `exit()` is deliberately left alone. argparse routes `--help` through it
    with status 0, and overriding both doors would turn a successful `--help`
    into a failure.
    """

    def print_help(self, file=None):
        super().print_help(artifact_stream())

    def error(self, message):
        # Imported here rather than at module scope: output.py is the one
        # module everything else is allowed to depend on, and it stays that way
        # by not depending on kopipasta.core at import time.
        from kopipasta.core.errors import UsageError

        raise UsageError(
            f"{self.prog}: {message}",
            hint=f"{self.prog} --help",
        )
