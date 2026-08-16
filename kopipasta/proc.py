"""How this tool decodes another tool's output. One answer, in one place.

`subprocess.run(..., text=True)` decodes with `locale.getpreferredencoding()`.
On a stock Windows box that is cp1252, and every child process whose output
contains a character outside it — vitest's `U+23AF` rules, eslint's `U+2716`,
ruff's arrows, a git path with an umlaut — killed the reader thread with

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d

The failure mode is worse than the crash. `subprocess` runs those readers on
background threads, so the exception is *printed* rather than raised: the exit
code still arrives, and the captured output arrives **empty**. `--verify`
reported `{"exit": 1, "output": ""}` — a failure with zero diagnostics, at the
exact moment the diagnostics are the only thing anyone wants. Worse still,
when only one of the two threads died the survivor's text was kept whole, so
the caller read a plausible transcript that had silently lost its first half.

`errors="replace"` is the second half of the fix and is not optional. A lone
0x9d in an otherwise fine stream is still a decode error, and utf-8 alone
would trade the cp1252 crash for a rarer one. There is no case in which
losing the whole stream beats losing one character to a `U+FFFD`.

Spelled once, as a constant, because the bug is not in any single call site:
it is in the default. Every captured subprocess in this package passes
`**TEXT`, and `tests/test_subprocess_encoding.py` walks the AST to keep it
that way for the next one somebody writes.
"""

from typing import Any, Dict

#: `**TEXT` on any subprocess call whose output we capture and read.
TEXT: Dict[str, Any] = {"text": True, "encoding": "utf-8", "errors": "replace"}

__all__ = ["TEXT"]
