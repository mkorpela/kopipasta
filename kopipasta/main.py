#!/usr/bin/env python3
import os
import argparse
import subprocess
import shutil
import sys
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
    sanitize_string,
    read_global_profile,
    open_profile_in_editor,
    read_project_context,
    read_session_state,
    check_session_gitignore_status,
    estimate_tokens,
)
from kopipasta.tree_selector import TreeSelector
from kopipasta.prompt import (
    generate_prompt_template,
    get_file_snippet,
    get_task_from_user_interactive,
    reset_template,
    open_template_in_editor,
)
from kopipasta.cache import (
    save_selection_to_cache,
    load_selection_from_cache,
    load_task_from_cache,
    save_task_to_cache,
)


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
    if sys.platform == "win32":
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

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
    parser.add_argument("--reset-template", action="store_true", help="Reset the prompt template to default")
    parser.add_argument("--edit-template", action="store_true", help="Open the template file in default editor")
    parser.add_argument("--edit-profile", action="store_true", help="Open the global user profile (AI Identity) in default editor")
    args = parser.parse_args()

    # Handle Template Management Arguments
    if args.reset_template:
        reset_template()
        return

    if args.edit_template:
        open_template_in_editor()
        return

    if args.edit_profile:
        open_profile_in_editor()
        return

    # Default to the current directory if no inputs are provided
    if not args.inputs:
        args.inputs.append(".")

    # Use robust reading functions from ops
    ignore_patterns = read_gitignore()
    env_vars = read_env_file()
    project_root_abs = os.path.abspath(os.getcwd())
    session_path = os.path.join(project_root_abs, "AI_SESSION.md")

    # --- Safety Check: AI_SESSION.md ---
    if not check_session_gitignore_status(project_root_abs):
        console.print(
            "\n[bold yellow]⚠️  WARNING: `AI_SESSION.md` is detected but NOT ignored by git.[/bold yellow]"
        )
        console.print("   This file is intended for ephemeral scratchpad data.")
        console.print("   Please add it to your `.gitignore` to prevent accidental commits.\n")
        # We continue execution, just warning.
        input("Press Enter to continue...")

    files_to_include: List[FileTuple] = []
    web_contents: Dict[str, Tuple[FileTuple, str]] = {}
    current_char_count = 0

    # Separate URLs from file/directory paths
    paths_for_tree = []
    files_to_preselect = []

    # --- Auto-load Session State ---
    is_ongoing_session = os.path.exists(session_path)
    if is_ongoing_session:
        # 1. Load previous file selection
        cached_paths = load_selection_from_cache()
        for p in cached_paths:
            abs_p = os.path.abspath(p)
            if os.path.exists(abs_p):
                files_to_preselect.append(abs_p)
        
        # 2. Force Context and Session files into selection if they exist
        files_to_preselect.append(session_path)
        context_path = os.path.join(project_root_abs, "AI_CONTEXT.md")
        if os.path.exists(context_path):
            files_to_preselect.append(context_path)

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

    # --- Quad-Memory: Auto-Load Context ---
    user_profile = read_global_profile()
    project_context = read_project_context(project_root_abs)
    session_state = read_session_state(project_root_abs)

    # Save the final selection for the next run
    if files_to_include:
        save_selection_to_cache(files_to_include)

    # Load default task if session is ongoing
    cached_task = None
    if is_ongoing_session and not args.task:
        cached_task = load_task_from_cache()

    print("\nFile and web content selection complete.")
    print_char_count(current_char_count)

    added_files_count = len(files_to_include)
    added_web_count = len(web_contents)
    print(
        f"Summary: Added {added_files_count} files/patches and {added_web_count} web sources."
    )

    # Deduplicate auto-loaded files if selected in tree
    files_to_include = [
        f for f in files_to_include
        if not (os.path.basename(f[0]) == "AI_CONTEXT.md" and project_context)
        and not (os.path.basename(f[0]) == "AI_SESSION.md" and session_state)
    ]

    prompt_template, cursor_position = generate_prompt_template(
        files_to_include, ignore_patterns, web_contents, env_vars, paths_for_tree,
        user_profile=user_profile,
        project_context=project_context,
        session_state=session_state
    )

    if args.task:
        task_description = args.task
        console.print(
            "\n[bold cyan]Using task description from --task argument.[/bold cyan]"
        )
    else:
        task_description = get_task_from_user_interactive(console, default_text=cached_task or "")

    if not task_description:
        console.print("\n[bold red]No task provided. Aborting.[/bold red]")
        return
    
    # Save task to cache
    save_task_to_cache(task_description)

    final_prompt = (
        prompt_template[:cursor_position]
        + task_description
        + prompt_template[cursor_position:]
    )

    final_prompt = sanitize_string(final_prompt)

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
        final_token_estimate = estimate_tokens(final_char_count)
        print(
            f"Prompt has been copied to clipboard. Final size: {final_char_count} characters (~ {final_token_estimate} tokens)"
        )
    except pyperclip.PyperclipException as e:
        print(f"\nWarning: Failed to copy to clipboard: {e}")
        print("You can manually copy the prompt above.")


if __name__ == "__main__":
    main()
