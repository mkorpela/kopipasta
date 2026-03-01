import ast
import copy
import fnmatch
import os
from typing import List, Optional, Tuple, Set
from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_javascript
    import tree_sitter_typescript

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False

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


def _is_dunder(name: str) -> bool:
    """Return True for dunder names like __init__, __repr__."""
    return name.startswith("__") and name.endswith("__") and len(name) > 4


def _is_private(name: str) -> bool:
    """Return True for private names (starting with '_') that are not dunders."""
    return name.startswith("_") and not _is_dunder(name)


def _normalize_method_name(name: str) -> str:
    """Convert dunder names like __init__ to init; leave others unchanged."""
    if _is_dunder(name):
        return name[2:-2]
    return name


def _get_signature(node: ast.AST) -> str:
    """Unparse the signature of a class or function, omitting the body."""
    n = copy.copy(node)
    n.body = [ast.Pass()]
    if hasattr(n, "decorator_list"):
        n.decorator_list = []
    try:
        code = ast.unparse(n).replace("\r\n", "\n")
        suffix = ":\n    pass"
        if code.endswith(suffix):
            sig = code[: -len(suffix)]
        elif code.endswith(": pass"):
            sig = code[:-6]
        else:
            sig = code.split(":\n")[0]
        # Ensure single line (removes newlines from multiline signatures)
        return " ".join(sig.split())
    except Exception:
        # Fallback if unparse fails
        if isinstance(node, ast.ClassDef):
            return f"class {node.name}"
        return f"def {node.name}"


def _get_docstring_suffix(node: ast.AST) -> str:
    """Extract the first line of the docstring."""
    doc = ast.get_docstring(node)
    if doc:
        first_line = doc.strip().split("\n")[0].strip()
        if first_line:
            return f"  # {first_line}"
    return ""


def _extract_frontend_symbols(path: str) -> List[str]:
    if not HAS_TREE_SITTER:
        return []

    _, ext = os.path.splitext(path)
    ext = ext.lower()

    try:
        with open(path, "rb") as f:
            source_bytes = f.read()
    except IOError:
        return []

    try:
        if ext == ".ts":
            lang = tree_sitter.Language(tree_sitter_typescript.language_typescript())
        elif ext == ".tsx":
            lang = tree_sitter.Language(tree_sitter_typescript.language_tsx())
        else:
            lang = tree_sitter.Language(tree_sitter_javascript.language())

        parser = tree_sitter.Parser(lang)
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    symbols = []

    def _get_jsdoc_summary(node) -> str:
        prev = node.prev_named_sibling
        if prev and prev.type == "comment":
            text = (
                source_bytes[prev.start_byte : prev.end_byte]
                .decode("utf-8", errors="ignore")
                .strip()
            )
            for line in text.split("\n"):
                cleaned = line.strip("/*\t\r ")
                if cleaned and not cleaned.startswith("@"):
                    return f"  // {cleaned}"
        return ""

    def process_node(node, doc_node=None):
        if doc_node is None:
            doc_node = node

        def _find_func(n):
            if n.type in (
                "arrow_function",
                "function_expression",
                "function_declaration",
                "generator_function_expression",
                "generator_function_declaration",
            ):
                return n
            for child in n.named_children:
                res = _find_func(child)
                if res:
                    return res
            return None

        def _extract_func_sig(start_node, f_node, d_node):
            body = f_node.child_by_field_name("body")
            if not body:
                for child in f_node.children:
                    if child.type == "statement_block":
                        body = child
                        break
            if body:
                sig = (
                    source_bytes[start_node.start_byte : body.start_byte]
                    .decode("utf-8", errors="ignore")
                    .strip()
                )
                sig = " ".join(sig.split())
                if not sig.endswith("=>") and f_node.type == "arrow_function":
                    sig += " =>"
                doc = _get_jsdoc_summary(d_node)
                symbols.append(f"{sig}{doc}")

        if node.type in ("export_statement", "export_default_statement"):
            for child in node.named_children:
                process_node(child, doc_node=node)
            return

        if node.type in ("function_declaration", "generator_function_declaration"):
            body_node = None
            for child in node.children:
                if child.type == "statement_block":
                    body_node = child
                    break

            if body_node:
                sig = (
                    source_bytes[node.start_byte : body_node.start_byte]
                    .decode("utf-8", errors="ignore")
                    .strip()
                )
            else:
                sig = (
                    source_bytes[node.start_byte : node.end_byte]
                    .decode("utf-8", errors="ignore")
                    .strip()
                )

            sig = " ".join(sig.split())
            doc = _get_jsdoc_summary(doc_node)
            symbols.append(f"{sig}{doc}")

        elif node.type in ("lexical_declaration", "variable_declaration"):
            for decl in node.named_children:
                if decl.type == "variable_declarator":
                    value_node = decl.child_by_field_name("value")
                    if value_node:
                        func_node = _find_func(value_node)
                        if func_node:
                            _extract_func_sig(node, func_node, doc_node)

        elif node.type == "call_expression":
            func_node = _find_func(node)
            if func_node:
                _extract_func_sig(node, func_node, doc_node)

        elif node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            name = (
                source_bytes[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="ignore"
                )
                if name_node
                else "Anonymous"
            )

            heritage = ""
            for child in node.children:
                if child.type == "class_heritage":
                    h_text = (
                        source_bytes[child.start_byte : child.end_byte]
                        .decode("utf-8", errors="ignore")
                        .strip()
                    )
                    if h_text.startswith("extends "):
                        heritage = f"({h_text[8:].strip()})"
                    break

            methods = []
            for child in node.children:
                if child.type == "class_body":
                    for m in child.named_children:
                        if m.type == "method_definition":
                            m_name = m.child_by_field_name("name")
                            if m_name:
                                methods.append(
                                    source_bytes[
                                        m_name.start_byte : m_name.end_byte
                                    ].decode("utf-8", errors="ignore")
                                )

            doc = _get_jsdoc_summary(doc_node)
            sig = f"class {name}{heritage}"
            if methods:
                symbols.append(f"{sig} [{', '.join(methods)}]{doc}")
            else:
                symbols.append(f"{sig}{doc}")

        elif node.type == "interface_declaration":
            name_node = node.child_by_field_name("name")
            name = (
                source_bytes[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="ignore"
                )
                if name_node
                else ""
            )
            doc = _get_jsdoc_summary(doc_node)
            symbols.append(f"interface {name}{doc}")

        elif node.type == "type_alias_declaration":
            name_node = node.child_by_field_name("name")
            name = (
                source_bytes[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="ignore"
                )
                if name_node
                else ""
            )
            doc = _get_jsdoc_summary(doc_node)
            symbols.append(f"type {name}{doc}")

    for child in tree.root_node.named_children:
        process_node(child)

    return symbols


def extract_symbols(path: str) -> List[str]:
    """Extract top-level class and function symbols from supported files.

    Private names (starting with '_') are omitted; dunder names are kept
    and normalized (e.g. __init__ -> init).

    Returns a list of symbol strings:
      - "class ClassName(Base) [method1, method2]  # Docstring" for classes
      - "def func_name(arg: type) -> type  # Docstring" for top-level functions
    Returns [] for non-Python files or on parse errors.
    """
    if path.endswith((".js", ".jsx", ".ts", ".tsx")):
        return _extract_frontend_symbols(path)

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
                and not _is_private(child.name)
            ]
            sig = _get_signature(node)
            doc = _get_docstring_suffix(node)
            if methods:
                symbols.append(f"{sig} [{', '.join(methods)}]{doc}")
            else:
                symbols.append(f"{sig}{doc}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_private(node.name):
                sig = _get_signature(node)
                doc = _get_docstring_suffix(node)
                symbols.append(f"{sig}{doc}")

    return symbols
