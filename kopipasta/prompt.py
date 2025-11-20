import os
from kopipasta.file import FileTuple, read_file_contents, is_ignored
from prompt_toolkit import prompt as prompt_toolkit_prompt
from prompt_toolkit.styles import Style
from rich.console import Console

from typing import Dict, List, Tuple


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


def get_project_structure(ignore_patterns):
    tree = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [
            d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)
        ]
        files = [
            f for f in files if not is_ignored(os.path.join(root, f), ignore_patterns)
        ]
        level = root.replace(".", "").count(os.sep)
        indent = " " * 4 * level + "|-- "
        tree.append(f"{indent}{os.path.basename(root)}/")
        subindent = " " * 4 * (level + 1) + "|-- "
        for f in files:
            tree.append(f"{subindent}{f}")
    return "\n".join(tree)


def handle_env_variables(content, env_vars):
    detected_vars = []
    for key, value in env_vars.items():
        # Only detect if value is not empty and present in content
        if value and value in content:
            detected_vars.append((key, value))
    if not detected_vars:
        return content

    print("Detected environment variables:")
    for key, value in detected_vars:
        print(f"- {key}={value}")

    for key, value in detected_vars:
        while True:
            choice = input(
                f"How would you like to handle {key}? (m)ask / (s)kip / (k)eep: "
            ).lower()
            if choice in ["m", "s", "k"]:
                break
            print("Invalid choice. Please enter 'm', 's', or 'k'.")

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
) -> Tuple[str, int]:
    prompt = "# Project Overview\n\n"
    prompt += "## Project Structure\n\n"
    prompt += "```\n"
    prompt += get_project_structure(ignore_patterns)
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
            file_content = handle_env_variables(file_content, env_vars)
            prompt += f"### {relative_path}\n\n```{language}\n{file_content}\n```\n\n"

    if web_contents:
        prompt += "## Web Content\n\n"
        for url, (file_tuple, content) in web_contents.items():
            _, is_snippet, _, content_type = file_tuple
            content = handle_env_variables(content, env_vars)
            language = content_type if content_type in ["json", "csv"] else ""
            prompt += f"### {url}{' (snippet)' if is_snippet else ''}\n\n```{language}\n{content}\n```\n\n"

    prompt += "## Task Instructions\n\n"
    cursor_position = len(prompt)
    prompt += "\n\n"
    prompt += "## Instructions for Achieving the Task\n\n"
    analysis_text = (
        "### Partnership Principles\n\n"
        "We work as collaborative partners. You provide technical expertise and critical thinking. "
        "I have exclusive access to my codebase, real environment, external services, and actual users. "
        "Never assume project file contents - always ask to see them.\n\n"
        "**Critical Thinking**: Challenge poor approaches, identify risks, suggest better alternatives. Don't be a yes-man.\n\n"
        "**Anti-Hallucination**: Never write placeholder code for files in ## Project Structure. Use [STOP - NEED FILE: filename] and wait.\n\n"
        "**Hard Stops**: End with [AWAITING USER RESPONSE] when you need input. Don't continue with assumptions.\n\n"
        "### Development Workflow\n\n"
        "We work in two modes:\n"
        "- **Iterative Mode**: Build incrementally, show only changes\n"
        "- **Consolidation Mode**: When I request, provide clean final version\n\n"
        "1. **Understand & Analyze**:\n"
        "   - Rephrase task, identify issues, list needed files\n"
        "   - Challenge problematic aspects\n"
        "   - End: 'I need: [files]. Is this correct?' [AWAITING USER RESPONSE]\n\n"
        "2. **Plan**:\n"
        "   - Present 2-3 approaches with pros/cons\n"
        "   - Recommend best approach\n"
        "   - End: 'Which approach?' [AWAITING USER RESPONSE]\n\n"
        "3. **Implement Iteratively**:\n"
        "   - Small, testable increments\n"
        "   - Track failed attempts: `Attempt 1: [FAILED] X→Y (learned: Z)`\n"
        "   - After 3 failures, request diagnostics\n\n"
        "4. **Code Presentation**:\n"
        "   - Always: `// FILE: path/to/file.ext`\n"
        "   - Iterative: Show only changes with context\n"
        "   - Consolidation: Smart choice - minimal changes = show patches, extensive = full file\n\n"
        "5. **Test & Validate**:\n"
        "   - 'Test with: [command]. Share any errors.' [AWAITING USER RESPONSE]\n"
        "   - Include debug outputs\n"
        "   - May return to implementation based on results\n\n"
        "### Permissions & Restrictions\n\n"
        "**You MAY**: Request project files, ask me to test code/services, challenge my approach, refuse without info\n\n"
        "**You MUST NOT**: Assume project file contents, continue past [AWAITING USER RESPONSE], be agreeable when you see problems\n"
    )
    prompt += analysis_text
    return prompt, cursor_position


def get_task_from_user_interactive(console: Console) -> str:
    """
    Prompts the user for a multiline task description using an interactive
    terminal prompt instead of an external editor.
    """
    console.print("\n[bold cyan]📝 Please enter your task instructions.[/bold cyan]")
    console.print("   - Press [bold]Meta+Enter[/bold] or [bold]Esc[/bold] then [bold]Enter[/bold] to submit.")
    console.print("   - Press [bold]Ctrl-C[/bold] to abort.")

    style = Style.from_dict({'': '#00ff00'})

    try:
        task = prompt_toolkit_prompt(
            '> ',
            multiline=True,
            prompt_continuation='  ',
            style=style,
        )
        return task.strip()
    except KeyboardInterrupt:
        return ""
