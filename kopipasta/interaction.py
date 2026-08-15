"""Single source of truth for "is a human attached?".

Every blocking prompt in kopipasta must consult this before it blocks. A
prompt with nobody on the other end is an unbounded stall inside someone
else's subprocess, and an agentic harness cannot tell a stall from slow work.

The guard lives here rather than at each call site so that a new prompt added
anywhere inherits the protection by calling one function.
"""

import os
import sys

# Exit code for "this needed a terminal and there wasn't one". Distinct from
# a usage error (1) because the fix is different: the caller needs a different
# invocation or a policy flag, not a corrected command line.
EXIT_NO_HUMAN = 8


class NoHumanAttached(RuntimeError):
    """Raised when code needs an interactive terminal and none is present."""


def human_attached() -> bool:
    """True only when it is safe to block for keyboard input.

    Deliberately conservative: when in doubt, assume nobody is watching.
    Falsely refusing to prompt produces a clear error; falsely prompting
    produces a hang.
    """
    if os.environ.get("KOPIPASTA_NONINTERACTIVE", "").strip():
        return False
    if os.environ.get("CI", "").strip():
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        # Streams replaced with non-file objects, or already closed.
        return False


def require_human(what: str, hint: str = "") -> None:
    """Raise NoHumanAttached unless a human could actually answer."""
    if human_attached():
        return
    msg = f"{what} needs an interactive terminal, and none is attached."
    if hint:
        msg = f"{msg} {hint}"
    raise NoHumanAttached(msg)
