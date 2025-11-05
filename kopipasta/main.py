#!/usr/bin/env python3
import os
import argparse
import subprocess
import shutil
from typing import Dict, List, Optional, Set, Tuple
import pyperclip
from rich.console import Console
from pygments import highlight
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.formatters import TerminalFormatter
import pygments.util

import requests

from kopipasta.file import (
    FileTuple,
    get_human_readable_size,
    is_binary,
    is_ignored,
    is_large_file,
    read_file_contents,
)
import kopipasta.import_parser as import_parser
from kopipasta.tree_selector import TreeSelector
from kopipasta.prompt import (
    generate_prompt_template,
    get_file_snippet,
    get_language_for_file,
    get_task_from_user_interactive,
)
from kopipasta.cache import save_selection_to_cache


def _propose_and_add_dependencies(
    file_just_added: str,
    project_root_abs: str,
    files_to_include: List[FileTuple],
    current_char_count: int,
) -> Tuple[List[FileTuple], int]:
    """
    Analyzes a file for local dependencies and interactively asks the user to add them.
    """
    language = get_language_for_file(file_just_added)
    if language not in ["python", "typescript", "javascript", "tsx", "jsx"]:
        return [], 0  # Only analyze languages we can parse

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

        # Filter out dependencies that are already in the context
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

            # Try to parse the input as numbers directly.
            try:
                selected_indices = set()
                parts = choice.replace(" ", "").split(",")
                if all(p.strip() for p in parts):  # Ensure no empty parts like in "1,"
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

                    # Validate that all selected numbers are within the valid range
                    if all(0 <= i < len(suggested_deps) for i in selected_indices):
                        deps_to_add_paths = [
                            suggested_deps[i] for i in sorted(list(selected_indices))
                        ]
                        break  # Success! Exit the loop.
                    else:
                        print(
                            f"Error: Invalid number selection. Please choose numbers between 1 and {len(suggested_deps)}."
                        )
                else:
                    raise ValueError("Empty part detected in input.")

            except ValueError:
                # This will catch any input that isn't 'a', 'n', or a valid number/range.
                print(
                    "Invalid choice. Please enter 'a', 'n', or a list/range of numbers (e.g., '1,3' or '2-4')."
                )

        if not deps_to_add_paths:
            return [], 0  # No dependencies were selected

        newly_added_files: List[FileTuple] = []
        char_count_delta = 0
        for dep_path in deps_to_add_paths:
            # Assume non-large for now for simplicity, can be enhanced later
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


def get_colored_code(file_path, code):
    try:
        lexer = get_lexer_for_filename(file_path)
    except pygments.util.ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, TerminalFormatter())


def read_gitignore():
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
        "*.class",
        "*.jar",
        "*.war",
        "*.ear",
        "*.sqlite",
        "*.db",
        "*.jpg",
        "*.jpeg",
        "*.png",
        "*.gif",
        "*.bmp",
        "*.tiff",
        "*.ico",
        "*.svg",
        "*.webp",
        "*.mp3",
        "*.mp4",
        "*.avi",
        "*.mov",
        "*.wmv",
        "*.flv",
        "*.pdf",
        "*.doc",
        "*.docx",
        "*.xls",
        "*.xlsx",
        "*.ppt",
        "*.pptx",
        "*.zip",
        "*.rar",
        "*.tar",
        "*.gz",
        "*.7z",
        "*.exe",
        "*.dll",
        "*.so",
        "*.dylib",
    ]
    gitignore_patterns = default_ignore_patterns.copy()

    if os.path.exists(".gitignore"):
        print(".gitignore detected.")
        with open(".gitignore", "r") as file:
            for line in file:
                line = line.strip()
                if line and not line.startswith("#"):
                    gitignore_patterns.append(line)
    return gitignore_patterns


def split_python_file(file_content):
    """
    Splits Python code into logical chunks using the AST module.
    Ensures each chunk is at least 10 lines.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    import ast

    tree = ast.parse(file_content)
    chunks = []
    prev_end = 0
    lines = file_content.splitlines(keepends=True)

    def get_code(start, end):
        return "".join(lines[start:end])

    nodes = [node for node in ast.iter_child_nodes(tree) if hasattr(node, "lineno")]

    i = 0
    while i < len(nodes):
        node = nodes[i]
        start_line = node.lineno - 1  # Convert to 0-indexed
        end_line = getattr(node, "end_lineno", None)
        if end_line is None:
            end_line = start_line + 1

        # Merge chunks to meet minimum lines
        chunk_start = start_line
        chunk_end = end_line
        while (chunk_end - chunk_start) < 10 and i + 1 < len(nodes):
            i += 1
            next_node = nodes[i]
            next_start = next_node.lineno - 1
            next_end = getattr(next_node, "end_lineno", None) or next_start + 1
            chunk_end = next_end

        # Add code before the node (e.g., imports or global code)
        if prev_end < chunk_start:
            code = get_code(prev_end, chunk_start)
            if code.strip():
                chunks.append((code, prev_end, chunk_start))

        # Add the merged chunk
        code = get_code(chunk_start, chunk_end)
        chunks.append((code, chunk_start, chunk_end))
        prev_end = chunk_end
        i += 1

    # Add any remaining code at the end
    if prev_end < len(lines):
        code = get_code(prev_end, len(lines))
        if code.strip():
            chunks.append((code, prev_end, len(lines)))

    return merge_small_chunks(chunks)


def merge_small_chunks(chunks, min_lines=10):
    """
    Merges chunks to ensure each has at least min_lines lines.
    """
    merged_chunks = []
    buffer_code = ""
    buffer_start = None
    buffer_end = None

    for code, start_line, end_line in chunks:
        num_lines = end_line - start_line
        if buffer_code == "":
            buffer_code = code
            buffer_start = start_line
            buffer_end = end_line
        else:
            buffer_code += code
            buffer_end = end_line

        if (buffer_end - buffer_start) >= min_lines:
            merged_chunks.append((buffer_code, buffer_start, buffer_end))
            buffer_code = ""
            buffer_start = None
            buffer_end = None

    if buffer_code:
        merged_chunks.append((buffer_code, buffer_start, buffer_end))

    return merged_chunks


def split_javascript_file(file_content):
    """
    Splits JavaScript code into logical chunks using regular expressions.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    lines = file_content.splitlines(keepends=True)
    chunks = []
    pattern = re.compile(
        r"^\s*(export\s+)?(async\s+)?(function\s+\w+|class\s+\w+|\w+\s*=\s*\(.*?\)\s*=>)",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(file_content))

    if not matches:
        return [(file_content, 0, len(lines))]

    prev_end_line = 0
    for match in matches:
        start_index = match.start()
        start_line = file_content.count("\n", 0, start_index)
        if prev_end_line < start_line:
            code = "".join(lines[prev_end_line:start_line])
            chunks.append((code, prev_end_line, start_line))

        function_code_lines = []
        brace_count = 0
        in_block = False
        for i in range(start_line, len(lines)):
            line = lines[i]
            function_code_lines.append(line)
            brace_count += line.count("{") - line.count("}")
            if "{" in line:
                in_block = True
            if in_block and brace_count == 0:
                end_line = i + 1
                code = "".join(function_code_lines)
                chunks.append((code, start_line, end_line))
                prev_end_line = end_line
                break
        else:
            end_line = len(lines)
            code = "".join(function_code_lines)
            chunks.append((code, start_line, end_line))
            prev_end_line = end_line

    if prev_end_line < len(lines):
        code = "".join(lines[prev_end_line:])
        chunks.append((code, prev_end_line, len(lines)))

    return merge_small_chunks(chunks)


def split_html_file(file_content):
    """
    Splits HTML code into logical chunks based on top-level elements using regular expressions.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    pattern = re.compile(r"<(?P<tag>\w+)(\s|>).*?</(?P=tag)>", re.DOTALL)
    lines = file_content.splitlines(keepends=True)
    chunks = []
    matches = list(pattern.finditer(file_content))

    if not matches:
        return [(file_content, 0, len(lines))]

    prev_end = 0
    for match in matches:
        start_index = match.start()
        end_index = match.end()
        start_line = file_content.count("\n", 0, start_index)
        end_line = file_content.count("\n", 0, end_index)

        if prev_end < start_line:
            code = "".join(lines[prev_end:start_line])
            chunks.append((code, prev_end, start_line))

        code = "".join(lines[start_line:end_line])
        chunks.append((code, start_line, end_line))
        prev_end = end_line

    if prev_end < len(lines):
        code = "".join(lines[prev_end:])
        chunks.append((code, prev_end, len(lines)))

    return merge_small_chunks(chunks)


def split_c_file(file_content):
    """
    Splits C/C++ code into logical chunks using regular expressions.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    pattern = re.compile(r"^\s*(?:[\w\*\s]+)\s+(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE)
    lines = file_content.splitlines(keepends=True)
    chunks = []
    matches = list(pattern.finditer(file_content))

    if not matches:
        return [(file_content, 0, len(lines))]

    prev_end_line = 0
    for match in matches:
        start_index = match.start()
        start_line = file_content.count("\n", 0, start_index)
        if prev_end_line < start_line:
            code = "".join(lines[prev_end_line:start_line])
            chunks.append((code, prev_end_line, start_line))

        function_code_lines = []
        brace_count = 0
        in_function = False
        for i in range(start_line, len(lines)):
            line = lines[i]
            function_code_lines.append(line)
            brace_count += line.count("{") - line.count("}")
            if "{" in line:
                in_function = True
            if in_function and brace_count == 0:
                end_line = i + 1
                code = "".join(function_code_lines)
                chunks.append((code, start_line, end_line))
                prev_end_line = end_line
                break
        else:
            end_line = len(lines)
            code = "".join(function_code_lines)
            chunks.append((code, start_line, end_line))
            prev_end_line = end_line

    if prev_end_line < len(lines):
        code = "".join(lines[prev_end_line:])
        chunks.append((code, prev_end_line, len(lines)))

    return merge_small_chunks(chunks)


def split_generic_file(file_content):
    """
    Splits generic text files into chunks based on double newlines.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    lines = file_content.splitlines(keepends=True)
    chunks = []
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            if start < i:
                chunk_code = "".join(lines[start:i])
                chunks.append((chunk_code, start, i))
            start = i + 1
    if start < len(lines):
        chunk_code = "".join(lines[start:])
        chunks.append((chunk_code, start, len(lines)))
    return merge_small_chunks(chunks)


def select_file_patches(file_path):
    file_content = read_file_contents(file_path)
    language = get_language_for_file(file_path)
    chunks = []
    total_char_count = 0

    if language == "python":
        code_chunks = split_python_file(file_content)
    elif language == "javascript":
        code_chunks = split_javascript_file(file_content)
    elif language == "html":
        code_chunks = split_html_file(file_content)
    elif language in ["c", "cpp"]:
        code_chunks = split_c_file(file_content)
    else:
        code_chunks = split_generic_file(file_content)
    placeholder = get_placeholder_comment(language)

    print(f"\nSelecting patches for {file_path}")
    for index, (chunk_code, start_line, end_line) in enumerate(code_chunks):
        print(f"\nChunk {index + 1} (Lines {start_line + 1}-{end_line}):")
        colored_chunk = get_colored_code(file_path, chunk_code)
        print(colored_chunk)
        while True:
            choice = input("(y)es include / (n)o skip / (q)uit rest of file? ").lower()
            if choice == "y":
                chunks.append(chunk_code)
                total_char_count += len(chunk_code)
                break
            elif choice == "n":
                if not chunks or chunks[-1] != placeholder:
                    chunks.append(placeholder)
                total_char_count += len(placeholder)
                break
            elif choice == "q":
                print("Skipping the rest of the file.")
                if chunks and chunks[-1] != placeholder:
                    chunks.append(placeholder)
                return chunks, total_char_count
            else:
                print("Invalid choice. Please enter 'y', 'n', or 'q'.")

    return chunks, total_char_count


def get_placeholder_comment(language):
    comments = {
        "python": "# Skipped content\n",
        "javascript": "// Skipped content\n",
        "typescript": "// Skipped content\n",
        "java": "// Skipped content\n",
        "c": "// Skipped content\n",
        "cpp": "// Skipped content\n",
        "html": "<!-- Skipped content -->\n",
        "css": "/* Skipped content */\n",
        "default": "# Skipped content\n",
    }
    return comments.get(language, comments["default"])


def get_colored_file_snippet(file_path, max_lines=50, max_bytes=4096):
    snippet = get_file_snippet(file_path, max_lines, max_bytes)
    return get_colored_code(file_path, snippet)


def print_char_count(count):
    token_estimate = count // 4
    print(
        f"\rCurrent prompt size: {count} characters (~ {token_estimate} tokens)",
        flush=True,
    )


def grep_files_in_directory(
    pattern: str, directory: str, ignore_patterns: List[str]
) -> List[Tuple[str, List[str], int]]:
    """
    Search for files containing a pattern using ag (silver searcher).
    Returns list of (filepath, preview_lines, match_count).
    """
    # Check if ag is available
    if not shutil.which("ag"):
        print("Silver Searcher (ag) not found. Install it for grep functionality:")
        print("  - Mac: brew install the_silver_searcher")
        print("  - Ubuntu/Debian: apt-get install silversearcher-ag")
        print("  - Other: https://github.com/ggreer/the_silver_searcher")
        return []

    try:
        # First get files with matches
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

            # Get match count and preview lines
            count_cmd = ["ag", "--count", "--nocolor", pattern, file]
            count_result = subprocess.run(count_cmd, capture_output=True, text=True)
            match_count = 0
            if count_result.stdout:
                # ag --count outputs "filename:count"
                # We need to handle filenames that might contain colons
                stdout_line = count_result.stdout.strip()
                # Find the last colon to separate filename from count
                last_colon_idx = stdout_line.rfind(":")
                if last_colon_idx > 0:
                    try:
                        match_count = int(stdout_line[last_colon_idx + 1 :])
                    except ValueError:
                        match_count = 1
                else:
                    match_count = 1

            # Get preview of matches (up to 3 lines)
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
                    # Format: "line_num:content"
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
    """
    Let user select from grep results.
    Returns (selected_files, new_char_count).
    """
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
                file_token_estimate = file_char_estimate // 4

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
            # Try to parse number selection
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


def select_files_in_directory(
    directory: str,
    ignore_patterns: List[str],
    project_root_abs: str,
    current_char_count: int = 0,
    selected_files_set: Optional[Set[str]] = None,
) -> Tuple[List[FileTuple], int]:
    if selected_files_set is None:
        selected_files_set = set()

    files = [
        f
        for f in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, f))
        and not is_ignored(os.path.join(directory, f), ignore_patterns)
        and not is_binary(os.path.join(directory, f))
    ]

    if not files:
        return [], current_char_count

    print(f"\nDirectory: {directory}")
    print("Files:")
    for file in files:
        file_path = os.path.join(directory, file)
        file_size = os.path.getsize(file_path)
        file_size_readable = get_human_readable_size(file_size)
        file_char_estimate = file_size  # Assuming 1 byte ≈ 1 character for text files
        file_token_estimate = file_char_estimate // 4

        # Show if already selected
        if os.path.abspath(file_path) in selected_files_set:
            print(
                f"✓ {file} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens) [already selected]"
            )
        else:
            print(
                f"- {file} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens)"
            )

    while True:
        print_char_count(current_char_count)
        choice = input(
            "(y)es add all / (n)o ignore all / (s)elect individually / (g)rep / (q)uit? "
        ).lower()
        selected_files: List[FileTuple] = []
        char_count_delta = 0

        if choice == "g":
            # Grep functionality
            pattern = input("\nEnter search pattern: ")
            if pattern:
                print(f"\nSearching in {directory} for '{pattern}'...")
                grep_results = grep_files_in_directory(
                    pattern, directory, ignore_patterns
                )

                if not grep_results:
                    print(f"No files found matching '{pattern}'")
                    continue

                grep_selected, new_char_count = select_from_grep_results(
                    grep_results, current_char_count
                )

                if grep_selected:
                    selected_files.extend(grep_selected)
                    current_char_count = new_char_count

                    # Update selected files set
                    for file_tuple in grep_selected:
                        selected_files_set.add(os.path.abspath(file_tuple[0]))

                    # Analyze dependencies for grep-selected files
                    files_to_analyze = [f[0] for f in grep_selected]
                    for file_path in files_to_analyze:
                        new_deps, deps_char_count = _propose_and_add_dependencies(
                            file_path,
                            project_root_abs,
                            selected_files,
                            current_char_count,
                        )
                        selected_files.extend(new_deps)
                        current_char_count += deps_char_count

                        # Update selected files set with dependencies
                        for dep_tuple in new_deps:
                            selected_files_set.add(os.path.abspath(dep_tuple[0]))

                    print(f"\nReturning to directory: {directory}")
                    # Re-show the directory with updated selections
                    print("Files:")
                    for file in files:
                        file_path = os.path.join(directory, file)
                        file_size = os.path.getsize(file_path)
                        file_size_readable = get_human_readable_size(file_size)
                        if os.path.abspath(file_path) in selected_files_set:
                            print(f"✓ {file} ({file_size_readable}) [already selected]")
                        else:
                            print(f"- {file} ({file_size_readable})")

                    # Ask what to do with remaining files
                    remaining_files = [
                        f
                        for f in files
                        if os.path.abspath(os.path.join(directory, f))
                        not in selected_files_set
                    ]
                    if remaining_files:
                        while True:
                            print_char_count(current_char_count)
                            remaining_choice = input(
                                "(y)es add remaining / (n)o skip remaining / (s)elect more / (g)rep again / (q)uit? "
                            ).lower()
                            if remaining_choice == "y":
                                # Add all remaining files
                                for file in remaining_files:
                                    file_path = os.path.join(directory, file)
                                    file_size = os.path.getsize(file_path)
                                    selected_files.append(
                                        (
                                            file_path,
                                            False,
                                            None,
                                            get_language_for_file(file_path),
                                        )
                                    )
                                    current_char_count += file_size
                                    selected_files_set.add(os.path.abspath(file_path))

                                # Analyze dependencies for remaining files
                                for file in remaining_files:
                                    file_path = os.path.join(directory, file)
                                    (
                                        new_deps,
                                        deps_char_count,
                                    ) = _propose_and_add_dependencies(
                                        file_path,
                                        project_root_abs,
                                        selected_files,
                                        current_char_count,
                                    )
                                    selected_files.extend(new_deps)
                                    current_char_count += deps_char_count

                                print(f"Added all remaining files from {directory}")
                                return selected_files, current_char_count
                            elif remaining_choice == "n":
                                print(f"Skipped remaining files from {directory}")
                                return selected_files, current_char_count
                            elif remaining_choice == "s":
                                # Continue to individual selection
                                choice = "s"
                                break
                            elif remaining_choice == "g":
                                # Continue to grep again
                                choice = "g"
                                break
                            elif remaining_choice == "q":
                                return selected_files, current_char_count
                            else:
                                print("Invalid choice. Please try again.")

                        if choice == "s":
                            # Fall through to individual selection
                            pass
                        elif choice == "g":
                            # Loop back to grep
                            continue
                    else:
                        # No remaining files
                        return selected_files, current_char_count
                else:
                    # No files selected from grep, continue
                    continue
            else:
                continue

        if choice == "y":
            files_to_add_after_loop = []
            for file in files:
                file_path = os.path.join(directory, file)
                if os.path.abspath(file_path) in selected_files_set:
                    continue  # Skip already selected files

                if is_large_file(file_path):
                    while True:
                        snippet_choice = input(
                            f"{file} is large. Use (f)ull content or (s)nippet? "
                        ).lower()
                        if snippet_choice in ["f", "s"]:
                            break
                        print("Invalid choice. Please enter 'f' or 's'.")
                    if snippet_choice == "s":
                        selected_files.append(
                            (file_path, True, None, get_language_for_file(file_path))
                        )
                        char_count_delta += len(get_file_snippet(file_path))
                    else:
                        selected_files.append(
                            (file_path, False, None, get_language_for_file(file_path))
                        )
                        char_count_delta += os.path.getsize(file_path)
                else:
                    selected_files.append(
                        (file_path, False, None, get_language_for_file(file_path))
                    )
                    char_count_delta += os.path.getsize(file_path)
                files_to_add_after_loop.append(file_path)

            # Analyze dependencies after the loop
            current_char_count += char_count_delta
            for file_path in files_to_add_after_loop:
                new_deps, deps_char_count = _propose_and_add_dependencies(
                    file_path, project_root_abs, selected_files, current_char_count
                )
                selected_files.extend(new_deps)
                current_char_count += deps_char_count

            print(f"Added all files from {directory}")
            return selected_files, current_char_count

        elif choice == "n":
            print(f"Ignored all files from {directory}")
            return [], current_char_count

        elif choice == "s":
            for file in files:
                file_path = os.path.join(directory, file)
                if os.path.abspath(file_path) in selected_files_set:
                    continue  # Skip already selected files

                file_size = os.path.getsize(file_path)
                file_size_readable = get_human_readable_size(file_size)
                file_char_estimate = file_size
                file_token_estimate = file_char_estimate // 4
                while True:
                    if current_char_count > 0:
                        print_char_count(current_char_count)
                    file_choice = input(
                        f"{file} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens) (y/n/p/q)? "
                    ).lower()
                    if file_choice == "y":
                        file_to_add = None
                        if is_large_file(file_path):
                            while True:
                                snippet_choice = input(
                                    f"{file} is large. Use (f)ull content or (s)nippet? "
                                ).lower()
                                if snippet_choice in ["f", "s"]:
                                    break
                                print("Invalid choice. Please enter 'f' or 's'.")
                            if snippet_choice == "s":
                                file_to_add = (
                                    file_path,
                                    True,
                                    None,
                                    get_language_for_file(file_path),
                                )
                                current_char_count += len(get_file_snippet(file_path))
                            else:
                                file_to_add = (
                                    file_path,
                                    False,
                                    None,
                                    get_language_for_file(file_path),
                                )
                                current_char_count += file_char_estimate
                        else:
                            file_to_add = (
                                file_path,
                                False,
                                None,
                                get_language_for_file(file_path),
                            )
                            current_char_count += file_char_estimate

                        if file_to_add:
                            selected_files.append(file_to_add)
                            selected_files_set.add(os.path.abspath(file_path))
                            # Analyze dependencies immediately after adding
                            new_deps, deps_char_count = _propose_and_add_dependencies(
                                file_path,
                                project_root_abs,
                                selected_files,
                                current_char_count,
                            )
                            selected_files.extend(new_deps)
                            current_char_count += deps_char_count
                        break
                    elif file_choice == "n":
                        break
                    elif file_choice == "p":
                        chunks, char_count = select_file_patches(file_path)
                        if chunks:
                            selected_files.append(
                                (
                                    file_path,
                                    False,
                                    chunks,
                                    get_language_for_file(file_path),
                                )
                            )
                            current_char_count += char_count
                            selected_files_set.add(os.path.abspath(file_path))
                        break
                    elif file_choice == "q":
                        print(f"Quitting selection for {directory}")
                        return selected_files, current_char_count
                    else:
                        print("Invalid choice. Please enter 'y', 'n', 'p', or 'q'.")
            print(f"Added {len(selected_files)} files from {directory}")
            return selected_files, current_char_count

        elif choice == "q":
            print(f"Quitting selection for {directory}")
            return [], current_char_count
        else:
            print("Invalid choice. Please try again.")


def process_directory(
    directory: str,
    ignore_patterns: List[str],
    project_root_abs: str,
    current_char_count: int = 0,
    selected_files_set: Optional[Set[str]] = None,
) -> Tuple[List[FileTuple], Set[str], int]:
    if selected_files_set is None:
        selected_files_set = set()

    files_to_include: List[FileTuple] = []
    processed_dirs: Set[str] = set()

    for root, dirs, files in os.walk(directory):
        dirs[:] = [
            d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)
        ]
        files = [
            f
            for f in files
            if not is_ignored(os.path.join(root, f), ignore_patterns)
            and not is_binary(os.path.join(root, f))
        ]

        if root in processed_dirs:
            continue

        print(f"\nExploring directory: {root}")
        choice = input("(y)es explore / (n)o skip / (q)uit? ").lower()
        if choice == "y":
            # Pass selected_files_set to track already selected files
            selected_files, current_char_count = select_files_in_directory(
                root,
                ignore_patterns,
                project_root_abs,
                current_char_count,
                selected_files_set,
            )
            files_to_include.extend(selected_files)

            # Update selected_files_set
            for file_tuple in selected_files:
                selected_files_set.add(os.path.abspath(file_tuple[0]))

            processed_dirs.add(root)
        elif choice == "n":
            dirs[:] = []  # Skip all subdirectories
            continue
        elif choice == "q":
            break
        else:
            print("Invalid choice. Skipping this directory.")
            continue

    return files_to_include, processed_dirs, current_char_count


def fetch_web_content(
    url: str,
) -> Tuple[Optional[FileTuple], Optional[str], Optional[str]]:
    try:
        response = requests.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        full_content = response.text
        snippet = (
            full_content[:10000] + "..." if len(full_content) > 10000 else full_content
        )

        if "json" in content_type:
            content_type = "json"
        elif "csv" in content_type:
            content_type = "csv"
        else:
            content_type = "text"

        return (url, False, None, content_type), full_content, snippet
    except requests.RequestException as e:
        print(f"Error fetching content from {url}: {e}")
        return None, None, None


def read_env_file():
    env_vars = {}
    if os.path.exists(".env"):
        with open(".env", "r") as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars


def main():
    console = Console()
    parser = argparse.ArgumentParser(
        description="Generate a prompt with project structure, file contents, and web content."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Files, directories, or URLs to include. Defaults to current directory.",
    )
    parser.add_argument("-t", "--task", help="Task description for the AI prompt")
    args = parser.parse_args()

    # Default to the current directory if no inputs are provided
    if not args.inputs:
        args.inputs.append(".")

    ignore_patterns = read_gitignore()
    env_vars = read_env_file()
    project_root_abs = os.path.abspath(os.getcwd())

    files_to_include: List[FileTuple] = []
    web_contents: Dict[str, Tuple[FileTuple, str]] = {}
    current_char_count = 0

    # Separate URLs from file/directory paths
    paths_for_tree = []
    files_to_preselect = []

    for input_path in args.inputs:
        if input_path.startswith(("http://", "https://")):
            # Handle web content as before
            result = fetch_web_content(input_path)
            if result:
                file_tuple, full_content, snippet = result
                is_large = len(full_content) > 10000
                if is_large:
                    print(f"\nContent from {input_path} is large. Here's a snippet:\n")
                    print(get_colored_code(input_path, snippet))
                    print("\n" + "-" * 40 + "\n")

                    while True:
                        choice = input("Use (f)ull content or (s)nippet? ").lower()
                        if choice in ["f", "s"]:
                            break
                        print("Invalid choice. Please enter 'f' or 's'.")

                    if choice == "f":
                        content = full_content
                        is_snippet = False
                        print("Using full content.")
                    else:
                        content = snippet
                        is_snippet = True
                        print("Using snippet.")
                else:
                    content = full_content
                    is_snippet = False
                    print(
                        f"Content from {input_path} is not large. Using full content."
                    )

                file_tuple = (file_tuple[0], is_snippet, file_tuple[2], file_tuple[3])
                web_contents[input_path] = (file_tuple, content)
                current_char_count += len(content)
                print(
                    f"Added {'snippet of ' if is_snippet else ''}web content from: {input_path}"
                )
                print_char_count(current_char_count)
        else:
            abs_path = os.path.abspath(input_path)
            if os.path.exists(abs_path):
                paths_for_tree.append(input_path)
                if os.path.isfile(abs_path):
                    files_to_preselect.append(abs_path)
            else:
                print(f"Warning: {input_path} does not exist. Skipping.")

    # Use tree selector for file/directory selection
    if paths_for_tree:
        print("\nStarting interactive file selection...")
        print(
            "Use arrow keys to navigate, Space to select, 'q' to finish. See all keys below.\n"
        )

        tree_selector = TreeSelector(ignore_patterns, project_root_abs)
        try:
            selected_files, file_char_count = tree_selector.run(
                paths_for_tree, files_to_preselect
            )
            files_to_include.extend(selected_files)
            current_char_count += file_char_count
        except KeyboardInterrupt:
            print("\nSelection cancelled.")
            return

    if not files_to_include and not web_contents:
        print("No files or web content were selected. Exiting.")
        return

    # Save the final selection for the next run
    if files_to_include:
        save_selection_to_cache(files_to_include)

    print("\nFile and web content selection complete.")
    print_char_count(current_char_count)

    added_files_count = len(files_to_include)
    added_web_count = len(web_contents)
    print(
        f"Summary: Added {added_files_count} files/patches and {added_web_count} web sources."
    )

    prompt_template, cursor_position = generate_prompt_template(
        files_to_include, ignore_patterns, web_contents, env_vars
    )

    if args.task:
        task_description = args.task
        console.print("\n[bold cyan]Using task description from --task argument.[/bold cyan]")
    else:
        task_description = get_task_from_user_interactive(console)
    
    if not task_description:
        console.print("\n[bold red]No task provided. Aborting.[/bold red]")
        return

    final_prompt = (
        prompt_template[:cursor_position]
        + task_description
        + prompt_template[cursor_position:]
    )

    print("\n\nGenerated prompt:")
    print("-" * 80)
    print(final_prompt)
    print("-" * 80)

    try:
        pyperclip.copy(final_prompt)
        print("\n--- Included Files & Content ---\n")
        for file_path, is_snippet, chunks, _ in sorted(
            files_to_include, key=lambda x: x[0]
        ):
            details = []
            if is_snippet:
                details.append("snippet")
            if chunks is not None:
                details.append(f"{len(chunks)} patches")

            detail_str = f" ({', '.join(details)})" if details else ""
            print(f"- {os.path.relpath(file_path)}{detail_str}")

        for url, (file_tuple, _) in sorted(web_contents.items()):
            is_snippet = file_tuple[1]
            detail_str = " (snippet)" if is_snippet else ""
            print(f"- {url}{detail_str}")

        separator = (
            "\n"
            + "=" * 40
            + "\n☕🍝       Kopipasta Complete!       🍝☕\n"
            + "=" * 40
            + "\n"
        )
        print(separator)

        final_char_count = len(final_prompt)
        final_token_estimate = final_char_count // 4
        print(
            f"Prompt has been copied to clipboard. Final size: {final_char_count} characters (~ {final_token_estimate} tokens)"
        )
    except pyperclip.PyperclipException as e:
        print(f"\nWarning: Failed to copy to clipboard: {e}")
        print("You can manually copy the prompt above.")


if __name__ == "__main__":
    main()
