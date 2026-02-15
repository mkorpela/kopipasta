#!/usr/bin/env python3
import os
import argparse
import sys
from typing import Dict, List, Optional, Tuple
import requests
import pyperclip
from rich.console import Console
from pygments import highlight
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.formatters import TerminalFormatter
import pygments.util

from kopipasta.file import (
    FileTuple,
)
from kopipasta.ops import (
    print_char_count,
    sanitize_string,
    estimate_tokens,
)
from kopipasta.config import (
    read_env_file,
    read_gitignore,
    read_global_profile,
    open_profile_in_editor,
    read_project_context,
    read_session_state,
)
from kopipasta.tui import run_tui
from kopipasta.prompt import (
    generate_prompt_template,
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
from kopipasta.logger import configure_logging, get_logger


def get_colored_code(file_path, code):
    try:
        lexer = get_lexer_for_filename(file_path)
    except pygments.util.ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, TerminalFormatter())


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


class KopipastaApp:
    def __init__(self):
        # Initialize logging as early as possible
        configure_logging()
        self.logger = get_logger()
        self.console = Console()
        self.args: argparse.Namespace = argparse.Namespace()

        # Core State
        self.project_root_abs = os.path.abspath(os.getcwd())
        self.files_to_include: List[FileTuple] = []
        self.web_contents: Dict[str, Tuple[FileTuple, str]] = {}
        self.paths_for_tree: List[str] = []
        self.files_to_preselect: List[str] = []
        self.current_char_count = 0

        # Configuration
        self.ignore_patterns: List[str] = []
        self.env_vars: Dict[str, str] = {}
        self.user_profile: Optional[str] = None
        self.project_context: Optional[str] = None
        self.session_state: Optional[str] = None

        # Session Flags
        self.session_path = os.path.join(self.project_root_abs, "AI_SESSION.md")
        self.is_ongoing_session = False

        self.logger.info("app_started", cwd=self.project_root_abs)

    def run(self):
        """Main lifecycle of the application."""
        try:
            self._configure_platform()
            self._parse_args()

            if self._handle_utility_commands():
                return

            self._load_configuration()
            self._load_session_state()
            self._process_inputs()

            self._run_interactive_selection()

            if not self.files_to_include and not self.web_contents:
                print("No files or web content were selected. Exiting.")
                self.logger.info("app_exit_no_selection")
                return

            self._finalize_and_output()
        except Exception as e:
            self.logger.exception("app_crash", error=str(e))
            raise
        finally:
            self.logger.info("app_exit")

    def _configure_platform(self):
        """Platform-specific setup."""
        if sys.platform == "win32":
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except AttributeError:
                pass

    def _parse_args(self):
        """Sets up argument parsing."""
        parser = argparse.ArgumentParser(
            description="Generate a prompt with project structure, file contents, and web content."
        )
        parser.add_argument(
            "inputs",
            nargs="*",
            help="Files, directories, or URLs to include. Defaults to current directory.",
        )
        parser.add_argument("-t", "--task", help="Task description for the AI prompt")
        parser.add_argument(
            "--reset-template",
            action="store_true",
            help="Reset the prompt template to default",
        )
        parser.add_argument(
            "--edit-template",
            action="store_true",
            help="Open the template file in default editor",
        )
        parser.add_argument(
            "--edit-profile",
            action="store_true",
            help="Open the global user profile (AI Identity) in default editor",
        )
        self.args = parser.parse_args()

        # Default to current dir if no inputs
        if not self.args.inputs:
            self.args.inputs.append(".")

    def _handle_utility_commands(self) -> bool:
        """Handles flags that exit immediately (reset/edit). Returns True if handled."""
        if self.args.reset_template:
            reset_template()
            return True

        if self.args.edit_template:
            open_template_in_editor()
            return True

        if self.args.edit_profile:
            open_profile_in_editor()
            return True

        return False

    def _load_configuration(self):
        """Loads all static configuration files."""
        self.ignore_patterns = read_gitignore()
        self.env_vars = read_env_file()
        self.user_profile = read_global_profile()
        self.project_context = read_project_context(self.project_root_abs)
        self.session_state = read_session_state(self.project_root_abs)
        self.is_ongoing_session = os.path.exists(self.session_path)

    def _load_session_state(self):
        """Loads previous session selection if active."""
        # 1. Always load previous file selection (Default: Reuse)
        cached_paths = load_selection_from_cache()
        for p in cached_paths:
            abs_p = os.path.abspath(p)
            if os.path.exists(abs_p):
                self.files_to_preselect.append(abs_p)

        # 2. Force Context and Session files into selection if session is active
        if self.is_ongoing_session:
            self.files_to_preselect.append(self.session_path)
            context_path = os.path.join(self.project_root_abs, "AI_CONTEXT.md")
            if os.path.exists(context_path):
                self.files_to_preselect.append(context_path)

    def _process_inputs(self):
        """Processes CLI arguments (files vs URLs)."""
        for input_path in self.args.inputs:
            if input_path.startswith(("http://", "https://")):
                self._handle_web_input(input_path)
            else:
                self._handle_file_input(input_path)

    def _handle_file_input(self, input_path: str):
        abs_path = os.path.abspath(input_path)
        if os.path.exists(abs_path):
            self.paths_for_tree.append(input_path)
            if os.path.isfile(abs_path):
                self.files_to_preselect.append(abs_path)
        else:
            print(f"Warning: {input_path} does not exist. Skipping.")

    def _handle_web_input(self, url: str):
        file_tuple, full_content, snippet = fetch_web_content(url)
        if file_tuple is None or full_content is None or snippet is None:
            return

        is_large = len(full_content) > 10000
        content = full_content
        is_snippet = False

        if is_large:
            print(f"\nContent from {url} is large. Here's a snippet:\n")
            print(get_colored_code(url, snippet))
            print("\n" + "-" * 40 + "\n")

            while True:
                choice = input("Use (f)ull content or (s)nippet? ").lower()
                if choice in ["f", "s"]:
                    break
                print("Invalid choice. Please enter 'f' or 's'.")

            if choice == "s":
                content = snippet
                is_snippet = True
                print("Using snippet.")
            else:
                print("Using full content.")

        # Reconstruct tuple with snippet choice
        final_tuple: FileTuple = (
            file_tuple[0],
            is_snippet,
            file_tuple[2],
            file_tuple[3],
        )

        self.web_contents[url] = (final_tuple, content)
        self.current_char_count += len(content)
        print(f"Added {'snippet of ' if is_snippet else ''}web content from: {url}")
        print_char_count(self.current_char_count)

    def _run_interactive_selection(self):
        """Runs the TreeSelector UI."""
        if not self.paths_for_tree:
            return

    def _run_interactive_selection(self):
        """Runs the TreeSelector UI."""
        if not self.paths_for_tree:
            return
    
        selected_files, file_char_count = run_tui(
            project_root=self.project_root_abs,
            ignore_patterns=self.ignore_patterns,
            files_to_preselect=self.files_to_preselect,
        )
        self.files_to_include.extend(selected_files)
        self.current_char_count += file_char_count

    def _finalize_and_output(self):
        """Generates the prompt, handles task input, and copies to clipboard."""
        # Cache the selection
        if self.files_to_include:
            save_selection_to_cache(self.files_to_include)

        print("\nFile and web content selection complete.")
        print_char_count(self.current_char_count)

        added_files_count = len(self.files_to_include)
        added_web_count = len(self.web_contents)
        print(
            f"Summary: Added {added_files_count} files/patches and {added_web_count} web sources."
        )

        # Deduplicate auto-loaded memory files
        self._deduplicate_memory_files()

        # Generate Template
        prompt_template, cursor_position = generate_prompt_template(
            self.files_to_include,
            self.ignore_patterns,
            self.web_contents,
            self.env_vars,
            self.paths_for_tree,
            user_profile=self.user_profile,
            project_context=self.project_context,
            session_state=self.session_state,
        )

        # Get Task
        task_description = self._get_task_description()
        if not task_description:
            self.console.print("\n[bold red]No task provided. Aborting.[/bold red]")
            return

        # Combine
        final_prompt = (
            prompt_template[:cursor_position]
            + task_description
            + prompt_template[cursor_position:]
        )
        final_prompt = sanitize_string(final_prompt)

        # Output
        self._print_and_copy(final_prompt)

        self.logger.info("prompt_generated", char_count=len(final_prompt))

    def _deduplicate_memory_files(self):
        """
        Removes Context/Session files from the generic file list if they are
        already injected via the quad-memory template slots.
        """
        self.files_to_include = [
            f
            for f in self.files_to_include
            if not (os.path.basename(f[0]) == "AI_CONTEXT.md" and self.project_context)
            and not (os.path.basename(f[0]) == "AI_SESSION.md" and self.session_state)
        ]

    def _get_task_description(self) -> str:
        cached_task: Optional[str] = None
        task_arg: Optional[str] = self.args.task
        if not task_arg:
            cached_task = load_task_from_cache()

        if task_arg:
            self.console.print(
                "\n[bold cyan]Using task description from --task argument.[/bold cyan]"
            )
            task: str = task_arg
        else:
            task = get_task_from_user_interactive(
                self.console, default_text=cached_task or ""
            )

        if task:
            save_task_to_cache(task)
        return task

    def _print_and_copy(self, final_prompt: str):
        print("\n\nGenerated prompt:")
        print("-" * 80)
        print(final_prompt)
        print("-" * 80)

        try:
            pyperclip.copy(final_prompt)
            print("\n--- Included Files & Content ---\n")
            for file_path, is_snippet, chunks, _ in sorted(
                self.files_to_include, key=lambda x: x[0]
            ):
                details = []
                if is_snippet:
                    details.append("snippet")
                if chunks is not None:
                    details.append(f"{len(chunks)} patches")

                detail_str = f" ({', '.join(details)})" if details else ""
                print(f"- {os.path.relpath(file_path)}{detail_str}")

            for url, (file_tuple, _) in sorted(self.web_contents.items()):
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


def main():
    app = KopipastaApp()
    app.run()


if __name__ == "__main__":
    main()
