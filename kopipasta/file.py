import fnmatch
import os
from typing import List, Optional, Tuple
from pathlib import Path

FileTuple = Tuple[str, bool, Optional[List[str]], str]

# --- Cache for .gitignore patterns ---
# Key: Directory path
# Value: List of patterns
_gitignore_cache: dict[str, list[str]] = {}

def _read_gitignore_patterns(gitignore_path: str) -> list[str]:
    """Reads patterns from a single .gitignore file and caches them."""
    if gitignore_path in _gitignore_cache:
        return _gitignore_cache[gitignore_path]
    if not os.path.isfile(gitignore_path):
        _gitignore_cache[gitignore_path] = []
        return []
    patterns = []
    try:
        with open(gitignore_path, 'r', encoding='utf-8') as f:
            for line in f:
                stripped_line = line.strip()
                if stripped_line and not stripped_line.startswith('#'):
                    patterns.append(stripped_line)
    except IOError:
        pass
    _gitignore_cache[gitignore_path] = patterns
    return patterns

def is_ignored(path: str, default_ignore_patterns: list[str], project_root: Optional[str] = None) -> bool:
    """
    Checks if a path should be ignored based on default patterns and .gitignore files.
    Searches for .gitignore from the path's location up to the project_root.
    """
    path_abs = os.path.abspath(path)
    if project_root is None:
        project_root = os.getcwd()
    project_root_abs = os.path.abspath(project_root)

    # --- Step 1: Gather all patterns from all relevant .gitignore files ---
    all_patterns = set(default_ignore_patterns)
    
    # Determine the directory to start searching for .gitignore files
    search_start_dir = path_abs if os.path.isdir(path_abs) else os.path.dirname(path_abs)

    current_dir = search_start_dir
    while True:
        gitignore_path = os.path.join(current_dir, ".gitignore")
        patterns_from_file = _read_gitignore_patterns(gitignore_path)

        if patterns_from_file:
            gitignore_dir_rel = os.path.relpath(current_dir, project_root_abs)
            if gitignore_dir_rel == '.': gitignore_dir_rel = ''

            for p in patterns_from_file:
                # Patterns with a '/' are relative to the .gitignore file's location.
                # We construct a new pattern relative to the project root.
                if '/' in p:
                    all_patterns.add(os.path.join(gitignore_dir_rel, p.lstrip('/')))
                else:
                    # Patterns without a '/' (e.g., `*.log`) can match anywhere.
                    all_patterns.add(p)

        if not current_dir.startswith(project_root_abs) or current_dir == project_root_abs:
            break
        parent = os.path.dirname(current_dir)
        if parent == current_dir: break
        current_dir = parent

    # --- Step 2: Check the path and its parents against the patterns ---
    try:
        path_rel_to_root = os.path.relpath(path_abs, project_root_abs)
    except ValueError:
        return False # Path is outside the project root

    path_parts = Path(path_rel_to_root).parts

    for pattern in all_patterns:
        # Check against basename for simple wildcards (e.g., `*.log`, `__pycache__`)
        # This is a primary matching mechanism.
        if fnmatch.fnmatch(os.path.basename(path_abs), pattern):
            return True
            
        # Check the full path and its parent directories against the pattern.
        # This handles directory ignores (`node_modules/`) and specific path ignores (`src/*.tmp`).
        for i in range(len(path_parts)):
            current_check_path = os.path.join(*path_parts[:i+1])
            
            # Handle directory patterns like `node_modules/`
            if pattern.endswith('/'):
                if fnmatch.fnmatch(current_check_path, pattern.rstrip('/')):
                    return True
            # Handle full path patterns
            else:
                if fnmatch.fnmatch(current_check_path, pattern):
                    return True
    
    return False

def read_file_contents(file_path):
    try:
        with open(file_path, 'r') as file:
            return file.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return ""

def is_binary(file_path):
    try:
        with open(file_path, 'rb') as file:
            chunk = file.read(1024)
            if b'\0' in chunk:
                return True
            if file_path.lower().endswith(('.json', '.csv')):
                return False
            return False
    except IOError:
        return False

def get_human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0

def is_large_file(file_path, threshold=102400):
    return os.path.getsize(file_path) > threshold