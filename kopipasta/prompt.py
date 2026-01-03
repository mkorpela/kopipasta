import os
from kopipasta.file import FileTuple, read_file_contents, is_ignored
from prompt_toolkit import prompt as prompt_toolkit_prompt
from prompt_toolkit.styles import Style
from rich.console import Console

from typing import Dict, List, Tuple


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


def get_file_snippet(file_path, max_lines=50, max_bytes=4096):
    snippet = ""
    byte_count = 0
    with open(file_path, "r") as file:
        for i, line in enumerate(file):
            if i >= max_lines or byte_count >= max_bytes:
                break
            snippet += line
            byte_count += len(line.encode("utf-8"))
    return snippet


def get_language_for_file(file_path):
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


def get_project_structure(ignore_patterns, search_paths=None):
    if not search_paths:
        search_paths = ["."]

    tree = []
    for start_path in search_paths:
        if os.path.isfile(start_path):
            if not is_ignored(start_path, ignore_patterns):
                tree.append(f"|-- {os.path.basename(start_path)}")
            continue

        for root, dirs, files in os.walk(start_path):
            dirs.sort()
            files.sort()
            dirs[:] = [
                d
                for d in dirs
                if not is_ignored(os.path.join(root, d), ignore_patterns)
            ]
            files = [
                f
                for f in files
                if not is_ignored(os.path.join(root, f), ignore_patterns)
            ]

            rel_path = os.path.relpath(root, start_path)
            level = 0 if rel_path == "." else rel_path.count(os.sep) + 1

            indent = " " * 4 * level + "|-- "
            display_name = start_path if level == 0 else os.path.basename(root)
            tree.append(f"{indent}{display_name}/")

            subindent = " " * 4 * (level + 1) + "|-- "
            for f in files:
                tree.append(f"{subindent}{f}")

    return "\n".join(tree)


def handle_env_variables(content, env_vars, decisions_cache: Dict[str, str] = None):
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
        print("Detected environment variables:")
        for key, value in undecided_vars:
            print(f"- {key}={value}")

        for key, value in undecided_vars:
            while True:
                choice = input(
                    f"How would you like to handle {key}? (m)ask / (s)kip / (k)eep: "
                ).lower()
                if choice in ["m", "s", "k"]:
                    break
                print("Invalid choice. Please enter 'm', 's', or 'k'.")
            decisions_cache[key] = choice

    for key, value in detected_vars:
        choice = decisions_cache.get(key, "k")
        if choice == "m":
            content = content.replace(value, "*" * len(value))
        elif choice == "s":
            content = content.replace(value, "[REDACTED]")
        # If 'k', we don't modify the content

    return content


def generate_prompt_template(
    files_to_include: List[FileTuple],
    ignore_patterns: List[str],
    web_contents: Dict[str, Tuple[FileTuple, str]],
    env_vars: Dict[str, str],
    search_paths: List[str] = None,
) -> Tuple[str, int]:
    env_decisions = {}

    prompt = "# Project Overview\n\n"
    prompt += "## Project Structure\n\n"
    prompt += "```\n"
    prompt += get_project_structure(ignore_patterns, search_paths)
    prompt += "\n```\n\n"
    prompt += "## File Contents\n\n"
    for file, use_snippet, chunks, content_type in files_to_include:
        relative_path = os.path.relpath(file)
        language = content_type if content_type else get_language_for_file(file)

        if chunks is not None:
            prompt += f"### {relative_path} (selected patches)\n\n```{language}\n"
            for chunk in chunks:
                prompt += f"{chunk}\n"
            prompt += "```\n\n"
        elif use_snippet:
            file_content = get_file_snippet(file)
            prompt += f"### {relative_path} (snippet)\n\n```{language}\n{file_content}\n```\n\n"
        else:
            file_content = read_file_contents(file)
            file_content = handle_env_variables(file_content, env_vars, env_decisions)
            prompt += f"### {relative_path}\n\n```{language}\n{file_content}\n```\n\n"

    if web_contents:
        prompt += "## Web Content\n\n"
        for url, (file_tuple, content) in web_contents.items():
            _, is_snippet, _, content_type = file_tuple
            content = handle_env_variables(content, env_vars, env_decisions)
            language = content_type if content_type in ["json", "csv"] else ""
            prompt += f"### {url}{' (snippet)' if is_snippet else ''}\n\n```{language}\n{content}\n```\n\n"

    prompt += "## Task Instructions\n\n"
    cursor_position = len(prompt)
    prompt += "\n\n"
    prompt += "## Instructions for Achieving the Task\n\n"
    analysis_text = (
        "### 🧠 Core Philosophy\n"
        "1. **No Hallucinations**: You see the ## Project Structure. If you need to read a file that isn't in ## File Contents, stop and ask me to paste it.\n"
        "2. **Critical Partner**: Do not blindly follow instructions if they are flawed. Challenge assumptions. Propose better architectural solutions.\n"
        "3. **Hard Stops**: If you need user input, end with [AWAITING USER RESPONSE]. Do not guess.\n\n"
        "### 🛠️ Code Output & Patching (CRITICAL)\n"
        "I use a local tool to auto-apply your code blocks. You MUST follow these rules or I will lose data:\n\n"
        "**Rule 1: File Headers**\n"
        "Every code block must start with a comment line specifying the file path.\n"
        "Example: `// FILE: src/utils.py` or `# FILE: config.toml`\n\n"
        "**Rule 2: Modification vs. Creation**\n"
        "- **To EDIT an existing file**: You MUST use **Unified Diff** format (with `@@ ... @@` headers). Do NOT post snippets without diff headers, or my tool will overwrite the whole file with just the snippet.\n"
        "- **To CREATE or OVERWRITE a file**: Provide the **FULL** file content. Do not use lazy comments like `// ... rest of code ...` inside the block.\n\n"
        "### 🚀 Workflow\n"
        "1. **Analyze**: Briefly restate the goal. **Assess the Context**: Identify missing files OR irrelevant files that clutter the context. If I provided too much, list exactly which files to keep for the next run. **Ask to confirm.** End with [AWAITING USER RESPONSE].\n"
        "2. **Plan & Execute**: ONCE CONFIRMED, outline your approach and provide the code blocks (Diffs or Full Files).\n"
        "3. **Verify**: Suggest a command to test the changes.\n"
    )
    prompt += analysis_text
    return prompt, cursor_position


def get_task_from_user_interactive(console: Console) -> str:
    """
    Prompts the user for a multiline task description using an interactive
    terminal prompt instead of an external editor.
    """
    console.print("\n[bold cyan]📝 Please enter your task instructions.[/bold cyan]")
    console.print(
        "   - Press [bold]Meta+Enter[/bold] or [bold]Esc[/bold] then [bold]Enter[/bold] to submit."
    )
    console.print("   - Press [bold]Ctrl-C[/bold] to abort.")

    style = Style.from_dict({"": "#00ff00"})

    try:
        task = prompt_toolkit_prompt(
            "> ",
            multiline=True,
            prompt_continuation="  ",
            style=style,
        )
        return task.strip()
    except KeyboardInterrupt:
        return ""