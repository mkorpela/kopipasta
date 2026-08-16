"""The rendering primitives both surfaces are built from — spec §13.

These lived in `kopipasta/prompt.py`, which is the TUI's module: it owns the
Jinja template, the `$EDITOR` launcher and the clipboard-bound composition. So
`core/context.py` reaching in for four pure helpers made the headless agent
path import `jinja2` and `prompt_toolkit` — the template engine and the
interactive task editor, neither of which `ask` can use. 212 modules of
terminal UI loaded to post a payload to an API.

Nothing broke, which is the problem: the coupling was invisible and ran the
wrong way. The dependency is surface -> core, and these four are core.

Everything here is pure, in the sense that matters for a shared renderer: it
returns strings and never decides where they go. `handle_env_variables` is the
one function with a side effect, and it narrates to **stderr** rather than
printing — stdout means different things on the two surfaces (a prompt to
paste, a JSON artifact to parse), so a shared helper must not write to it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from kopipasta.file import extract_symbols, is_ignored
from kopipasta.interaction import use_default_without_human
from kopipasta.output import narrate


def get_language_for_file(file_path: str) -> str:
    extension = os.path.splitext(file_path)[1].lower()
    language_map = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "jsx",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".json": "json",
        ".md": "markdown",
        ".sql": "sql",
        ".sh": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".go": "go",
        ".toml": "toml",
        ".c": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".h": "cpp",
        ".hpp": "cpp",
    }
    return language_map.get(extension, "")


def get_file_snippet(file_path: str, max_lines: int = 50, max_bytes: int = 4096) -> str:
    snippet = ""
    byte_count = 0
    try:
        with open(file_path, encoding="utf-8", errors="replace") as file:
            for i, line in enumerate(file):
                if i >= max_lines or byte_count >= max_bytes:
                    break
                snippet += line
                byte_count += len(line.encode("utf-8"))
    except Exception as e:
        return f"<Error reading snippet: {e}>"
    return snippet


# --------------------------------------------------------------------------
# the structure tree
# --------------------------------------------------------------------------
def _build_dir_dict(
    dir_path: str, ignore_patterns: List[str], map_files_set: set
) -> Dict[str, Any]:
    """Recursively build a nested dict representing a directory's contents."""
    result: Dict[str, Any] = {}
    try:
        items = sorted(os.listdir(dir_path))
    except (PermissionError, FileNotFoundError):
        return result

    dirs = []
    files = []
    for item in items:
        item_path = os.path.join(dir_path, item)
        if is_ignored(item_path, ignore_patterns):
            continue
        if os.path.isdir(item_path):
            dirs.append(item)
        elif os.path.isfile(item_path):
            files.append(item)

    for d in dirs:
        result[d] = _build_dir_dict(
            os.path.join(dir_path, d), ignore_patterns, map_files_set
        )

    for f in files:
        file_abs = os.path.abspath(os.path.join(dir_path, f))
        if file_abs in map_files_set:
            result[f] = extract_symbols(file_abs)
        else:
            result[f] = []

    return result


def get_project_structure(
    ignore_patterns: List[str],
    search_paths: Optional[List[str]] = None,
    map_files: Optional[List[str]] = None,
) -> str:
    """Return a minified JSON string describing the project file tree.

    Leaf nodes are lists of symbol strings (from extract_symbols).
    Non-Python files get an empty list [].
    """
    if not search_paths:
        search_paths = ["."]

    map_files_set = set(os.path.abspath(p) for p in (map_files or []))
    structure: Dict[str, Any] = {}

    for start_path in search_paths:
        if os.path.isfile(start_path):
            if not is_ignored(start_path, ignore_patterns):
                name = os.path.basename(start_path)
                start_abs = os.path.abspath(start_path)
                structure[name] = (
                    extract_symbols(start_path) if start_abs in map_files_set else []
                )
            continue

        dir_contents = _build_dir_dict(
            os.path.abspath(start_path), ignore_patterns, map_files_set
        )
        structure.update(dir_contents)

    return json.dumps(structure, separators=(",", ":"))


# --------------------------------------------------------------------------
# secrets — spec §14
# --------------------------------------------------------------------------
def _is_masking_candidate(value: str) -> bool:
    """
    Determines if an environment variable value is distinct enough to be worth masking.
    Filters out common configuration values, short strings, and integers to prevent
    aggressive false positives (e.g., masking '1', 'true', 'dev').
    """
    if not value:
        return False

    val_lower = value.lower().strip()

    # Common values that appear frequently in code and shouldn't be masked
    common_values = {
        # Booleans and Nulls
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "1",
        "0",
        "null",
        "none",
        "undefined",
        "nil",
        # Environments
        "development",
        "production",
        "test",
        "staging",
        "dev",
        "prod",
        "local",
        # Log levels
        "debug",
        "info",
        "warn",
        "warning",
        "error",
        "trace",
        "fatal",
        # Network
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        # Common credentials placeholders
        "password",
        "secret",
        "key",
        "token",
        "admin",
        "root",
        "user",
        # Misc
        "public",
        "private",
        "default",
        "utf-8",
    }

    if val_lower in common_values:
        return False

    # Ignore short values (likely false positives in text)
    if len(value) < 4:
        return False

    # Ignore short numeric values (ports, counts, simple IDs)
    # Most secrets (API keys, etc.) are longer or mixed alphanumeric.
    if value.isdigit() and len(value) < 6:
        return False

    return True


def handle_env_variables(
    content: str,
    env_vars: Dict[str, str],
    decisions_cache: Optional[Dict[str, str]] = None,
) -> str:
    if decisions_cache is None:
        decisions_cache = {}

    detected_vars = []
    for key, value in env_vars.items():
        # Only detect if value is not empty, present in content, AND is a candidate
        if value and value in content and _is_masking_candidate(value):
            detected_vars.append((key, value))
    if not detected_vars:
        return content

    undecided_vars = [item for item in detected_vars if item[0] not in decisions_cache]

    if undecided_vars:
        # stderr, not stdout. This runs inside the renderer both surfaces
        # share, and stdout is the prompt on one and a JSON object on the
        # other — a line of narration landing in either is a corrupted
        # artifact, not a note to the reader.
        narrate("Detected environment variables:")
        for key, value in undecided_vars:
            narrate(f"- {key}={value}")

        # No human: mask, don't ask and don't fail. Leaking a secret to a
        # third-party API is worse than a masked value, and refusing to run at
        # all would make kopipasta useless in CI for no safety gain (spec
        # §12). This is the one prompt where the conservative answer is
        # unambiguous, so it is the one prompt that gets defaulted.
        headless = use_default_without_human(
            f"Handling {len(undecided_vars)} detected environment variable(s)",
            "masking every detected value",
        )

        for key, _value in undecided_vars:
            if headless:
                decisions_cache[key] = "m"
                continue
            while True:
                try:
                    choice = input(
                        f"How would you like to handle {key}? (m)ask / (s)kip / (k)eep: "
                    ).lower()
                except EOFError:
                    # stdin died mid-run. Fall back to the safe answer rather
                    # than spinning on a stream that will never yield again.
                    choice = "m"
                if choice in ["m", "s", "k"]:
                    break
                narrate("Invalid choice. Please enter 'm', 's', or 'k'.")
            decisions_cache[key] = choice

    for key, value in detected_vars:
        choice = decisions_cache.get(key, "k")
        if choice == "m":
            content = content.replace(value, "*" * len(value))
        elif choice == "s":
            content = content.replace(value, "[REDACTED]")
        # If 'k', we don't modify the content

    return content


__all__ = [
    "get_file_snippet",
    "get_language_for_file",
    "get_project_structure",
    "handle_env_variables",
]
