import os
import shutil
import subprocess
from pathlib import Path
import sys
from typing import Dict, List, Optional, Set, Tuple

from kopipasta.file import (
    FileTuple,
    get_human_readable_size,
    is_binary,
    is_ignored,
    is_large_file,
    read_file_contents,
)
from kopipasta.prompt import get_file_snippet, get_language_for_file
import kopipasta.import_parser as import_parser


def sanitize_string(text: str) -> str:
    """
    Ensures the string contains valid unicode code points, fixing surrogate pairs
    that might have been introduced by Windows terminal input.
    """
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except Exception:
        return text


def estimate_tokens(char_count: int) -> int:
    """
    Estimates token count based on character count.
    Code files (with whitespace/syntax) average ~3.6 characters per token.
    """
    return int(char_count / 3.6)


def print_char_count(count: int):
    token_estimate = estimate_tokens(count)
    print(
        f"\rCurrent prompt size: {count} characters (~ {token_estimate} tokens)",
        flush=True,
    )


def read_env_file() -> Dict[str, str]:
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


def add_to_gitignore(project_root: str, entry: str):
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


def get_global_profile_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "kopipasta" / "ai_profile.md"
    else:
        return Path.home() / ".config" / "kopipasta" / "ai_profile.md"


def read_global_profile() -> Optional[str]:
    """Reads ~/.config/kopipasta/ai_profile.md (XDG compliant only)."""
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


def check_session_gitignore_status(project_root: str) -> bool:
    """
    Checks if AI_SESSION.md is ignored by git.
    Returns True if ignored (Safe), False if not ignored (Warning needed).
    Returns True if file doesn't exist or git is not present (Skipping check).
    """
    session_path = os.path.join(project_root, "AI_SESSION.md")
    if not os.path.exists(session_path):
        return True

    git_executable = shutil.which("git")
    if not git_executable:
        return True

    try:
        # git check-ignore returns 0 if ignored, 1 if not ignored
        result = subprocess.run(
            [git_executable, "check-ignore", "AI_SESSION.md"],
            cwd=project_root,
            capture_output=True
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Warning: Could not check git status: {e}")
        return False


def propose_and_add_dependencies(
    file_just_added: str,
    project_root_abs: str,
    files_to_include: List[FileTuple],
    current_char_count: int,
) -> Tuple[List[FileTuple], int]:
    language = get_language_for_file(file_just_added)
    if language not in ["python", "typescript", "javascript", "tsx", "jsx"]:
        return [], 0

    print(f"Analyzing {os.path.relpath(file_just_added)} for local dependencies...")

    try:
        file_content = read_file_contents(file_just_added)
        if not file_content:
            return [], 0

        resolved_deps_abs: Set[str] = set()
        if language == "python":
            resolved_deps_abs = import_parser.parse_python_imports(
                file_content, file_just_added, project_root_abs
            )
        elif language in ["typescript", "javascript", "tsx", "jsx"]:
            resolved_deps_abs = import_parser.parse_typescript_imports(
                file_content, file_just_added, project_root_abs
            )

        included_paths = {os.path.abspath(f[0]) for f in files_to_include}
        suggested_deps = sorted(
            [
                dep
                for dep in resolved_deps_abs
                if os.path.abspath(dep) not in included_paths
                and os.path.abspath(dep) != os.path.abspath(file_just_added)
            ]
        )

        if not suggested_deps:
            print("No new local dependencies found.")
            return [], 0

        print(
            f"\nFound {len(suggested_deps)} new local {'dependency' if len(suggested_deps) == 1 else 'dependencies'}:"
        )
        for i, dep_path in enumerate(suggested_deps):
            print(f"  ({i+1}) {os.path.relpath(dep_path)}")

        while True:
            choice = input(
                "\nAdd dependencies? (a)ll, (n)one, or enter numbers (e.g. 1, 3-4): "
            ).lower()

            deps_to_add_paths = None
            if choice == "a":
                deps_to_add_paths = suggested_deps
                break
            if choice == "n":
                deps_to_add_paths = []
                print(f"Skipped {len(suggested_deps)} dependencies.")
                break

            try:
                selected_indices = set()
                parts = choice.replace(" ", "").split(",")
                if all(p.strip() for p in parts):
                    for part in parts:
                        if "-" in part:
                            start_str, end_str = part.split("-", 1)
                            start = int(start_str)
                            end = int(end_str)
                            if start > end:
                                start, end = end, start
                            selected_indices.update(range(start - 1, end))
                        else:
                            selected_indices.add(int(part) - 1)

                    if all(0 <= i < len(suggested_deps) for i in selected_indices):
                        deps_to_add_paths = [
                            suggested_deps[i] for i in sorted(list(selected_indices))
                        ]
                        break
                    else:
                        print(
                            f"Error: Invalid number selection. Please choose numbers between 1 and {len(suggested_deps)}."
                        )
                else:
                    raise ValueError("Empty part detected in input.")

            except ValueError:
                print(
                    "Invalid choice. Please enter 'a', 'n', or a list/range of numbers (e.g., '1,3' or '2-4')."
                )

        if not deps_to_add_paths:
            return [], 0

        newly_added_files: List[FileTuple] = []
        char_count_delta = 0
        for dep_path in deps_to_add_paths:
            file_size = os.path.getsize(dep_path)
            newly_added_files.append(
                (dep_path, False, None, get_language_for_file(dep_path))
            )
            char_count_delta += file_size
            print(
                f"Added dependency: {os.path.relpath(dep_path)} ({get_human_readable_size(file_size)})"
            )

        return newly_added_files, char_count_delta

    except Exception as e:
        print(
            f"Warning: Could not analyze dependencies for {os.path.relpath(file_just_added)}: {e}"
        )
        return [], 0


def grep_files_in_directory(
    pattern: str, directory: str, ignore_patterns: List[str]
) -> List[Tuple[str, List[str], int]]:
    if not shutil.which("ag"):
        print("Silver Searcher (ag) not found. Install it for grep functionality:")
        print("  - Mac: brew install the_silver_searcher")
        print("  - Ubuntu/Debian: apt-get install silversearcher-ag")
        print("  - Other: https://github.com/ggreer/the_silver_searcher")
        return []

    try:
        cmd = [
            "ag",
            "--files-with-matches",
            "--nocolor",
            "--ignore-case",
            pattern,
            directory,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        files = result.stdout.strip().split("\n")
        grep_results = []

        for file in files:
            if is_ignored(file, ignore_patterns, directory) or is_binary(file):
                continue

            count_cmd = ["ag", "--count", "--nocolor", pattern, file]
            count_result = subprocess.run(count_cmd, capture_output=True, text=True)
            match_count = 0
            if count_result.stdout:
                stdout_line = count_result.stdout.strip()
                last_colon_idx = stdout_line.rfind(":")
                if last_colon_idx > 0:
                    try:
                        match_count = int(stdout_line[last_colon_idx + 1 :])
                    except ValueError:
                        match_count = 1
                else:
                    match_count = 1

            preview_cmd = [
                "ag",
                "--max-count=3",
                "--nocolor",
                "--noheading",
                "--numbers",
                pattern,
                file,
            ]
            preview_result = subprocess.run(preview_cmd, capture_output=True, text=True)
            preview_lines = []
            if preview_result.stdout:
                for line in preview_result.stdout.strip().split("\n")[:3]:
                    if ":" in line:
                        line_num, content = line.split(":", 1)
                        preview_lines.append(f"   {line_num}: {content.strip()}")
                    else:
                        preview_lines.append(f"   {line.strip()}")

            grep_results.append((file, preview_lines, match_count))

        return sorted(grep_results)

    except Exception as e:
        print(f"Error running ag: {e}")
        return []


def select_from_grep_results(
    grep_results: List[Tuple[str, List[str], int]], current_char_count: int
) -> Tuple[List[FileTuple], int]:
    if not grep_results:
        return [], current_char_count

    print(f"\nFound {len(grep_results)} files:")
    for i, (file_path, preview_lines, match_count) in enumerate(grep_results):
        file_size = os.path.getsize(file_path)
        file_size_readable = get_human_readable_size(file_size)
        print(
            f"\n{i+1}. {os.path.relpath(file_path)} ({file_size_readable}) - {match_count} {'match' if match_count == 1 else 'matches'}"
        )
        for preview_line in preview_lines[:3]:
            print(preview_line)
        if match_count > 3:
            print(f"   ... and {match_count - 3} more matches")

    while True:
        print_char_count(current_char_count)
        choice = input(
            "\nSelect grep results: (a)ll / (n)one / (s)elect individually / numbers (e.g. 1,3-4) / (q)uit? "
        ).lower()

        selected_files: List[FileTuple] = []
        char_count_delta = 0

        if choice == "a":
            for file_path, _, _ in grep_results:
                file_size = os.path.getsize(file_path)
                selected_files.append(
                    (file_path, False, None, get_language_for_file(file_path))
                )
                char_count_delta += file_size
            print(f"Added all {len(grep_results)} files from grep results.")
            return selected_files, current_char_count + char_count_delta

        elif choice == "n":
            print("Skipped all grep results.")
            return [], current_char_count

        elif choice == "q":
            print("Cancelled grep selection.")
            return [], current_char_count

        elif choice == "s":
            for i, (file_path, preview_lines, match_count) in enumerate(grep_results):
                file_size = os.path.getsize(file_path)
                file_size_readable = get_human_readable_size(file_size)
                file_char_estimate = file_size
                file_token_estimate = estimate_tokens(file_char_estimate)

                print(
                    f"\n{os.path.relpath(file_path)} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens)"
                )
                print(
                    f"{match_count} {'match' if match_count == 1 else 'matches'} for search pattern"
                )

                while True:
                    print_char_count(current_char_count + char_count_delta)
                    file_choice = input("(y)es / (n)o / (q)uit? ").lower()

                    if file_choice == "y":
                        if is_large_file(file_path):
                            while True:
                                snippet_choice = input(
                                    f"File is large. Use (f)ull content or (s)nippet? "
                                ).lower()
                                if snippet_choice in ["f", "s"]:
                                    break
                                print("Invalid choice. Please enter 'f' or 's'.")
                            if snippet_choice == "s":
                                selected_files.append(
                                    (
                                        file_path,
                                        True,
                                        None,
                                        get_language_for_file(file_path),
                                    )
                                )
                                char_count_delta += len(get_file_snippet(file_path))
                            else:
                                selected_files.append(
                                    (
                                        file_path,
                                        False,
                                        None,
                                        get_language_for_file(file_path),
                                    )
                                )
                                char_count_delta += file_size
                        else:
                            selected_files.append(
                                (
                                    file_path,
                                    False,
                                    None,
                                    get_language_for_file(file_path),
                                )
                            )
                            char_count_delta += file_size
                        print(f"Added: {os.path.relpath(file_path)}")
                        break
                    elif file_choice == "n":
                        break
                    elif file_choice == "q":
                        print(f"Added {len(selected_files)} files from grep results.")
                        return selected_files, current_char_count + char_count_delta
                    else:
                        print("Invalid choice. Please enter 'y', 'n', or 'q'.")

            print(f"Added {len(selected_files)} files from grep results.")
            return selected_files, current_char_count + char_count_delta

        else:
            try:
                selected_indices = set()
                parts = choice.replace(" ", "").split(",")
                if all(p.strip() for p in parts):
                    for part in parts:
                        if "-" in part:
                            start_str, end_str = part.split("-", 1)
                            start = int(start_str)
                            end = int(end_str)
                            if start > end:
                                start, end = end, start
                            selected_indices.update(range(start - 1, end))
                        else:
                            selected_indices.add(int(part) - 1)

                    if all(0 <= i < len(grep_results) for i in selected_indices):
                        for i in sorted(selected_indices):
                            file_path, _, _ = grep_results[i]
                            file_size = os.path.getsize(file_path)
                            selected_files.append(
                                (
                                    file_path,
                                    False,
                                    None,
                                    get_language_for_file(file_path),
                                )
                            )
                            char_count_delta += file_size
                        print(f"Added {len(selected_files)} files from grep results.")
                        return selected_files, current_char_count + char_count_delta
                    else:
                        print(
                            f"Error: Invalid number selection. Please choose numbers between 1 and {len(grep_results)}."
                        )
                else:
                    raise ValueError("Empty part detected in input.")
            except ValueError:
                print(
                    "Invalid choice. Please enter 'a', 'n', 's', 'q', or a list/range of numbers."
                )
