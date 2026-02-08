import os
import shutil
import subprocess
import uuid
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from jinja2 import Template

from kopipasta.file import FileTuple, read_file_contents, is_ignored
from prompt_toolkit import prompt as prompt_toolkit_prompt
from prompt_toolkit.styles import Style
from rich.console import Console

CURSOR_MARKER = "<<CURSOR_POSITION>>"

DEFAULT_TEMPLATE = """{% if user_profile -%}
# User Profile & Preferences
{{ user_profile }}

{% endif -%}
{% if project_context -%}
# Project Constitution (AI_CONTEXT.md)
{{ project_context }}

{% endif -%}
{% if session_state -%}
# Current Working Session (AI_SESSION.md)
{{ session_state }}

{% endif -%}
# Project Overview

## Project Structure

```
{{ structure }}
```

## File Contents

{% for file in files -%}
### {{ file.path }}{{ file.description }}

```{{ file.language }}
{{ file.content }}
```

{% endfor -%}
{% if web_pages -%}
## Web Content

{% for page in web_pages -%}
### {{ page.url }}{{ page.description }}

```{{ page.language }}
{{ page.content }}
```

{% endfor -%}
{% endif -%}
## Task Instructions

{{ cursor_marker }}

## Instructions for Achieving the Task

### 🧠 Core Philosophy
1. **No Hallucinations**: You see the ## Project Structure. If you need to read a file that isn't in ## File Contents, stop and ask me to paste it.
2. **Critical Partner**: Do not blindly follow instructions if they are flawed. Challenge assumptions. Propose better architectural solutions.
3. **Hard Stops**: If you need user input, end with [AWAITING USER RESPONSE]. Do not guess.

### 🛠️ Code Output & Patching (CRITICAL)
I use a local tool to auto-apply your code blocks. You MUST follow these rules or I will lose data:

**Rule 1: File Headers**
Every code block must start with a comment line specifying the file path.
Example: `// FILE: src/utils.py` or `# FILE: config.toml`

**Rule 2: Modification vs. Creation**
- **To EDIT an existing file**: You MUST use **Unified Diff** format (with `@@ ... @@` headers). Do NOT post snippets without diff headers, or my tool will overwrite the whole file with just the snippet.
- **To CREATE or OVERWRITE a file**: Provide the **FULL** file content. Do not use lazy comments like `// ... rest of code ...` inside the block.
- **To DELETE a file**: Output a code block containing exactly `<<<DELETE>>>`.

### 🚀 Workflow
1. **Analyze**: Briefly restate the goal. **Assess the Context**: Identify missing files OR irrelevant files that clutter the context. If I provided too much, list exactly which files to keep for the next run. **Ask to confirm.** End with [AWAITING USER RESPONSE].
2. **Plan & Execute**: ONCE CONFIRMED, outline your approach and provide the code blocks (Diffs or Full Files).
3. **Verify**: Suggest a command to test the changes.
"""


def _get_config_dir() -> Path:
    """Returns the configuration directory, creating it if necessary."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        config_dir = Path(config_home) / "kopipasta"
    else:
        config_dir = Path.home() / ".config" / "kopipasta"
    
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_template_path() -> Path:
    """Returns the path to the user's prompt template file."""
    return _get_config_dir() / "prompt_template.j2"


def ensure_template_exists():
    """Ensures the prompt template exists. If not, creates it from default."""
    template_path = get_template_path()
    if not template_path.exists():
        reset_template()


def reset_template():
    """Overwrites the user's template with the default template."""
    template_path = get_template_path()
    try:
        with open(template_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TEMPLATE)
        print(f"Template reset to default at: {template_path}")
    except IOError as e:
        print(f"Error writing template file: {e}")


def open_template_in_editor():
    """Opens the template file in the system default editor."""
    ensure_template_exists()
    template_path = get_template_path()
    
    editor = os.environ.get("EDITOR", "code" if shutil.which("code") else "vim")
    
    if sys.platform == "win32":
        os.startfile(template_path)
    elif sys.platform == "darwin":
        subprocess.call(("open", template_path))
    else:
        subprocess.call((editor, template_path))


def load_template() -> Template:
    """Loads the Jinja2 template from disk or uses default if loading fails."""
    ensure_template_exists()
    template_path = get_template_path()
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Template(content, keep_trailing_newline=True)
    except Exception as e:
        print(f"Warning: Could not load template from {template_path}: {e}")
        print("Using default template fallback.")
        return Template(DEFAULT_TEMPLATE, keep_trailing_newline=True)


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
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as file:
            for i, line in enumerate(file):
                if i >= max_lines or byte_count >= max_bytes:
                    break
                snippet += line
                byte_count += len(line.encode("utf-8"))
    except Exception as e:
        return f"<Error reading snippet: {e}>"
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
    user_profile: Optional[str] = None,
    project_context: Optional[str] = None,
    session_state: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Generates the prompt using the Jinja2 template.
    Returns (rendered_prompt_string, cursor_position_index).
    """
    env_decisions = {}
    
    # 1. Prepare Project Structure
    structure_tree = get_project_structure(ignore_patterns, search_paths)

    # 2. Prepare File Contents List
    processed_files = []
    for file, use_snippet, chunks, content_type in files_to_include:
        relative_path = os.path.relpath(file)
        language = content_type if content_type else get_language_for_file(file)
        description = ""
        content = ""

        if chunks is not None:
            description = " (selected patches)"
            # Chunks are already strings, just join them
            content = "\n".join(chunks)
        elif use_snippet:
            description = " (snippet)"
            content = get_file_snippet(file)
        else:
            raw_content = read_file_contents(file)
            content = handle_env_variables(raw_content, env_vars, env_decisions)

        processed_files.append({
            "path": relative_path,
            "relative_path": relative_path,
            "description": description,
            "language": language,
            "content": content
        })

    # 3. Prepare Web Contents List
    processed_web_pages = []
    if web_contents:
        for url, (file_tuple, raw_content) in web_contents.items():
            _, is_snippet, _, content_type = file_tuple
            safe_content = handle_env_variables(raw_content, env_vars, env_decisions)
            
            # Default empty lang for HTML/Web content unless specified (json/csv)
            language = content_type if content_type in ["json", "csv"] else ""
            
            processed_web_pages.append({
                "url": url,
                "description": " (snippet)" if is_snippet else "",
                "language": language,
                "content": safe_content
            })

    # 4. Render Template
    template = load_template()
    
    # Use a unique marker for this render to prevent collision if the 
    # CURSOR_MARKER constant string itself appears in the file contents.
    unique_render_marker = f"{CURSOR_MARKER}_{uuid.uuid4().hex}"
    rendered = template.render(
        structure=structure_tree,
        files=processed_files,
        web_pages=processed_web_pages,
        cursor_marker=unique_render_marker,
        user_profile=user_profile,
        project_context=project_context,
        session_state=session_state,
    )

    # 5. Find and remove cursor marker
    cursor_position = rendered.find(unique_render_marker)
    if cursor_position == -1:
        # Fallback if user deleted marker from template: append to end
        cursor_position = len(rendered)
    else:
        rendered = rendered.replace(unique_render_marker, "", 1)

    return rendered, cursor_position


def get_task_from_user_interactive(console: Console, default_text: str = "") -> str:
    """
    Prompts the user for a multiline task description using an interactive
    terminal prompt instead of an external editor.
    """
    console.print("\n[bold cyan]📝 Please enter your task instructions.[/bold cyan]")
    if default_text:
        console.print(f"   [dim](Pre-filled from previous session. Edit or clear as needed.)[/dim]")
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
            default=default_text,
        )
        return task.strip()
    except KeyboardInterrupt:
        return ""