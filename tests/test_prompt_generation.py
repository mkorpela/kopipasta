import json
import os
from unittest.mock import patch

import pytest

from kopipasta.core.context import LEGEND
from kopipasta.prompt import DEFAULT_TEMPLATE, generate_prompt_template


@pytest.fixture
def default_template(tmp_path, monkeypatch):
    """A pristine template on disk, whatever the developer's own looks like."""
    from kopipasta.prompt import get_template_path

    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    get_template_path().write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    from kopipasta import file as filemod

    filemod._is_ignored_cache.clear()
    filemod._is_binary_cache.clear()
    filemod._gitignore_cache.clear()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_generate_prompt_template_regression(default_template):
    """The exact bytes of the clipboard prompt.

    The context half of this — everything from `# Project Overview` down to
    the last zone — is rendered by `kopipasta.core.context`, the same call
    `ask` makes. So this pins the shared shape as well as the template's own
    composition; `tests/test_shared_rendering.py` is what pins them *together*.
    """
    (default_template / "file.py").write_text("print('hello world')\n")
    (default_template / "large.py").write_text("print('snippet')\n")
    (default_template / "patched.py").write_text("irrelevant: chunks win\n")

    result, cursor_pos = generate_prompt_template(
        files_to_include=[
            ("file.py", False, None, "python"),  # Full file
            ("large.py", True, None, "python"),  # Snippet
            ("patched.py", False, ["line 1", "line 2"], "python"),  # Patches
        ],
        ignore_patterns=[],
        web_contents={
            "http://example.com": (
                ("http://example.com", False, None, "html"),
                "<html>web</html>",
            )
        },
        env_vars={},  # Empty to avoid interaction
        search_paths=["."],
        root=str(default_template),
    )

    expected_parts = [
        "# Project Overview\n\n",
        "## Project Structure\n\n",
        LEGEND + "\n\n",
        "```json\n",
        '{"file.py":[],"large.py":[],"patched.py":[]}\n',
        "```\n\n",
        # A flat file list has no Delta/Base to preserve, so it all lands in
        # the working set. What it must never do is land in no zone at all.
        "## Working Set (Focus Here)\n\n",
        "The task centres on these files. They are sent whole and are never "
        "trimmed to fit a budget.\n\n",
        "# FILE: file.py\n",
        "```python\n",
        "print('hello world')\n",
        "```\n\n",
        "# FILE: patched.py (selected patches)\n",
        "```python\n",
        "line 1\n",
        "line 2\n",
        "```\n\n",
        "## Snippets (partial files)\n\n",
        "Only the first lines of each file are shown. Ask for the rest if you need it.\n\n",
        "# FILE: large.py (first 50 lines only)\n",
        "```python\n",
        "print('snippet')\n",
        "```\n\n",
        "## Web Content\n\n",
        "### http://example.com\n\n",
        "```\n",
        "<html>web</html>\n",
        "```\n\n",
        "## Task Instructions\n\n",
        # CURSOR POSITION HERE
        "\n\n",
        "## Instructions for Achieving the Task\n\n",
        "### 🧠 Core Philosophy\n",
        "1. **No Hallucinations**: You see the ## Project Structure. If you need to read a file whose contents are not under one of the zone headings above, stop and ask me to paste it.\n",
        "2. **Respect the Zones**: The task centres on ## Working Set (Focus Here); keep changes there where you can. Files under ## Supporting Context are mostly there to be read — change one only if the task genuinely needs it, and say why.\n",
        "3. **Critical Partner**: Do not blindly follow instructions if they are flawed. Challenge assumptions. Propose better architectural solutions.\n",
        "4. **Hard Stops**: If you need user input, end with [AWAITING USER RESPONSE]. Do not guess.\n\n",
        "### 🛠️ Code Output & Patching (CRITICAL)\n",
        "I use a local tool to auto-apply your code blocks. You MUST follow these rules:\n\n",
        "**Rule 1: File Headers**\n",
        "Every code block must start with a comment line specifying the file path.\n",
        "Example: `// FILE: src/utils.py` or `# FILE: config.toml`\n\n",
        "**Rule 2: Modification vs. Creation**\n",
        "- **To EDIT an existing file**: Use **Unified Diff** format (with `@@ ... @@` headers) OR **Search/Replace** blocks (`<<<<` ... `====` ... `>>>>`).\n",
        "- **To CREATE or OVERWRITE a file**: Provide the **FULL** file content.\n",
        "- **To DELETE a file**: Output a code block containing exactly `<<<DELETE>>>`.\n\n",
        "**Rule 3: The Reset Marker**\n",
        "If you realize you made a mistake earlier in your response, output `<<<RESET>>>` on a new line. My tool will ignore everything before that marker and only process patches following it.\n\n",
        "### 🚀 Workflow\n",
        "1. **Analyze**: Briefly restate the goal. **Assess the Context**: Identify missing files OR irrelevant files that clutter the context. If I provided too much, list exactly which files to keep for the next run. **Ask to confirm.** End with [AWAITING USER RESPONSE].\n",
        "2. **Plan & Execute**: ONCE CONFIRMED, outline your approach and provide the code blocks (Diffs or Full Files).\n",
        "3. **Verify**: Suggest a command to test the changes.\n",
    ]

    assert result == "".join(expected_parts)

    # The cursor still lands between the task heading and the instructions.
    assert result[:cursor_pos].endswith("## Task Instructions\n\n")
    assert result[cursor_pos:].startswith("\n\n## Instructions for Achieving the Task")


def test_generate_extension_prompt(tmp_path):
    import os

    from kopipasta.file import FileTuple
    from kopipasta.prompt import generate_extension_prompt

    f1 = tmp_path / "new_logic.py"
    f1.write_text("def added(): pass")

    # FileTuple: (path, is_snippet, chunks, content_type)
    files: list[FileTuple] = [(str(f1), False, None, "python")]

    # Change CWD to tmp_path so relpath returns expected short name
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        prompt = generate_extension_prompt(files, {})
    finally:
        os.chdir(old_cwd)

    assert "Here are the additional files you requested:" in prompt
    assert "# FILE: new_logic.py" in prompt
    assert "def added(): pass" in prompt
    assert "```python" in prompt


def test_get_project_structure_returns_json(tmp_path):
    """get_project_structure returns a minified JSON string with symbol lists."""
    from kopipasta.prompt import get_project_structure

    main_py = tmp_path / "main.py"
    main_py.write_text("def main(): pass\n")
    (tmp_path / "sub").mkdir()
    util_py = tmp_path / "sub" / "util.py"
    util_py.write_text("class Util: pass\n")
    unmapped_py = tmp_path / "sub" / "unmapped.py"
    unmapped_py.write_text("def hidden(): pass\n")
    (tmp_path / "notes.md").write_text("# Notes")

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = get_project_structure(
            [], search_paths=["."], map_files=[str(main_py), str(util_py)]
        )
    finally:
        os.chdir(old_cwd)

    parsed = json.loads(result)
    assert "main.py" in parsed
    assert parsed["main.py"] == ["def main()"]
    assert "sub" in parsed
    assert "util.py" in parsed["sub"]
    assert parsed["sub"]["util.py"] == ["class Util"]
    assert "unmapped.py" in parsed["sub"]
    assert parsed["sub"]["unmapped.py"] == []  # Not in map_files
    assert "notes.md" in parsed
    assert parsed["notes.md"] == []  # Non-Python file has no symbols


@patch("kopipasta.prompt.load_template")
def test_generate_prompt_template_with_map_files(mock_load_template, tmp_path):
    """MAP files are included in the JSON Project Structure, but NOT in File Contents."""
    from jinja2 import Template

    from kopipasta.prompt import DEFAULT_TEMPLATE

    py_file = tmp_path / "service.py"
    py_file.write_text("class Service:\n    def run(self):\n        return 'running'\n")

    mock_load_template.return_value = Template(
        DEFAULT_TEMPLATE, keep_trailing_newline=True
    )

    mock_load_template.return_value = Template(
        DEFAULT_TEMPLATE, keep_trailing_newline=True
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result, _ = generate_prompt_template(
            files_to_include=[],
            ignore_patterns=[],
            web_contents={},
            env_vars={},
            search_paths=["."],
            map_files=[str(py_file)],
        )
    finally:
        os.chdir(old_cwd)

    # Should be in the JSON tree structure
    assert '"service.py":["class Service [run]"]' in result
    # Should NOT be in the file contents
    assert "# FILE: service.py (map)" not in result
    assert "class Service:" not in result
