import ast
import fnmatch
import os
from typing import List, Optional, Tuple, Set
from pathlib import Path

FileTuple = Tuple[str, bool, Optional[List[str]], str]

# --- Caches ---
_gitignore_cache: dict[str, list[str]] = {}
_is_ignored_cache: dict[str, bool] = {}
_is_binary_cache: dict[str, bool] = {}

# --- Known File Extensions for is_binary ---
# Using sets for O(1) average time complexity lookups
TEXT_EXTENSIONS = {
    # Code
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".sh",
    ".bash",
    ".ps1",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".pl",
    ".pm",
    ".tcl",
    # Markup & Data
    ".html",
    ".htm",
    ".xml",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".md",
    ".txt",
    ".rtf",
    ".csv",
    ".tsv",
    ".sql",
    ".graphql",
    ".gql",
    # Config & Other
    ".gitignore",
    ".dockerfile",
    "dockerfile",
    ".env",
    ".properties",
    ".mdx",
}

BINARY_EXTENSIONS = {
    # Images
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tiff",
    ".ico",
    ".webp",
    ".svg",
    # Audio/Video
    ".mp3",
    ".wav",
    ".ogg",
    ".flac",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".mkv",
    # Archives
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    # Executables & Compiled
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".pyc",
    ".pyd",
    ".whl",
    # Databases & Other
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".lock",
    ".bak",
    ".swo",
    ".swp",
}


def _read_gitignore_patterns(gitignore_path: str) -> list[str]:
    """Reads patterns from a single .gitignore file and caches them."""
    if gitignore_path in _gitignore_cache:
        return _gitignore_cache[gitignore_path]
    if not os.path.isfile(gitignore_path):
        _gitignore_cache[gitignore_path] = []
        return []
    patterns = []
    try:
        with open(gitignore_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped_line = line.strip()
                if stripped_line and not stripped_line.startswith("#"):
                    patterns.append(stripped_line)
    except IOError:
        pass
    _gitignore_cache[gitignore_path] = patterns
    return patterns


def is_ignored(
    path: str, default_ignore_patterns: list[str], project_root: Optional[str] = None
) -> bool:
    """
    Checks if a path should be ignored by splitting patterns into fast (basename)
    and slow (full path) checks, with heavy caching and optimized inner loops.
    """
    path_abs = os.path.abspath(path)
    if path_abs in _is_ignored_cache:
        return _is_ignored_cache[path_abs]

    parent_dir = os.path.dirname(path_abs)
    if parent_dir != path_abs and _is_ignored_cache.get(parent_dir, False):
        _is_ignored_cache[path_abs] = True
        return True

    if project_root is None:
        project_root = os.getcwd()
    project_root_abs = os.path.abspath(project_root)

    basename_patterns, path_patterns = get_all_patterns(
        default_ignore_patterns, path_abs, project_root_abs
    )

    # --- Step 1: Fast check for basename patterns ---
    path_basename = os.path.basename(path_abs)
    for pattern in basename_patterns:
        if fnmatch.fnmatch(path_basename, pattern):
            _is_ignored_cache[path_abs] = True
            return True

    # --- Step 2: Optimized nested check for path patterns ---
    try:
        path_rel_to_root = os.path.relpath(path_abs, project_root_abs)
    except ValueError:
        _is_ignored_cache[path_abs] = False
        return False

    # Pre-calculate all path prefixes to check, avoiding re-joins in the loop.
    path_parts = Path(path_rel_to_root).parts
    path_prefixes = [
        os.path.join(*path_parts[:i]) for i in range(1, len(path_parts) + 1)
    ]

    # Pre-process patterns to remove trailing slashes once.
    processed_path_patterns = [p.rstrip("/") for p in path_patterns]

    for prefix in path_prefixes:
        for pattern in processed_path_patterns:
            if fnmatch.fnmatch(prefix, pattern):
                _is_ignored_cache[path_abs] = True
                return True

    _is_ignored_cache[path_abs] = False
    return False


def get_all_patterns(
    default_ignore_patterns, path_abs, project_root_abs
) -> Tuple[Set[str], Set[str]]:
    """
    Gathers all applicable ignore patterns, splitting them into two sets
    for optimized checking: one for basenames, one for full paths.
    """
    basename_patterns = set()
    path_patterns = set()

    for p in default_ignore_patterns:
        if "/" in p:
            path_patterns.add(p)
        else:
            basename_patterns.add(p)

    search_start_dir = (
        path_abs if os.path.isdir(path_abs) else os.path.dirname(path_abs)
    )

    current_dir = search_start_dir
    while True:
        gitignore_path = os.path.join(current_dir, ".gitignore")
        patterns_from_file = _read_gitignore_patterns(gitignore_path)

        if patterns_from_file:
            gitignore_dir_rel = os.path.relpath(current_dir, project_root_abs)
            if gitignore_dir_rel == ".":
                gitignore_dir_rel = ""

            for p in patterns_from_file:
                if "/" in p:
                    # Path patterns are relative to the .gitignore file's location
                    path_patterns.add(os.path.join(gitignore_dir_rel, p.lstrip("/")))
                else:
                    basename_patterns.add(p)

        if (
            not current_dir.startswith(project_root_abs)
            or current_dir == project_root_abs
        ):
            break
        parent = os.path.dirname(current_dir)
        if parent == current_dir:
            break
        current_dir = parent
    return basename_patterns, path_patterns


def read_file_contents(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()
    except (IOError, UnicodeDecodeError) as e:
        failure = f"Error reading {file_path}: {e}"
        print(failure)
        return f"<.. {failure} ..>"


def is_binary(file_path: str) -> bool:
    """
    Efficiently checks if a file is binary.

    The check follows a fast, multi-step process to minimize I/O:
    1. Checks a memory cache for a previously determined result.
    2. Checks the file extension against a list of known text file types.
    3. Checks the file extension against a list of known binary file types.
    4. As a last resort, reads the first 512 bytes of the file to check for
       a null byte, a common indicator of a binary file.
    """
    # Step 1: Check cache first for fastest response
    if file_path in _is_binary_cache:
        return _is_binary_cache[file_path]

    # Step 2: Fast check based on known text/binary extensions (no I/O)
    _, extension = os.path.splitext(file_path)
    extension = extension.lower()

    if extension in TEXT_EXTENSIONS:
        _is_binary_cache[file_path] = False
        return False
    if extension in BINARY_EXTENSIONS:
        _is_binary_cache[file_path] = True
        return True

    # Step 3: Fallback to content analysis for unknown extensions
    try:
        with open(file_path, "rb") as file:
            # Read a smaller chunk, 512 bytes is usually enough to find a null byte
            chunk = file.read(512)
            if b"\0" in chunk:
                _is_binary_cache[file_path] = True
                return True
            # If no null byte, assume it's a text file
            _is_binary_cache[file_path] = False
            return False
    except IOError:
        # If we can't open it, treat it as binary to be safe
        _is_binary_cache[file_path] = True
        return True


def get_human_readable_size(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0


def is_large_file(file_path, threshold=102400):
    return os.path.getsize(file_path) > threshold


def _normalize_method_name(name: str) -> str:
    """Convert dunder names like __init__ to init; leave others unchanged."""
    if name.startswith("__") and name.endswith("__") and len(name) > 4:
        return name[2:-2]
    return name


def extract_symbols(path: str) -> List[str]:
    """Extract top-level class and function symbols from a Python file.

    Returns a list of symbol strings:
      - "class ClassName(method1, method2)" for classes
      - "def func_name" for top-level functions/async functions
    Returns [] for non-Python files or on parse errors.
    """
    if not path.endswith(".py"):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except (SyntaxError, IOError, UnicodeDecodeError):
        return []

    symbols: List[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = [
                _normalize_method_name(child.name)
                for child in ast.iter_child_nodes(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if methods:
                symbols.append(f"class {node.name}({', '.join(methods)})")
            else:
                symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")

    return symbols
