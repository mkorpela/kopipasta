import os
import re
import subprocess
import json
from datetime import datetime
from typing import Optional, TypedDict
from rich.console import Console
import click
from kopipasta.ops import check_session_gitignore_status, add_to_gitignore

# Use a local console instance
console = Console()

SESSION_FILENAME = "AI_SESSION.md"

class SessionMetadata(TypedDict):
    start_commit: str
    timestamp: str

def get_git_head_hash(project_root: str) -> Optional[str]:
    """Returns the current HEAD commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def is_git_dirty(project_root: str) -> bool:
    """Returns True if there are uncommitted changes."""
    try:
        # Check for modifications
        subprocess.run(["git", "diff", "--quiet"], cwd=project_root, check=True)
        # Check for staged changes
        subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=project_root, check=True)
        return False
    except subprocess.CalledProcessError:
        return True

def init_session(project_root: str) -> bool:
    """
    Initializes a new session:
    1. Checks for existing session.
    2. Checks git status.
    3. Creates AI_SESSION.md with metadata.
    """
    session_path = os.path.join(project_root, SESSION_FILENAME)
    
    if os.path.exists(session_path):
        console.print(f"[yellow]Session already active at {SESSION_FILENAME}.[/yellow]")
        return False

    head_hash = get_git_head_hash(project_root)
    if not head_hash:
        console.print("[red]Not a git repository. Cannot track session history.[/red]")
        # We rely on the caller to handle pauses, but input here is fine for now
        # Returning True/False allows the UI to refresh
        return False
    
    if is_git_dirty(project_root):
        console.print("[yellow]Warning: You have uncommitted changes.[/yellow]")
        console.print("It is recommended to start a session from a clean state for squashing to work correctly.")
        console.print("[red]Aborting session start. Please commit or stash changes.[/red]")
        return False

    # --- Safety Check: Ensure ignored ---
    if not check_session_gitignore_status(project_root):
        console.print(f"\n[bold yellow]⚠ {SESSION_FILENAME} is NOT ignored by git.[/bold yellow]")
        if click.confirm(f"Add {SESSION_FILENAME} to .gitignore now?", default=True):
            add_to_gitignore(project_root, SESSION_FILENAME)
            console.print(f"[green]Added {SESSION_FILENAME} to .gitignore[/green]")
        else:
            console.print("[red]Safety check failed.[/red]")
            console.print(f"Please manually add {SESSION_FILENAME} to your .gitignore before starting a session.")
            return False


    metadata = {
        "start_commit": head_hash,
        "timestamp": datetime.now().isoformat()
    }
    
    # Create the file with hidden metadata
    content = (
        f"<!-- KOPIPASTA_METADATA {json.dumps(metadata)} -->\n"
        "# Current Working Session\n\n"
        "## Current Progress\n- [ ] Session Started\n\n"
        "## Next Steps\n- [ ] Define Task\n"
    )
    
    try:
        with open(session_path, "w", encoding="utf-8") as f:
            f.write(content)
        console.print(f"[green]Session initialized: {SESSION_FILENAME}[/green]")
        return True
    except IOError as e:
        console.print(f"[red]Failed to create session file: {e}[/red]")
        return False

def get_session_metadata(project_root: str) -> Optional[SessionMetadata]:
    """Reads metadata from AI_SESSION.md."""
    session_path = os.path.join(project_root, SESSION_FILENAME)
    if not os.path.exists(session_path):
        return None
    
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            first_line = f.readline()
            match = re.search(r"<!-- KOPIPASTA_METADATA (.+) -->", first_line)
            if match:
                return json.loads(match.group(1))
    except Exception:
        pass
    return None

def auto_commit_changes(project_root: str, message: str = "kopipasta: auto-checkpoint") -> bool:
    """Adds all changes and commits them (no-verify)."""
    if not get_git_head_hash(project_root):
        return False

    try:
        # Determine correct add command based on ignore status
        # If ignored: 'git add .' is safe (git skips ignored files)
        # If NOT ignored: we must explicitly exclude it via magic pathspec
        cmd = ["git", "add", "."]
        if not check_session_gitignore_status(project_root):
            cmd.append(f":!{SESSION_FILENAME}")

        subprocess.run(
            cmd, 
            cwd=project_root, check=True, capture_output=True
        )
        
        # Check if anything is staged
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=project_root)
        if result.returncode != 0: # 1 means diff found (dirty)
            subprocess.run(
                ["git", "commit", "--no-verify", "--no-gpg-sign", "-m", message],
                cwd=project_root,
                check=True,
                capture_output=True
            )
            console.print(f"[dim]Auto-committed changes.[/dim]")
            return True
            
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e)
        console.print(f"[yellow]Auto-commit failed: {error_msg}[/yellow]")
    return False