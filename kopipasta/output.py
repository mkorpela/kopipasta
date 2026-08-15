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

import contextlib
import sys
from typing import IO, Iterator, Optional

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
    """
    global _artifact_stream
    saved = sys.stdout
    previous = _artifact_stream
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
        # Restore rather than clear: nesting would otherwise leave the outer
        # context emitting its artifact to stderr.
        _artifact_stream = previous


def artifact_stream() -> IO[str]:
    """The real stdout while narration is redirected; plain stdout otherwise."""
    return _artifact_stream if _artifact_stream is not None else sys.stdout


def emit(text: str) -> None:
    """Write the artifact to the real stdout, wherever narration is pointed."""
    stream = artifact_stream()
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()
