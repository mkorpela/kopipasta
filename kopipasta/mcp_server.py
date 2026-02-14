import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Any

from mcp.server.fastmcp import FastMCP
from kopipasta.patcher import parse_llm_output, apply_patches

# Initialize FastMCP server
mcp = FastMCP("kopipasta-ralph")

RALPH_CONFIG_FILENAME = ".ralph.json"


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
    import sys

    # Check for --project-root argument
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--project-root" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--project-root="):
            return arg.split("=", 1)[1]
    # Fallback to environment variable
    return os.environ.get("KOPIPASTA_PROJECT_ROOT")


@mcp.tool()
def read_context() -> str:
    """
    Reads the project context defined by kopipasta.
    Returns a formatted string containing the contents of all readable files
    and the task description.
    """
    try:
        config = _load_config()
        project_root = Path(config["project_root"])
        readable_files = config.get("readable_files", [])
        editable_files = config.get("editable_files", [])
        task = config.get("task_description", "")

        output = []
        if task:
            output.append(f"# Task\n{task}\n")

        output.append("# Context Files\n")

        # Merge lists and deduplicate
        all_files = sorted(list(set(readable_files + editable_files)))

        for rel_path in all_files:
            file_path = project_root / rel_path

            # Metadata
            perms = []
            if rel_path in editable_files:
                perms.append("EDITABLE")
            else:
                perms.append("READ-ONLY")

            output.append(f"## File: {rel_path} ({', '.join(perms)})")

            try:
                if file_path.exists():
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    output.append("```")
                    output.append(content)
                    output.append("```\n")
                else:
                    output.append("(File does not exist yet)\n")
            except Exception as e:
                output.append(f"(Error reading file: {e})\n")

        return "\n".join(output)
    except Exception as e:
        return f"Error reading context: {str(e)}"


@mcp.tool()
def apply_patch(patch_content: str) -> str:
    """
    Applies a patch to the project.

    Args:
        patch_content: The markdown content containing code blocks.
                       Use Unified Diff format (@@ ... @@) for edits
                       and Full File content for new files.
                       Always include '# FILE: path/to/file' headers.
    """
    try:
        config = _load_config()
        editable_files = set(config.get("editable_files", []))
        project_root = Path(config["project_root"])

        # Change CWD to project root for patcher compatibility
        original_cwd = os.getcwd()
        os.chdir(project_root)

        try:
            # Parse patches
            patches = parse_llm_output(patch_content)
            if not patches:
                return "No valid patches found in input."

            # Validate permissions
            for patch in patches:
                # patch['file_path'] is likely relative or absolute. Normalize to relative.
                p_path = Path(patch["file_path"])
                if p_path.is_absolute():
                    try:
                        rel_path = p_path.relative_to(project_root).as_posix()
                    except ValueError:
                        return (
                            f"Error: Path {patch['file_path']} is outside project root."
                        )
                else:
                    rel_path = p_path.as_posix()

                # Safety check: Is this file allowed to be edited?
                if rel_path not in editable_files:
                    return f"Permission Denied: {rel_path} is Read-Only or not in the active context."

            # Apply
            modified = apply_patches(patches)

            return f"Successfully applied patches to: {', '.join(modified)}"

        finally:
            os.chdir(original_cwd)

    except Exception as e:
        return f"Error applying patch: {str(e)}"


@mcp.tool()
def run_verification() -> str:
    """
    Runs the verification command (tests, linter, etc.) defined for this task.
    Returns the stdout and stderr.
    """
    try:
        config = _load_config()
        command = config.get("verification_command")
        project_root = config["project_root"]

        if not command:
            return "No verification command defined in Ralph configuration."

        result = subprocess.run(
            command,
            cwd=project_root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

        output = [
            f"$ {command}",
            f"Exit Code: {result.returncode}",
            "--- STDOUT ---",
            result.stdout,
            "--- STDERR ---",
            result.stderr,
        ]
        return "\n".join(output)

    except subprocess.TimeoutExpired:
        return (
            f"$ {command}\n"
            "Error: Command timed out after 300 seconds.\n"
            "Consider breaking the verification into smaller steps."
        )
    except Exception as e:
        return f"Error running verification: {str(e)}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
