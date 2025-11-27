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
    read_file_contents,
)
from kopipasta.ops import (
    print_char_count,
    read_env_file,
    read_gitignore,
)
from kopipasta.tree_selector import TreeSelector
from kopipasta.prompt import (
    generate_prompt_template,
    get_file_snippet,
    get_task_from_user_interactive,
)
from kopipasta.cache import save_selection_to_cache


def get_colored_code(file_path, code):
    try:
        lexer = get_lexer_for_filename(file_path)
    except pygments.util.ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, TerminalFormatter())


def get_colored_file_snippet(file_path, max_lines=50, max_bytes=4096):
    snippet = get_file_snippet(file_path, max_lines, max_bytes)
    return get_colored_code(file_path, snippet)


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

    # Use robust reading functions from ops
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
        console.print(
            "\n[bold cyan]Using task description from --task argument.[/bold cyan]"
        )
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
