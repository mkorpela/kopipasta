import os
import re
import subprocess
import json
from datetime import datetime
from typing import Optional, TypedDict

from kopipasta.git_utils import check_session_gitignore_status, add_to_gitignore

SESSION_FILENAME = "AI_SESSION.md"


class SessionMetadata(TypedDict):
    start_commit: str
    timestamp: str


class Session:
    """
    Domain Entity representing a working session.
    Encapsulates state (AI_SESSION.md), lifecycle, and git integration.
    """

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.path = os.path.join(project_root, SESSION_FILENAME)

    @property
    def is_active(self) -> bool:
        return os.path.exists(self.path)

    @property
    def content(self) -> str:
        if not self.is_active:
            return ""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return f.read()
        except IOError:
            return ""

    def get_metadata(self) -> Optional[SessionMetadata]:
        if not self.is_active:
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                first_line = f.readline()
                match = re.search(r"<!-- KOPIPASTA_METADATA (.+) -->", first_line)
                if match:
                    result: SessionMetadata = json.loads(match.group(1))
                    return result
        except Exception:
            pass
        return None

    def start(self, console_printer=print) -> bool:
        """
        Initializes a new session.
        Returns True if successful.
        """
        if self.is_active:
            console_printer(f"Session already active at {SESSION_FILENAME}.")
            return False

        if not self._check_git_status(console_printer):
            return False

        # --- Safety Check: Ensure ignored ---
        if not check_session_gitignore_status(self.project_root):
            # We assume the UI handled the confirmation prompt before calling this,
            # or we handle it here if we inject an interaction callback.
            # For simplicity in this domain class, we'll try to add it blindly
            # if the caller didn't, or rely on the caller to have checked.
            # Ideally, the Controller ensures this. We will just attempt to add it.
            add_to_gitignore(self.project_root, SESSION_FILENAME)

        head_hash = self._get_git_head()
        metadata = {
            "start_commit": head_hash or "NO_GIT",
            "timestamp": datetime.now().isoformat(),
        }

        file_content = (
            f"<!-- KOPIPASTA_METADATA {json.dumps(metadata)} -->\n"
            "# Current Working Session\n\n"
            "## Current Progress\n- [ ] Session Started\n\n"
            "## Next Steps\n- [ ] Define Task\n"
            "- [ ] Refactor / Simplify Code?\n"
        )

        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(file_content)
            return True
        except IOError as e:
            console_printer(f"Failed to create session file: {e}")
            return False

    def finish(self, squash: bool = False, console_printer=print) -> bool:
        """
        Ends the session. Deletes the session file and optionally squashes commits.
        """
        if not self.is_active:
            return False

        metadata = self.get_metadata()
        start_commit = metadata.get("start_commit") if metadata else None

        # 1. Delete File
        try:
            os.remove(self.path)
        except OSError as e:
            console_printer(f"Error deleting session file: {e}")
            return False

        # 2. Squash (Soft Reset)
        if squash and start_commit and start_commit != "NO_GIT":
            try:
                subprocess.run(
                    ["git", "reset", "--soft", start_commit],
                    cwd=self.project_root,
                    check=True,
                    capture_output=True,
                )
                return True
            except subprocess.CalledProcessError as e:
                console_printer(f"Squash failed: {e}")
                return False

        return True

    def auto_commit(self, message: str = "kopipasta: auto-checkpoint") -> bool:
        """
        Adds all changes (excluding session file if not ignored) and commits.
        """
        if not self._get_git_head():
            return False

        try:
            cmd = ["git", "add", "."]
            if not check_session_gitignore_status(self.project_root):
                cmd.append(f":!{SESSION_FILENAME}")

            subprocess.run(cmd, cwd=self.project_root, check=True, capture_output=True)

            # Check for staged changes
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=self.project_root
            )
            if result.returncode != 0:  # 1 means diff found (dirty)
                subprocess.run(
                    ["git", "commit", "--no-verify", "--no-gpg-sign", "-m", message],
                    cwd=self.project_root,
                    check=True,
                    capture_output=True,
                )
                return True
        except subprocess.CalledProcessError:
            pass
        return False

    def _get_git_head(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def _check_git_status(self, console_printer) -> bool:
        if not self._get_git_head():
            console_printer("Not a git repository. Cannot track session history.")
            return False

        # Check modifications
        try:
            subprocess.run(
                ["git", "diff", "--quiet"], cwd=self.project_root, check=True
            )
            subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self.project_root,
                check=True,
            )
            return True
        except subprocess.CalledProcessError:
            console_printer(
                "Warning: You have uncommitted changes. Commit or stash them before starting."
            )
            return False
