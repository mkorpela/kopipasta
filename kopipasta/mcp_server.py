import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, List
from collections import defaultdict

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from kopipasta.file import is_ignored, is_binary
from kopipasta.config import read_gitignore

# Initialize FastMCP server
mcp = FastMCP("kopipasta-ralph")

RALPH_CONFIG_FILENAME = ".ralph.json"


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
        return json.load(f)


def _get_project_root_override() -> str | None:
    """Resolves the project root from CLI args or environment."""
    # Check for --project-root argument
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--project-root" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--project-root="):
            return arg.split("=", 1)[1]
    # Fallback to environment variable
    return os.environ.get("KOPIPASTA_PROJECT_ROOT")


def _get_project_root() -> Path:
    config = _load_config()
    return Path(config["project_root"])


def _run_cmd(command: str, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return f"$ {command}\nExit Code: {result.returncode}\n--- STDOUT ---\n{result.stdout}\n--- STDERR ---\n{result.stderr}"
    except Exception as e:
        return f"Error running command '{command}': {e}"


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
        task = config.get("task_description", "")

        output = []

        if task:
            output.append(f"# Task\n{task}\n")

        output.append("# Available Files\n")
        output.append("Use `list_files` to see all project files.\n")
        output.append("Use `read_files` to retrieve file contents.\n")

        all_files = sorted(list(set(editable_files)))

        for rel_path in all_files:
            if rel_path in editable_files:
                output.append(f"- {rel_path} (EDITABLE)")
            else:
                output.append(f"- {rel_path} (READ-ONLY)")

        # Auto-run verification to give the agent immediate feedback
        output.append("\n# Verification Result\n")
        command = config.get("verification_command")
        if command:
            try:
                result = subprocess.run(
                    command,
                    cwd=str(project_root),
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                output.append(f"$ {command}")
                output.append(f"Exit Code: {result.returncode}")
                output.append("--- STDOUT ---")
                output.append(result.stdout)
                output.append("--- STDERR ---")
                output.append(result.stderr)
            except subprocess.TimeoutExpired:
                output.append(f"$ {command}")
                output.append("Error: Command timed out after 300 seconds.")
            except Exception as e:
                output.append(f"Error running verification: {e}")
        else:
            output.append("No verification command configured.")

        return "\n".join(output)

    except Exception as e:
        return f"Error reading context: {str(e)}"


@mcp.tool()
def list_files() -> str:
    """
    Recursively lists all files in the project root, respecting .gitignore.
    """
    try:
        project_root = _get_project_root()
        ignore_patterns = read_gitignore()

        all_files = []
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
                    rel_path = os.path.relpath(full_path, project_root)
                    all_files.append(rel_path)

        return "\n".join(sorted(all_files))
    except Exception as e:
        return f"Error listing files: {e}"


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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
