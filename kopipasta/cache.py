import json
import os
from pathlib import Path
from typing import List, Tuple, Optional

# Define FileTuple for type hinting
FileTuple = Tuple[str, bool, Optional[List[str]], str]


def get_cache_file_path() -> Path:
    """Gets the cross-platform path to the cache file for the last selection."""
    cache_dir = Path.home() / ".cache" / "kopipasta"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_selection_cache_file() -> Path:
    return get_cache_file_path() / "last_selection.json"


def get_task_cache_file() -> Path:
    return get_cache_file_path() / "last_task.txt"


def save_selection_to_cache(files_to_include: List[FileTuple]):
    """Saves the list of selected file relative paths to the cache."""
    cache_file = get_selection_cache_file()
    relative_paths = sorted([os.path.relpath(f[0]) for f in files_to_include])
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(relative_paths, f, indent=2)
    except IOError as e:
        print(f"\nWarning: Could not save selection to cache: {e}")


def load_selection_from_cache() -> List[str]:
    """Loads the list of selected files from the cache file."""
    cache_file = get_selection_cache_file()
    if not cache_file.exists():
        return []
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            paths = json.load(f)
            # Filter out paths that no longer exist
            return [p for p in paths if os.path.exists(p)]
    except (IOError, json.JSONDecodeError) as e:
        print(f"\nWarning: Could not load previous selection from cache: {e}")
        return []


def save_task_to_cache(task_description: str):
    """Saves the task description to cache."""
    cache_file = get_task_cache_file()
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(task_description)
    except IOError as e:
        print(f"\nWarning: Could not save task to cache: {e}")


def load_task_from_cache() -> Optional[str]:
    """Loads the task description from cache."""
    cache_file = get_task_cache_file()
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return f.read()
    except IOError as e:
        print(f"\nWarning: Could not load previous task from cache: {e}")
        return None


def clear_cache():
    """Clears all cached data (selection and task)."""
    try:
        selection_file = get_selection_cache_file()
        if selection_file.exists():
            os.remove(selection_file)

        task_file = get_task_cache_file()
        if task_file.exists():
            os.remove(task_file)
    except OSError as e:
        print(f"\nWarning: Could not clear cache: {e}")
