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
        # sys.stdout here means "the stream we narrate to". During a run it is
        # pointed at stderr (output.stdout_reserved_for_output), so
        # `kopipasta > prompt.txt` from a terminal correctly keeps the TUI:
        # the keyboard and the display are both still there, only the artifact
        # was redirected. Before that split, redirecting the prompt to a file
        # disabled the interface you needed to produce it.
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        # Streams replaced with non-file objects, or already closed.
        return False


def require_human(what: str, hint: str = "") -> None:
    """Raise NoHumanAttached unless a human could actually answer.

    Use this where the question has NO safe default — "which files do you
    want?" cannot be guessed. Where a safe default does exist, prefer
    `use_default_without_human`: refusing to run is a worse answer than the
    obviously-correct one.
    """
    if human_attached():
        return
    msg = f"{what} needs an interactive terminal, and none is attached."
    if hint:
        msg = f"{msg} {hint}"
    raise NoHumanAttached(msg)


def use_default_without_human(what: str, default_desc: str) -> bool:
    """True when nobody can answer and the caller should apply its safe default.

    The counterpart to `require_human`, for questions that *do* have a
    conservative answer — mask the secret, don't delete the file, don't apply
    the suspicious patch. Failing fast there would make kopipasta unusable
    headlessly for no safety gain.

    The substitution is narrated on stderr (never stdout, which is data under
    the §8 output contract) so a headless run is auditable after the fact: a
    silent policy decision about a secret is its own kind of bug.
    """
    if human_attached():
        return False
    print(f"kopipasta: {what} needs a human; {default_desc} instead.", file=sys.stderr)
    return True


def get_task_from_user_interactive(console=None, default_text: str = "") -> str:
    """Ask the human for a multiline task description.

    Lives here, next to the guard it depends on, rather than in the TUI's
    module: both surfaces need to ask a human for a task when one is present,
    and `core/ask.py` reaching up into `kopipasta.prompt` for it made the
    headless path depend on the interactive one (spec §13).

    `prompt_toolkit` and `rich` are imported inside the function, so a run
    that never asks never loads them — which is what keeps the agent CLI's
    import graph clear of the terminal UI.
    """
    # Checked before the first console.print, so nothing is drawn into a pipe.
    # No safe default exists here: an empty task silently produces a useless
    # prompt, and guessing one is worse than refusing.
    require_human(
        "Entering a task description",
        "Pass -t/--task instead, or set KOPIPASTA_NONINTERACTIVE=1 to make this explicit.",
    )

    from prompt_toolkit import prompt as prompt_toolkit_prompt
    from prompt_toolkit.styles import Style
    from rich.console import Console

    # Narration, so stderr — the artifact on stdout is the prompt itself.
    console = console or Console(stderr=True)

    console.print("\n[bold cyan]📝 Please enter your task instructions.[/bold cyan]")
    if default_text:
        console.print(
            "   [dim](Pre-filled from previous session. Edit or clear as needed.)[/dim]"
        )
    console.print(
        "   - Press [bold]Meta+Enter[/bold] or [bold]Esc[/bold] then [bold]Enter[/bold] to submit."
    )
    console.print("   - Press [bold]Ctrl-C[/bold] to abort.")

    style = Style.from_dict({"": "#00ff00"})

    try:
        task = prompt_toolkit_prompt(
            "> ",
            multiline=True,
            prompt_continuation="  ",
            style=style,
            default=default_text,
        )
        return task.strip()
    except KeyboardInterrupt:
        return ""
