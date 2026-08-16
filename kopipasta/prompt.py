"""The clipboard prompt: the user's template, composed and copied.

This module belongs to the TUI. It owns the Jinja template, the `$EDITOR`
launcher and the composition that ends on the clipboard — none of which the
agent CLI can use, and none of which it should have to load.

The rendering primitives it used to define now live in `kopipasta.core.render`
and the interactive task prompt in `kopipasta.interaction`, because
`core/context.py` needed the former and `core/ask.py` the latter. They are
re-exported here so existing imports keep working; new code should take them
from core.
"""

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from jinja2 import Template

# Re-exported for callers that predate the split. The definitions are in core
# so that importing them cannot drag the template engine and the terminal UI
# in with them — see tests/test_delivery_boundary.py.
from kopipasta.core.render import (  # noqa: F401
    get_file_snippet,
    get_language_for_file,
    get_project_structure,
    handle_env_variables,
)
from kopipasta.file import FileTuple
from kopipasta.interaction import get_task_from_user_interactive, require_human

if TYPE_CHECKING:  # pragma: no cover - core.context is imported lazily below
    from kopipasta.core.resolver import Selection

__all__ = [
    "CURSOR_MARKER",
    "DEFAULT_TEMPLATE",
    "EXTENSION_TEMPLATE",
    "ensure_template_exists",
    "generate_extension_prompt",
    "generate_prompt_template",
    "get_file_snippet",
    "get_language_for_file",
    "get_project_structure",
    "get_task_from_user_interactive",
    "get_template_path",
    "handle_env_variables",
    "load_template",
    "open_template_in_editor",
    "reset_template",
    "selection_from_files",
]

CURSOR_MARKER = "<<CURSOR_POSITION>>"

DEFAULT_TEMPLATE = """{{ memory }}{{ context }}
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
1. **No Hallucinations**: You see the ## Project Structure. If you need to read a file whose contents are not under one of the zone headings above, stop and ask me to paste it.
2. **Respect the Zones**: Propose changes only to files under ## Active Workspace (Editable). Files under ## Reference Context are there to be read; if one of them has to change, say so and ask me to move it.
3. **Critical Partner**: Do not blindly follow instructions if they are flawed. Challenge assumptions. Propose better architectural solutions.
4. **Hard Stops**: If you need user input, end with [AWAITING USER RESPONSE]. Do not guess.

### 🛠️ Code Output & Patching (CRITICAL)
I use a local tool to auto-apply your code blocks. You MUST follow these rules:

**Rule 1: File Headers**
Every code block must start with a comment line specifying the file path.
Example: `// FILE: src/utils.py` or `# FILE: config.toml`

**Rule 2: Modification vs. Creation**
- **To EDIT an existing file**: Use **Unified Diff** format (with `@@ ... @@` headers) OR **Search/Replace** blocks (`<<<<` ... `====` ... `>>>>`).
- **To CREATE or OVERWRITE a file**: Provide the **FULL** file content.
- **To DELETE a file**: Output a code block containing exactly `<<<DELETE>>>`.

**Rule 3: The Reset Marker**
If you realize you made a mistake earlier in your response, output `<<<RESET>>>` on a new line. My tool will ignore everything before that marker and only process patches following it.

### 🚀 Workflow
1. **Analyze**: Briefly restate the goal. **Assess the Context**: Identify missing files OR irrelevant files that clutter the context. If I provided too much, list exactly which files to keep for the next run. **Ask to confirm.** End with [AWAITING USER RESPONSE].
2. **Plan & Execute**: ONCE CONFIRMED, outline your approach and provide the code blocks (Diffs or Full Files).
3. **Verify**: Suggest a command to test the changes.
"""

EXTENSION_TEMPLATE = """Here are the additional files you requested:

{% for file in files -%}
# FILE: {{ file.path }}{{ file.description }}
```{{ file.language }}
{{ file.content }}
```
{% endfor -%}"""


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
    except OSError as e:
        print(f"Error writing template file: {e}")


def open_template_in_editor():
    """Opens the template file in the system default editor."""
    ensure_template_exists()
    template_path = get_template_path()

    # `$EDITOR` defaults to vim here, and a terminal editor launched onto a
    # pipe is the original bug in a different costume: it blocks forever with
    # nobody able to type `:q`. The file is created first and named in the
    # error, so a headless caller can still edit it by other means.
    require_human(
        f"Opening {template_path} in an editor",
        "The file exists and can be edited directly.",
    )

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
        with open(template_path, encoding="utf-8") as f:
            content = f.read()
        return Template(content, keep_trailing_newline=True)
    except Exception as e:
        print(f"Warning: Could not load template from {template_path}: {e}")
        print("Using default template fallback.")
        return Template(DEFAULT_TEMPLATE, keep_trailing_newline=True)


def selection_from_files(
    files_to_include: List[FileTuple],
    map_files: Optional[List[str]] = None,
    root: Optional[str] = None,
) -> "Selection":
    """A flat `FileTuple` list as a role-bearing selection.

    The fallback for callers that never had roles to lose — a cached
    selection, a test, the extension prompt. Everything lands in the active
    workspace because that is what an undifferentiated "selected" meant before
    the two vocabularies were joined up; a caller that *does* know its roles
    passes a `Selection` instead and keeps them.
    """
    from kopipasta.core.resolver import EDIT, MAP, SNIPPET, Entry, Selection

    root = root or os.getcwd()
    selection = Selection(root=root)

    def add(path: str, role: str, chunks: Optional[List[str]] = None) -> None:
        abs_path = os.path.abspath(path)
        selection.entries[abs_path] = Entry(
            path=abs_path,
            rel=os.path.relpath(abs_path, root).replace(os.sep, "/"),
            role=role,
            bulk=False,
            chunks=chunks,
        )

    for path in map_files or []:
        add(path, MAP)
    for path, use_snippet, chunks, _ in files_to_include:
        add(path, SNIPPET if use_snippet and chunks is None else EDIT, chunks)
    return selection


def generate_prompt_template(
    files_to_include: List[FileTuple],
    ignore_patterns: List[str],
    web_contents: Dict[str, Tuple[FileTuple, str]],
    env_vars: Dict[str, str],
    search_paths: Optional[List[str]] = None,
    user_profile: Optional[str] = None,
    project_context: Optional[str] = None,
    session_state: Optional[str] = None,
    map_files: Optional[List[str]] = None,
    selection: Optional["Selection"] = None,
    root: Optional[str] = None,
) -> Tuple[str, int]:
    """The clipboard prompt. Returns (rendered, cursor_position).

    The context — the structure tree, the legend and the file zones — is
    rendered by `kopipasta.core.context`, the same call `ask` makes before
    posting to a model. This function only composes it with the memory layers
    and the instruction tail, which is what the user's template is for. The
    file-block format lives in exactly one place, so the two surfaces cannot
    drift apart again without the shared test noticing.

    `selection` carries the TUI's Delta/Base roles when the caller has them;
    without it the file list is flattened into the active workspace.
    """
    from kopipasta.core.context import render_context, render_memory

    root = root or os.getcwd()
    if selection is None:
        selection = selection_from_files(files_to_include, map_files, root)

    env_decisions: Dict[str, str] = {}

    # 1. The memory prologue and the shared body, both from the renderer
    #    `ask` uses. Masking happens inside the latter.
    memory = render_memory(
        user_profile=user_profile,
        project_context=project_context,
        session_state=session_state,
    )
    context = render_context(
        selection,
        ignore=ignore_patterns,
        root=root,
        env_vars=env_vars,
        search_paths=search_paths,
    )

    # 2. Web content has no counterpart in a repository selection, so it stays
    #    a template slot rather than being forced into a zone.
    processed_web_pages = []
    if web_contents:
        for url, (file_tuple, raw_content) in web_contents.items():
            _, is_snippet, _, content_type = file_tuple
            safe_content = handle_env_variables(raw_content, env_vars, env_decisions)

            # Default empty lang for HTML/Web content unless specified (json/csv)
            language = content_type if content_type in ["json", "csv"] else ""

            processed_web_pages.append(
                {
                    "url": url,
                    "description": " (snippet)" if is_snippet else "",
                    "language": language,
                    "content": safe_content,
                }
            )

    # 3. Render Template
    template = load_template()

    # Use a unique marker for this render to prevent collision if the
    # CURSOR_MARKER constant string itself appears in the file contents.
    unique_render_marker = f"{CURSOR_MARKER}_{uuid.uuid4().hex}"
    rendered = template.render(
        memory=memory,
        context=context,
        web_pages=processed_web_pages,
        cursor_marker=unique_render_marker,
        # The three layers individually, for a template that lays them out
        # itself rather than taking the rendered prologue.
        user_profile=user_profile,
        project_context=project_context,
        session_state=session_state,
        # Kept for templates written before the context was shared. A custom
        # template that still loops over `files` keeps working; it just misses
        # the zones, which is a reason to re-run --reset-template, not a crash.
        structure=get_project_structure(ignore_patterns, search_paths, map_files),
        files=_legacy_file_dicts(selection),
    )

    # 4. Find and remove cursor marker
    cursor_position = rendered.find(unique_render_marker)
    if cursor_position == -1:
        # Fallback if user deleted marker from template: append to end
        cursor_position = len(rendered)
    else:
        rendered = rendered.replace(unique_render_marker, "", 1)

    return rendered, cursor_position


def _legacy_file_dicts(selection: "Selection") -> List[Dict[str, str]]:
    """`{{ files }}` for a template written before zones existed."""
    from kopipasta.core.context import entry_content, entry_note
    from kopipasta.core.resolver import EDIT, REF, SNIPPET

    out = []
    for entry in selection.by_role(EDIT, REF, SNIPPET):
        note = entry_note(entry)
        out.append(
            {
                "path": entry.rel,
                "relative_path": entry.rel,
                "description": f" ({note})" if note else "",
                "language": get_language_for_file(entry.path),
                "content": entry_content(entry),
            }
        )
    return out


def generate_extension_prompt(
    files_to_include: List[FileTuple],
    env_vars: Dict[str, str],
    selection: Optional["Selection"] = None,
    root: Optional[str] = None,
) -> str:
    """The follow-up paste: file blocks and nothing else.

    Third caller of the shared block format, and the same rule applies — a
    file that arrives here has to look exactly like the same file did in the
    first prompt, or the model sees two renderings of one path and has to
    guess which is current. `ask` solves that turn-to-turn problem in its own
    suffix; this is the clipboard's version of it.
    """
    from kopipasta.core.context import entry_content, entry_note
    from kopipasta.core.resolver import EDIT, REF, SNIPPET

    root = root or os.getcwd()
    if selection is None:
        selection = selection_from_files(files_to_include, root=root)

    env_decisions: Dict[str, str] = {}
    processed_files = []
    for entry in selection.by_role(EDIT, REF, SNIPPET):
        note = entry_note(entry)
        processed_files.append(
            {
                "path": entry.rel,
                "description": f" ({note})" if note else "",
                "language": get_language_for_file(entry.path),
                "content": handle_env_variables(
                    entry_content(entry), env_vars, env_decisions
                ),
            }
        )

    template = Template(EXTENSION_TEMPLATE, keep_trailing_newline=True)
    return template.render(files=processed_files)
