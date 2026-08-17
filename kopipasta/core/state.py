"""Session state directory resolution — where conversations and leases live.

State used to live at the hardcoded `<project_root>/.kopipasta/`. That had
four concrete costs in real repositories:
  1. Session._ensure_dir modified tracked .gitignore files on first use.
  2. It wrote .gitignore files into directories that had no git at all.
  3. `git clean -xfd` wiped .kopipasta/, destroying transcripts and active
     provider-side cache leases (cache.json) billed per token-hour.
  4. Repo-wide tooling that skips .gitignore (Docker builds, npm pack, tsc,
     IDE indexes) walked session records as if they were source.

Resolving state to `.git/kopipasta/` eliminates all four costs while keeping
the store relative to project_root, ensuring CLI tools (cat, rg) and agent
sandboxes constrained to the workspace boundary continue to work seamlessly.

$GIT_COMMON_DIR is deliberately NOT consulted: for a linked worktree, `.git`
points at `.git/worktrees/<name>`, giving that worktree its OWN session store.
A session's fixed prefix is a byte-exact render of the files in one checkout at
turn 1, and `apply` enforces an editable set of paths in that checkout. Sharing
one store across worktrees on different branches would hand turn 2 a prefix
describing files that are not there.

Relative literal paths resolve against `project_root` and NOT `os.getcwd()`:
kopipasta is routinely run from subdirectories, and cwd-relative paths would
create fragmented state stores across subdirectories.

XDG state uses XDG state directories ($XDG_STATE_HOME / ~/.local/state), never
`~/.cache`: cache directories may be purged at any time, which would destroy
live cache.json leases while Google/Anthropic continues to bill for them.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from kopipasta.cache import get_project_key
from kopipasta.core.config import config_path as default_config_path
from kopipasta.core.config import load_toml
from kopipasta.core.errors import UsageError

SRC_FLAG = "--state-dir"
SRC_ENV = "KOPIPASTA_STATE_DIR"
SRC_CONFIG = "config.toml [state] dir"
SRC_DEFAULT = "built-in default"


def _is_path_syntax(val: str) -> bool:
    """Check if a string contains explicit path syntax.

    A value is treated as a path when it begins with a dot (./state, ../shared,
    .cache), begins with a tilde (~/state), contains a POSIX or Windows path
    separator, is absolute, or starts with a Windows drive letter (e.g. C:state).
    Single bare words without path syntax are rejected to prevent typos for known
    keywords (e.g. 'gti') from silently creating directories in the worktree.
    """
    if val.startswith((".", "~")):
        return True
    if "/" in val or "\\" in val:
        return True
    if os.path.isabs(val):
        return True
    if len(val) >= 2 and val[0].isalpha() and val[1] == ":":
        return True
    return False


def _is_subpath(path: str, parent: str) -> bool:
    """Check if path is identical to or contained within parent directory."""
    try:
        p = Path(path).resolve()
        r = Path(parent).resolve()
        return p == r or p.is_relative_to(r)
    except (ValueError, RuntimeError):
        pass
    try:
        p_abs = os.path.abspath(path)
        r_abs = os.path.abspath(parent)
        rel = os.path.relpath(p_abs, r_abs)
        return not rel.startswith("..") and not os.path.isabs(rel)
    except (ValueError, OSError):
        return False


@dataclass(frozen=True)
class StateRoot:
    project_root: str  # absolute; what find_project_root() returned
    path: str  # absolute path of the state directory
    source: str  # provenance, one of the SRC_* constants below
    kind: str  # "git" | "repo" | "xdg" | "temp" | "explicit"

    @property
    def in_worktree(self) -> bool:
        """True when `path` is inside `project_root`. Governs display formatting,
        determining whether the path can be presented as repo-relative."""
        return _is_subpath(self.path, self.project_root)

    @property
    def needs_gitignore(self) -> bool:
        """True when the state directory must be ignored in .gitignore.

        A directory outside the project root never needs ignoring, and a project
        with no git repository at all has no use for a .gitignore file. When git
        is present, git unconditionally ignores everything inside its own metadata
        directory (.git/), so only state paths in the worktree outside .git/
        require an explicit ignore rule.
        """
        if not self.in_worktree:
            return False

        has_git = bool(os.environ.get("GIT_DIR", "").strip()) or os.path.exists(
            os.path.join(self.project_root, ".git")
        )
        if not has_git:
            return False

        git_dir = _find_git_dir(self.project_root, os.environ)
        if not git_dir:
            # Something git-shaped is present but its git directory could not
            # be resolved (e.g. a corrupt or unreadable .git file). Err on the
            # side of caution: a redundant .gitignore line in a broken repo is
            # harmless, whereas missing one leaks transcripts and leases.
            return True

        return not _is_subpath(self.path, git_dir)

    @property
    def display(self) -> str:
        """Repo-relative with forward slashes when in_worktree, else absolute.
        This is what goes in the --json envelope and in help text."""
        if self.in_worktree:
            try:
                rel = os.path.relpath(self.path, self.project_root)
                return rel.replace(os.sep, "/")
            except (ValueError, OSError):
                pass
        return self.path


def _is_writable(path: str) -> bool:
    """Check if the nearest existing ancestor of path is writable.

    os.access with W_OK is close to meaningless for directories on Windows (it
    checks read-only attributes rather than ACLs), so this probe mostly buys us
    the POSIX read-only-checkout and CI cases. It must never be the reason a
    run fails, so any unexpected exception means 'usable' and moves on.
    """
    try:
        curr = Path(path).resolve()
        while not curr.exists() and curr.parent != curr:
            curr = curr.parent
        if curr.exists():
            return os.access(str(curr), os.W_OK)
    except Exception:
        pass
    return True


def _find_git_dir(project_root: str, env: Mapping[str, str]) -> Optional[str]:
    git_dir_env = env.get("GIT_DIR")
    if git_dir_env and git_dir_env.strip():
        expanded = os.path.expanduser(git_dir_env.strip())
        if not os.path.isabs(expanded):
            expanded = os.path.normpath(os.path.join(project_root, expanded))
        return os.path.abspath(expanded)

    dot_git = os.path.join(project_root, ".git")
    if os.path.isdir(dot_git):
        return os.path.abspath(dot_git)

    if os.path.isfile(dot_git):
        try:
            with open(dot_git, encoding="utf-8") as fh:
                content = fh.read().strip()
            if content.startswith("gitdir:"):
                raw_path = content[len("gitdir:") :].strip()
                if raw_path:
                    expanded = os.path.expanduser(raw_path)
                    if not os.path.isabs(expanded):
                        expanded = os.path.normpath(
                            os.path.join(project_root, expanded)
                        )
                    return os.path.abspath(expanded)
        except (OSError, UnicodeDecodeError):
            return None

    return None


def _xdg_state_dir(project_root: str, env: Mapping[str, str]) -> Optional[str]:
    key = get_project_key(project_root)
    xdg_env = env.get("XDG_STATE_HOME")
    if xdg_env and xdg_env.strip():
        base = os.path.expanduser(xdg_env.strip())
        return os.path.abspath(os.path.join(base, "kopipasta", "projects", key))

    if os.name == "nt":
        local_app_data = env.get("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            base = local_app_data.strip()
            return os.path.abspath(
                os.path.join(base, "kopipasta", "state", "projects", key)
            )

    try:
        home = Path.home()
    except RuntimeError:
        return None

    return os.path.abspath(
        os.path.join(str(home), ".local", "state", "kopipasta", "projects", key)
    )


def _temp_state_dir(project_root: str) -> str:
    key = get_project_key(project_root)
    return os.path.abspath(os.path.join(tempfile.gettempdir(), "kopipasta-state", key))


def resolve_state_root(
    project_root: str,
    override: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> StateRoot:
    """Resolve where session state lives, recording provenance and state kind."""
    environ = os.environ if env is None else env
    abs_root = os.path.abspath(project_root)

    raw_value: Optional[str] = None
    source: Optional[str] = None

    if override is not None and override.strip():
        raw_value = override.strip()
        source = SRC_FLAG
    elif (environ.get(SRC_ENV) or "").strip():
        raw_value = environ[SRC_ENV].strip()
        source = SRC_ENV
    else:
        cfg_p = config_path if config_path is not None else default_config_path()
        try:
            cfg_data = load_toml(cfg_p)
        except Exception:
            cfg_data = {}
        state_section = cfg_data.get("state")
        if isinstance(state_section, dict):
            dir_val = state_section.get("dir")
            if isinstance(dir_val, str) and dir_val.strip():
                raw_value = dir_val.strip()
                source = SRC_CONFIG

    if raw_value is not None and source is not None:
        if raw_value == "git":
            git_dir = _find_git_dir(abs_root, environ)
            if not git_dir:
                raise UsageError(
                    "state directory is configured as 'git', but no git repository was"
                    " found.",
                    detail=f"Resolved from {source} for project root {abs_root}.",
                    hint=(
                        "Use 'repo' for a local .kopipasta/ directory, 'xdg' for"
                        " user state,\nor pass a literal path."
                    ),
                )
            return StateRoot(
                project_root=abs_root,
                path=os.path.abspath(os.path.join(git_dir, "kopipasta")),
                source=source,
                kind="git",
            )
        if raw_value == "repo":
            return StateRoot(
                project_root=abs_root,
                path=os.path.abspath(os.path.join(abs_root, ".kopipasta")),
                source=source,
                kind="repo",
            )
        if raw_value == "xdg":
            xdg_path = _xdg_state_dir(abs_root, environ)
            if xdg_path:
                return StateRoot(
                    project_root=abs_root,
                    path=os.path.abspath(xdg_path),
                    source=source,
                    kind="xdg",
                )
            return StateRoot(
                project_root=abs_root,
                path=_temp_state_dir(abs_root),
                source=source,
                kind="temp",
            )
        if not _is_path_syntax(raw_value):
            if source == SRC_FLAG:
                msg = f"{SRC_FLAG} was given unrecognised keyword {raw_value!r}."
            elif source == SRC_ENV:
                msg = (
                    f"{SRC_ENV} is set to {raw_value!r}, which is not a"
                    " recognized keyword."
                )
            elif source == SRC_CONFIG:
                msg = (
                    f"{SRC_CONFIG} is set to {raw_value!r}, which is not a"
                    " recognized keyword."
                )
            else:
                msg = (
                    f"unrecognised state directory keyword {raw_value!r} from {source}."
                )
            raise UsageError(
                msg,
                detail=f"Resolved from {source} for project root {abs_root}.",
                hint=(
                    "Accepted keywords are 'git', 'repo', 'xdg'.\n"
                    f"To specify a relative path named '{raw_value}', prefix it"
                    f" with './' (e.g. './{raw_value}')."
                ),
            )

        expanded = os.path.expanduser(raw_value)
        if not os.path.isabs(expanded):
            target_path = os.path.abspath(os.path.join(abs_root, expanded))
        else:
            target_path = os.path.abspath(expanded)
        return StateRoot(
            project_root=abs_root,
            path=target_path,
            source=source,
            kind="explicit",
        )

    # Default fallback chain
    # 1. "git", if git directory found AND writable
    git_dir = _find_git_dir(abs_root, environ)
    if git_dir and _is_writable(git_dir):
        return StateRoot(
            project_root=abs_root,
            path=os.path.abspath(os.path.join(git_dir, "kopipasta")),
            source=SRC_DEFAULT,
            kind="git",
        )

    # 2. "repo", if project_root is writable
    if _is_writable(abs_root):
        return StateRoot(
            project_root=abs_root,
            path=os.path.abspath(os.path.join(abs_root, ".kopipasta")),
            source=SRC_DEFAULT,
            kind="repo",
        )

    # 3. "xdg", if a home or XDG_STATE_HOME is available
    xdg_path = _xdg_state_dir(abs_root, environ)
    if xdg_path and _is_writable(xdg_path):
        return StateRoot(
            project_root=abs_root,
            path=os.path.abspath(xdg_path),
            source=SRC_DEFAULT,
            kind="xdg",
        )

    # 4. "temp"
    return StateRoot(
        project_root=abs_root,
        path=_temp_state_dir(abs_root),
        source=SRC_DEFAULT,
        kind="temp",
    )
