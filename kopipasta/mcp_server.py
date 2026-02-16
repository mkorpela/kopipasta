import json
import os
import re
import platform
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, Any, Generator, List, Optional, Tuple, IO
from collections import defaultdict

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from kopipasta.file import is_ignored, is_binary
from kopipasta.config import read_gitignore, get_active_project

# Initialize FastMCP server
mcp = FastMCP("kopipasta-ralph")

RALPH_CONFIG_FILENAME = ".ralph.json"

# Global state for background processes
# Maps PID -> (process_handle, out_path, err_path, out_file_handle, err_file_handle)
_BACKGROUND_JOBS: Dict[int, Tuple[subprocess.Popen, Path, Path, IO, IO]] = {}

# Safe timeout margin for Claude Desktop (60s limit -> 50s safe)
SAFE_CLIENT_TIMEOUT = 50


class EditBlock(BaseModel):
    file_path: str = Field(..., description="Relative path to the file to modify.")
    search: str = Field(
        ...,
        description="The exact string to be replaced. Must match exactly one location in the file.",
    )
    replace: str = Field(..., description="The new string to insert.")


def _load_config() -> Dict[str, Any]:
    """Loads the Ralph configuration from the current working directory."""
    # Try multiple strategies to find the project root:
    # 1. Command-line argument (most reliable)
    # 2. Environment variable
    # 3. Current working directory (fallback)
    env_root = _get_project_root_override()
    if env_root:
        config_path = Path(env_root) / RALPH_CONFIG_FILENAME
    else:
        config_path = Path.cwd() / RALPH_CONFIG_FILENAME

    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file {RALPH_CONFIG_FILENAME} not found. "
            f"Please run 'r' in kopipasta to generate it. "
            f"(searched: {config_path})"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        result: Dict[str, Any] = json.load(f)
        return result


def _get_project_root_override() -> Optional[str]:
    """Resolves the project root from CLI args or environment."""
    # Check for --project-root argument
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--project-root" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--project-root="):
            return arg.split("=", 1)[1]

    # Check environment variable
    env_root = os.environ.get("KOPIPASTA_PROJECT_ROOT")
    if env_root:
        return env_root

    # Check global active project pointer
    active = get_active_project()
    if active:
        return str(active)

    return None


def _get_shell_env() -> Dict[str, str]:
    """Build a subprocess environment with the user's full login-shell PATH.

    Claude Desktop on macOS launches MCP servers with a minimal PATH that
    excludes ~/.cargo/bin, ~/.local/bin, etc.  We resolve the real PATH
    once via the user's login shell so tools like ``uv`` are discoverable.
    """
    env = os.environ.copy()
    if platform.system() != "Darwin":
        return env

    shell = os.environ.get("SHELL", "/bin/zsh")
    try:
        result = subprocess.run(
            [shell, "-lc", "echo $PATH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            env["PATH"] = result.stdout.strip()
    except Exception:
        pass  # fall back to existing env

    return env


def _prepare_command(command: str) -> str:
    """Prepare a shell command for the current platform.

    On Windows, cmd.exe cannot execute .ps1 files directly — it either
    silently "opens" them (exit 0, no output) or hangs.  We detect bare
    .ps1 invocations and wrap them with powershell.exe.

    Commands already prefixed with powershell/pwsh are left untouched.
    """
    if platform.system() != "Windows":
        return command

    stripped = command.strip()

    # Already explicitly using powershell/pwsh — don't double-wrap
    if re.match(r"(?i)^(?:powershell|pwsh)\b", stripped):
        return command

    # Split into script path and trailing arguments
    parts = stripped.split(None, 1)
    script = parts[0] if parts else ""
    args_tail = parts[1] if len(parts) > 1 else ""

    if script.lower().endswith(".ps1"):
        wrapped = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
        if args_tail:
            wrapped += f" {args_tail}"
        return wrapped

    return command


def _get_project_root() -> Path:
    config = _load_config()
    return Path(config["project_root"])


def _run_cmd(command: str, cwd: Path) -> str:
    """
    Runs a command with 'Smart Long-Polling'.
    If command takes > 50s, it backgrounds the process and returns a prompt 
    for the agent to wait on it.
    """
    command = _prepare_command(command)
    
    # Generate unique output file names
    run_id = uuid.uuid4().hex[:8]
    out_path = cwd / f".ralph_exec_{run_id}.out"
    err_path = cwd / f".ralph_exec_{run_id}.err"
    
    try:
        # We use files for stdout/stderr to prevent buffer deadlocks and allow 
        # persistent capture if we detach.
        out_file = open(out_path, "w", encoding="utf-8")
        err_file = open(err_path, "w", encoding="utf-8")
        
        proc = subprocess.Popen(
            command, 
            cwd=cwd, 
            shell=True, 
            stdout=out_file,
            stderr=err_file,
            env=_get_shell_env(),
            stdin=subprocess.DEVNULL,
            text=True
        )
        
        # Wait for the safe duration
        try:
            proc.wait(timeout=SAFE_CLIENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Handoff to background
            _BACKGROUND_JOBS[proc.pid] = (proc, out_path, err_path, out_file, err_file)
            return (
                f"[WARNING] Verification is taking longer than {SAFE_CLIENT_TIMEOUT}s.\n"
                f"Process (PID {proc.pid}) continues in background.\n"
                f"Action Required: Call `wait_for_verification(pid={proc.pid})` to retrieve results."
            )

        # Finished within time
        out_file.close()
        err_file.close()
        
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
        
        # Cleanup temp files
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)
        
        return f"$ {command}\nExit Code: {proc.returncode}\n--- STDOUT ---\n{stdout}\n--- STDERR ---\n{stderr}"

    except Exception as e:
        return f"Error starting command '{command}': {e}"


@mcp.tool()
def read_context() -> str:
    """
    Reads the project context overview defined by kopipasta.
    Returns a file listing with permissions and the verification command result.
    and the task description.
    """
    try:
        config = _load_config()
        project_root = Path(config["project_root"])
        editable_files = config.get("editable_files", [])

        output = []
        output.append("# Available Files\n")
        output.append("Use `read_files` to retrieve file contents.\n")

        for rel_path in list_files():
            if rel_path in editable_files:
                output.append(f"- {rel_path} (EDITABLE)")
            else:
                output.append(f"- {rel_path} (READ-ONLY)")

        # Auto-run verification to give the agent immediate feedback
        output.append("\n# Verification Result\n")
        command = config.get("verification_command")
        if command:
            output.append(_run_cmd(command, project_root))
        else:
            output.append("No verification command configured.")

        return "\n".join(output)

    except Exception as e:
        return f"Error reading context: {str(e)}"


def list_files() -> Generator[str, None, None]:
    try:
        project_root = _get_project_root()
        ignore_patterns = read_gitignore()

        for root, dirs, files in os.walk(project_root):
            # Filter directories
            dirs[:] = [
                d
                for d in dirs
                if not is_ignored(
                    os.path.join(root, d), ignore_patterns, str(project_root)
                )
            ]

            for file in files:
                full_path = os.path.join(root, file)
                if not is_ignored(full_path, ignore_patterns, str(project_root)):
                    yield os.path.relpath(full_path, project_root)

        return
    except Exception:
        return


@mcp.tool()
def read_files(paths: List[str]) -> str:
    """
    Reads the contents of specific project files.
    Allowed to read any non-ignored, non-binary file in the project.

    Args:
        paths: List of relative file paths to read.
    """
    try:
        config = _load_config()
        project_root = Path(config["project_root"])
        ignore_patterns = read_gitignore()
        editable_files = config.get("editable_files", [])

        output = []
        for rel_path in paths:
            file_path = project_root / rel_path

            # Safety checks
            if not file_path.exists():
                output.append(f"## File: {rel_path}\n(File does not exist)\n")
                continue

            if is_ignored(str(file_path), ignore_patterns, str(project_root)):
                output.append(f"## File: {rel_path}\n(Ignored by .gitignore)\n")
                continue

            if is_binary(str(file_path)):
                output.append(f"## File: {rel_path}\n(Binary file)\n")
                continue

            perm = "EDITABLE" if rel_path in editable_files else "READ-ONLY"
            output.append(f"## File: {rel_path} ({perm})")
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                output.append("```")
                output.append(content)
                output.append("```\n")
            except Exception as e:
                output.append(f"(Error reading file: {e})\n")

        return "\n".join(output)
    except Exception as e:
        return f"Error reading context: {str(e)}"


@mcp.tool()
def apply_edits(edits: List[EditBlock]) -> str:
    """
    Atomic Search-and-Replace.
    Applies a list of edits. If ANY edit fails validation (e.g., search block not found
    uniquely), NO files are changed.
    If successful, runs the verification command automatically.

    Args:
        edits: A list of file modifications.
    """
    try:
        config = _load_config()
        editable_files = set(config.get("editable_files", []))
        project_root = Path(config["project_root"])
        verification_cmd = config.get("verification_command")

        # --- Phase 1: Validation & Staging ---
        staged_changes: Dict[Path, str] = {}

        # We need to process edits per file to handle sequential changes to the same file
        edits_by_file = defaultdict(list)
        for edit in edits:
            edits_by_file[edit.file_path].append(edit)

        for rel_path, file_edits in edits_by_file.items():
            if rel_path not in editable_files:
                return f"Permission Denied: '{rel_path}' is not in the editable_files whitelist."

            file_path = project_root / rel_path
            if not file_path.exists():
                return f"Error: File '{rel_path}' does not exist."

            content = file_path.read_text(encoding="utf-8")

            # Apply edits sequentially in memory
            for edit in file_edits:
                count = content.count(edit.search)
                if count == 0:
                    return f"Search block not found in '{rel_path}'.\nBlock:\n{edit.search}"
                if count > 1:
                    return f"Ambiguous match: Search block found {count} times in '{rel_path}'. Block must be unique."

                content = content.replace(edit.search, edit.replace)

            staged_changes[file_path] = content

        # --- Phase 2: Execution ---
        for file_path, new_content in staged_changes.items():
            file_path.write_text(new_content, encoding="utf-8")

        summary = f"Successfully modified {len(staged_changes)} files: {', '.join([str(p.relative_to(project_root)) for p in staged_changes.keys()])}."

        # --- Phase 3: Verification ---
        if verification_cmd:
            verify_output = _run_cmd(verification_cmd, project_root)
            return f"{summary}\n\n# Verification Output\n{verify_output}"

        return summary

    except Exception as e:
        return f"Error applying edits: {str(e)}"


@mcp.tool()
def wait_for_verification(pid: int) -> str:
    """
    Waits for a background verification process to complete.
    Use this when apply_edits or read_context tells you a PID is pending.
    """
    if pid not in _BACKGROUND_JOBS:
        return f"Error: PID {pid} is not a known background job. It may have completed or been lost."
    
    proc, out_path, err_path, out_file, err_file = _BACKGROUND_JOBS[pid]
    
    try:
        # Wait another cycle
        proc.wait(timeout=SAFE_CLIENT_TIMEOUT)
        
        # If we get here, it finished!
        del _BACKGROUND_JOBS[pid]
        out_file.close()
        err_file.close()
        
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
        
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)
        
        return f"[OK] Process {pid} Finished.\nExit Code: {proc.returncode}\n--- STDOUT ---\n{stdout}\n--- STDERR ---\n{stderr}"
        
    except subprocess.TimeoutExpired:
        return f"[PENDING] Process {pid} is still running... Please call `wait_for_verification({pid})` again."


def main():
    mcp.run()


if __name__ == "__main__":
    main()
