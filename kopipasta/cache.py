import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

# Define FileTuple for type hinting
FileTuple = Tuple[str, bool, Optional[List[str]], str]

LEGACY_CACHE_FILES = ("last_selection.json", "last_map.json", "last_task.txt")

_REPLACE_ATTEMPTS = 5


def get_cache_root() -> Path:
    """The machine-wide cache root. Deliberately NOT inside the project.

    Putting per-project state in the project would need a .gitignore entry in
    every repo kopipasta touches, and breaks on read-only checkouts.
    """
    try:
        home = Path.home()
    except RuntimeError:
        # Path.home() raises when no home can be determined (unset HOME under
        # a service account, some CI images). A cache is a convenience; it
        # must never be the reason the tool cannot start.
        return Path(tempfile.gettempdir()) / "kopipasta-cache"
    return home / ".cache" / "kopipasta"


def find_project_root(start: Optional[str] = None) -> Path:
    """The repository root, falling back to cwd outside a repo.

    Keying on cwd alone would give `repo/` and `repo/src/` two separate
    caches for one project, so running kopipasta one directory down would
    silently look like a first run.
    """
    current = Path(start or os.getcwd()).resolve()
    for candidate in (current, *current.parents):
        # .git is a *file* in worktrees and submodules, so check existence
        # rather than is_dir().
        if (candidate / ".git").exists():
            return candidate
    return current


def _norm_for_key(path: str) -> str:
    path = os.path.normcase(path)
    if sys.platform == "darwin":
        # normcase is a no-op on POSIX, but the default macOS filesystem is
        # case-INsensitive: /Users/me/Repo and /Users/me/repo are one
        # directory and must not get two caches. Linux is genuinely
        # case-sensitive, so folding there would merge two real projects.
        path = path.lower()
    return path


def get_project_key(project_root: Optional[str] = None) -> str:
    """A stable per-project directory name: <slug>-<hash>.

    The hash is what provides isolation; the slug is there so the cache
    directory can be read by a human debugging it.
    """
    root = find_project_root(project_root)
    digest = hashlib.sha256(_norm_for_key(str(root)).encode("utf-8")).hexdigest()[:12]
    # The slug has to fold exactly as the digest does. It did not, so on a
    # case-insensitive filesystem one project could produce two different
    # directory NAMES -- Repo-<h> and REPO-<h> -- that agreed on the hash but
    # not on the path: the same split the hash exists to prevent. Path.resolve()
    # is no help; it expands symlinks but preserves whatever case the caller
    # typed. Only os.getcwd() canonicalises case, which is why this stayed
    # latent for the no-argument call and appears only when a root is passed in.
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", _norm_for_key(root.name)) or "root"
    return f"{slug[:40]}-{digest}"


def get_cache_file_path(project_root: Optional[str] = None) -> Path:
    """Gets the cache directory for THIS project.

    Was a single global directory shared by every project on the machine
    (spec 11.3). Because the files it holds store *relative* paths, that was
    not merely a clobber: running in repo B silently pre-selected repo B's
    own src/main.py because the path from repo A happened to exist there too,
    and leaked repo A's task description into repo B's prompt.
    """
    cache_dir = get_cache_root() / "projects" / get_project_key(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp = cache_dir / "project.json"
    if not stamp.exists():
        # Record which path this hash came from, so the directory is
        # debuggable and a collision would be visible rather than baffling.
        try:
            _atomic_write(
                stamp,
                json.dumps({"root": str(find_project_root(project_root))}, indent=2),
            )
        except OSError:
            pass
    return cache_dir


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then replace.

    Agents run things in parallel; two processes writing the same cache file
    could otherwise leave a half-written JSON document that the next read
    reports as a corruption warning.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows only: os.replace fails while another process (a
                # concurrent kopipasta, an indexer, antivirus) holds a handle
                # to the destination. The window is short, so a brief retry
                # turns a lost write into a slightly delayed one.
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        # Never leave a .tmp behind to accumulate in the user's cache dir.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_selection_cache_file(project_root: Optional[str] = None) -> Path:
    return get_cache_file_path(project_root) / "last_selection.json"


def get_map_cache_file(project_root: Optional[str] = None) -> Path:
    return get_cache_file_path(project_root) / "last_map.json"


def get_task_cache_file(project_root: Optional[str] = None) -> Path:
    return get_cache_file_path(project_root) / "last_task.txt"


def _to_stored(paths: List[str]) -> List[str]:
    """Paths are stored relative to the PROJECT ROOT, not the cwd.

    Storing them cwd-relative meant a selection saved from `repo/` could not
    be resolved from `repo/src/`, and vice versa.
    """
    root = find_project_root()
    return sorted(
        {os.path.relpath(os.path.abspath(p), root).replace(os.sep, "/") for p in paths}
    )


def _from_stored(stored: List[str]) -> List[str]:
    """Back to cwd-relative, which is what every caller expects (callers do
    os.path.abspath on these). Paths that no longer exist are dropped."""
    root = find_project_root()
    out = []
    for p in stored:
        absolute = os.path.normpath(os.path.join(root, p))
        if os.path.exists(absolute):
            out.append(os.path.relpath(absolute))
    return out


def save_selection_to_cache(files_to_include: List[FileTuple]):
    """Saves the list of selected file paths to this project's cache."""
    try:
        _atomic_write(
            get_selection_cache_file(),
            json.dumps(_to_stored([f[0] for f in files_to_include]), indent=2),
        )
    except OSError as e:
        print(f"\nWarning: Could not save selection to cache: {e}", file=sys.stderr)


def save_map_to_cache(map_files: List[str]):
    """Saves the list of mapped file paths to this project's cache."""
    try:
        _atomic_write(get_map_cache_file(), json.dumps(_to_stored(map_files), indent=2))
    except OSError as e:
        print(f"\nWarning: Could not save map selection to cache: {e}", file=sys.stderr)


def load_selection_from_cache() -> List[str]:
    """Loads the list of selected files from the cache file."""
    try:
        cache_file = get_selection_cache_file()
        if not cache_file.exists():
            return []
        with open(cache_file, encoding="utf-8") as f:
            return _from_stored(json.load(f))
    except (OSError, ValueError) as e:
        print(
            f"\nWarning: Could not load previous selection from cache: {e}",
            file=sys.stderr,
        )
        return []


def load_map_from_cache() -> List[str]:
    """Loads the list of mapped files from the cache file."""
    try:
        cache_file = get_map_cache_file()
        if not cache_file.exists():
            return []
        with open(cache_file, encoding="utf-8") as f:
            return _from_stored(json.load(f))
    except (OSError, ValueError) as e:
        print(
            f"\nWarning: Could not load previous map selection from cache: {e}",
            file=sys.stderr,
        )
        return []


def save_task_to_cache(task_description: str):
    """Saves the task description to cache."""
    try:
        _atomic_write(get_task_cache_file(), task_description)
    except OSError as e:
        print(f"\nWarning: Could not save task to cache: {e}", file=sys.stderr)


def load_task_from_cache() -> Optional[str]:
    """Loads the task description from cache."""
    try:
        cache_file = get_task_cache_file()
        if not cache_file.exists():
            return None
        with open(cache_file, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(
            f"\nWarning: Could not load previous task from cache: {e}", file=sys.stderr
        )
        return None


def clear_cache():
    """Clears cached data (selection, map, task) FOR THIS PROJECT ONLY.

    This used to wipe the shared global files, so finishing a session in one
    repo silently destroyed every other repo's saved selection.
    """
    try:
        for cache_file in (
            get_selection_cache_file(),
            get_map_cache_file(),
            get_task_cache_file(),
        ):
            if cache_file.exists():
                os.remove(cache_file)

        # An explicit clear is the one safe moment to sweep the pre-per-project
        # files. They are deliberately never *read*: they cannot be attributed
        # to a project, and guessing would recreate the bug they came from.
        for name in LEGACY_CACHE_FILES:
            legacy = get_cache_root() / name
            if legacy.exists():
                os.remove(legacy)
    except OSError as e:
        print(f"\nWarning: Could not clear cache: {e}", file=sys.stderr)
