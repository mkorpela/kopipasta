import base64
import os
import sys
import pyperclip
from rich.console import Console
from typing import Optional


class ClipboardError(Exception):
    """Raised when writing to the clipboard fails (either local or OSC 52)."""

    pass


def is_ssh_session() -> bool:
    """Detects if the process is running inside an SSH session."""
    return "SSH_CLIENT" in os.environ or "SSH_TTY" in os.environ


def write_osc52(text: str, console: Optional[Console] = None) -> None:
    """
    Writes text to the local machine's clipboard over an SSH session
    using the OSC 52 terminal escape sequence.
    """
    payload_bytes = text.encode("utf-8", errors="replace")

    # Warn if payload is exceptionally large (many terminals truncate or reject > 4MB)
    if console and len(payload_bytes) > 4 * 1024 * 1024:
        console.print(
            "\n[bold yellow]⚠️ Warning: Clipboard payload exceeds 4MB.[/bold yellow]\n"
            "[yellow]Your terminal emulator may truncate or reject this OSC 52 sequence.\n"
            "Consider using 'Extend Context (e)' to copy smaller deltas instead.[/yellow]"
        )

    payload = base64.b64encode(payload_bytes).decode("ascii")
    seq = f"\033]52;c;{payload}\a"

    # Wrap in tmux DCS passthrough if running inside tmux
    if os.environ.get("TMUX"):
        seq = f"\033Ptmux;\033{seq}\033\\"

    try:
        # Write directly to /dev/tty to bypass stdout buffering or redirection
        with open("/dev/tty", "wb") as tty:
            tty.write(seq.encode("ascii"))
    except OSError:
        # Fallback to sys.stdout if /dev/tty is unavailable
        sys.stdout.buffer.write(seq.encode("ascii"))
        sys.stdout.buffer.flush()


def copy_to_clipboard(text: str, console: Optional[Console] = None) -> None:
    """
    Copies text to the clipboard, automatically routing to OSC 52
    if in an SSH session, or falling back to native pyperclip.
    """
    force_osc52 = os.environ.get("KOPIPASTA_CLIPBOARD") == "osc52"

    if force_osc52 or is_ssh_session():
        try:
            write_osc52(text, console)
            return
        except Exception as e:
            raise ClipboardError(f"OSC 52 clipboard write failed: {e}")

    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as e:
        raise ClipboardError(f"Local clipboard write failed: {e}")
