import os
import re
import subprocess
import json
from typing import Optional, TypedDict
from rich.console import Console

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
        # In the interactive TUI, we return False to let the user clean up, 
        # or we could ask via click/input. Let's return False to be safe and strict.
        console.print("[red]Aborting session start. Please commit or stash changes.[/red]")
        return False

    metadata = {
        "start_commit": head_hash,
        "timestamp": "TODO_TIMESTAMP" 
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
        # Stage everything
        subprocess.run(["git", "add", "."], cwd=project_root, check=True, capture_output=True)
        
        # Check if anything is staged
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=project_root)
        if result.returncode != 0: # 1 means diff found (dirty)
            subprocess.run(
                ["git", "commit", "--no-verify", "-m", message],
                cwd=project_root,
                check=True,
                capture_output=True
            )
            console.print(f"[dim]Auto-committed changes.[/dim]")
            return True
            
    except subprocess.CalledProcessError as e:
        console.print(f"[yellow]Auto-commit failed: {e.stderr}[/yellow]")
    return False