import os
import shutil
import subprocess
from typing import List, Tuple, Set

import kopipasta.import_parser as import_parser
from kopipasta.file import (
    FileTuple,
    get_human_readable_size,
    read_file_contents,
    is_ignored,
    is_binary,
    is_large_file,
)
from kopipasta.prompt import get_language_for_file, get_file_snippet
from kopipasta.ops import print_char_count, estimate_tokens


def find_local_dependencies(
    target_file: str,
    project_root: str,
    excluded_paths: Set[str],
) -> List[str]:
    """
    Pure logic: Analyzes imports in target_file and returns a list of
    local absolute file paths that are not in excluded_paths.
    """
    language = get_language_for_file(target_file)
    if language not in ["python", "typescript", "javascript", "tsx", "jsx"]:
        return []

    try:
        file_content = read_file_contents(target_file)
        if not file_content:
            return []

        resolved_deps_abs: Set[str] = set()
        if language == "python":
            resolved_deps_abs = import_parser.parse_python_imports(
                file_content, target_file, project_root
            )
        elif language in ["typescript", "javascript", "tsx", "jsx"]:
            resolved_deps_abs = import_parser.parse_typescript_imports(
                file_content, target_file, project_root
            )

        return sorted(
            [
                dep
                for dep in resolved_deps_abs
                if dep not in excluded_paths and dep != os.path.abspath(target_file)
            ]
        )
    except Exception as e:
        # In a pure function, we might want to log this or re-raise.
        # For now, printing to stderr or returning empty is acceptable safety.
        print(
            f"Warning: Dependency analysis failed for {os.path.relpath(target_file)}: {e}"
        )
        return []


def propose_and_add_dependencies(
    file_just_added: str,
    project_root_abs: str,
    files_to_include: List[FileTuple],
    current_char_count: int,
) -> Tuple[List[FileTuple], int]:
    """
    UI/Controller: Uses find_local_dependencies to prompt the user.
    """
    print(f"Analyzing {os.path.relpath(file_just_added)} for local dependencies...")

    included_paths = {os.path.abspath(f[0]) for f in files_to_include}

    suggested_deps = find_local_dependencies(
        file_just_added, project_root_abs, included_paths
    )

    if not suggested_deps:
        print("No new local dependencies found.")
        return [], 0

    print(
        f"\nFound {len(suggested_deps)} new local {'dependency' if len(suggested_deps) == 1 else 'dependencies'}:"
    )
    for i, dep_path in enumerate(suggested_deps):
        print(f"  ({i + 1}) {os.path.relpath(dep_path)}")

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


def grep_files_in_directory(
    pattern: str, directory: str, ignore_patterns: List[str]
) -> List[Tuple[str, List[str], int]]:
    """Runs 'ag' (Silver Searcher) to find patterns in files."""
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
    """Interactive selection from grep results."""
    if not grep_results:
        return [], current_char_count

    print(f"\nFound {len(grep_results)} files:")
    for i, (file_path, preview_lines, match_count) in enumerate(grep_results):
        file_size = os.path.getsize(file_path)
        file_size_readable = get_human_readable_size(file_size)
        print(
            f"\n{i + 1}. {os.path.relpath(file_path)} ({file_size_readable}) - {match_count} {'match' if match_count == 1 else 'matches'}"
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
                                    "File is large. Use (f)ull content or (s)nippet? "
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
