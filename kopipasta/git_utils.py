import os
import shutil
import subprocess


def add_to_gitignore(project_root: str, entry: str) -> bool:
    """Appends an entry to the .gitignore file if not already present."""
    path = os.path.join(project_root, ".gitignore")
    content = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

    if entry not in content.splitlines():
        with open(path, "a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")
        return True
    return False


def check_session_gitignore_status(project_root: str) -> bool:
    """
    Checks if AI_SESSION.md is ignored by git.
    Returns True if ignored (Safe), False if not ignored (Warning needed).
    Returns True if file doesn't exist or git is not present (Skipping check).
    """
    git_executable = shutil.which("git")
    if not git_executable:
        return True

    try:
        # git check-ignore returns 0 if ignored, 1 if not ignored
        result = subprocess.run(
            [git_executable, "check-ignore", "AI_SESSION.md"],
            cwd=project_root,
            capture_output=True,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Warning: Could not check git status: {e}")
        return False