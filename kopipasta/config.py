import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from kopipasta.file import read_file_contents


def read_env_file() -> Dict[str, str]:
    """Reads .env file from the current directory."""
    env_vars = {}
    if os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as env_file:
                for line in env_file:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line:
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            if value:
                                env_vars[key] = value
        except Exception as e:
            print(f"Warning: Could not read .env file: {e}")
    return env_vars


def read_gitignore() -> List[str]:
    """Reads .gitignore and returns a list of patterns."""
    default_ignore_patterns = [
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "dist",
        ".idea",
        "__pycache__",
        "*.pyc",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".vscode",
        ".vite",
        ".terraform",
        "output",
        "poetry.lock",
        "package-lock.json",
        ".env",
        "*.log",
        "*.bak",
        "*.swp",
        "*.swo",
        "*.tmp",
        "tmp",
        "temp",
        "logs",
        "build",
        "target",
        ".DS_Store",
        "Thumbs.db",
    ]
    gitignore_patterns = default_ignore_patterns.copy()

    if os.path.exists(".gitignore"):
        print(".gitignore detected.")
        try:
            with open(".gitignore", "r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        gitignore_patterns.append(line)
        except Exception as e:
            print(f"Warning: Could not read .gitignore: {e}")

    return gitignore_patterns


def get_global_profile_path() -> Path:
    """Returns the path to the global user profile (AI Identity)."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "kopipasta" / "ai_profile.md"
    else:
        return Path.home() / ".config" / "kopipasta" / "ai_profile.md"


def read_global_profile() -> Optional[str]:
    """Reads the global profile content."""
    config_path = get_global_profile_path()
    if config_path.exists():
        return read_file_contents(str(config_path))
    return None


def open_profile_in_editor():
    """Opens the global profile in the default editor, creating it if needed."""
    config_path = get_global_profile_path()

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        default_content = (
            "# Global AI Profile\n"
            "This file is injected into the top of every prompt.\n"
            "Use it for your identity and global preferences.\n\n"
            "- I am a Senior Python Developer.\n"
            "- I prefer functional programming patterns where possible.\n"
            "- I use VS Code on MacOS.\n"
            "- Always type annotate Python code.\n"
        )
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(default_content)
            print(f"Created new profile at: {config_path}")
        except IOError as e:
            print(f"Error creating profile: {e}")
            return

    editor = os.environ.get("EDITOR", "code" if shutil.which("code") else "vim")

    if sys.platform == "win32":
        os.startfile(config_path)
    elif sys.platform == "darwin":
        subprocess.call(("open", config_path))
    else:
        subprocess.call((editor, config_path))


def read_project_context(project_root: str) -> Optional[str]:
    """Reads AI_CONTEXT.md from project root."""
    path = os.path.join(project_root, "AI_CONTEXT.md")
    if os.path.exists(path):
        return read_file_contents(path)
    return None


def read_session_state(project_root: str) -> Optional[str]:
    """Reads AI_SESSION.md from project root."""
    path = os.path.join(project_root, "AI_SESSION.md")
    if os.path.exists(path):
        return read_file_contents(path)
    return None


def read_fix_command(project_root: str) -> str:
    """
    Reads the fix command for the 'x' hotkey.

    Resolution order:
    1. AI_CONTEXT.md HTML comment: <!-- KOPIPASTA_FIX_CMD: your command here -->
    2. .git/hooks/pre-commit (platform-aware executable check)
    3. git diff --check HEAD (universal fallback)
    """
    # 1. Parse AI_CONTEXT.md for explicit config
    context_path = os.path.join(project_root, "AI_CONTEXT.md")
    if os.path.exists(context_path):
        try:
            content = read_file_contents(context_path)
            match = re.search(
                r"<!--\s*KOPIPASTA_FIX_CMD:\s*(.+?)\s*-->", content
            )
            if match:
                return match.group(1).strip()
        except Exception:
            pass

    # 2. Check for git pre-commit hook
    hook_path = os.path.join(project_root, ".git", "hooks", "pre-commit")
    if os.path.exists(hook_path):
        if platform.system() == "Windows":
            # On Windows, hooks need to be invoked through the shell
            # (git bash / sh). Check the shebang or just invoke via sh.
            git_sh = shutil.which("sh") or shutil.which("bash")
            if git_sh:
                return f"{git_sh} {hook_path}"
        else:
            # POSIX: just needs to be executable
            if os.access(hook_path, os.X_OK):
                return hook_path

    # 3. Universal fallback
    return "git diff --check HEAD"