"""Tests for the fix workflow (x hotkey): config resolution, prompt generation, and path detection."""

import os
import platform
import stat
import textwrap
from unittest.mock import patch, MagicMock

import pytest

from kopipasta.config import read_fix_command
from kopipasta.prompt import generate_fix_prompt, FIX_TEMPLATE
from kopipasta.patcher import find_paths_in_text


# ============================================================
# 1. read_fix_command — Configuration Resolution
# ============================================================


class TestReadFixCommand:
    """Tests the three-tier fallback for fix command resolution."""

    def test_reads_from_ai_context_html_comment(self, tmp_path):
        """Tier 1: Explicit HTML comment in AI_CONTEXT.md wins."""
        ctx = tmp_path / "AI_CONTEXT.md"
        ctx.write_text(
            "# My Project\n"
            "<!-- KOPIPASTA_FIX_CMD: npm run lint --fix -->\n"
            "Some other content.\n"
        )
        assert read_fix_command(str(tmp_path)) == "npm run lint --fix"

    def test_html_comment_with_extra_whitespace(self, tmp_path):
        """Whitespace around the command is stripped."""
        ctx = tmp_path / "AI_CONTEXT.md"
        ctx.write_text("<!--   KOPIPASTA_FIX_CMD:   ruff check .   -->")
        assert read_fix_command(str(tmp_path)) == "ruff check ."

    def test_html_comment_buried_in_document(self, tmp_path):
        """Comment can appear anywhere in the file, not just the top."""
        ctx = tmp_path / "AI_CONTEXT.md"
        ctx.write_text(
            "# Project Constitution\n\n"
            "## Architecture\n"
            "Blah blah blah.\n\n"
            "## Config\n"
            "<!-- KOPIPASTA_FIX_CMD: make check -->\n\n"
            "## More stuff\n"
        )
        assert read_fix_command(str(tmp_path)) == "make check"

    def test_html_comment_with_complex_command(self, tmp_path):
        """Commands with pipes, &&, and flags are preserved."""
        cmd = "cd frontend && npm run lint 2>&1 | head -50"
        ctx = tmp_path / "AI_CONTEXT.md"
        ctx.write_text(f"<!-- KOPIPASTA_FIX_CMD: {cmd} -->")
        assert read_fix_command(str(tmp_path)) == cmd

    @pytest.mark.skipif(platform.system() == "Windows", reason="POSIX executable check")
    def test_falls_back_to_pre_commit_hook_posix(self, tmp_path):
        """Tier 2 (POSIX): Detects executable .git/hooks/pre-commit."""
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'hook'\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

        result = read_fix_command(str(tmp_path))
        assert result == str(hook)

    @pytest.mark.skipif(platform.system() == "Windows", reason="POSIX executable check")
    def test_skips_non_executable_hook_posix(self, tmp_path):
        """Tier 2 (POSIX): Non-executable hook is skipped, falls to tier 3."""
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'hook'\n")
        # Explicitly remove execute bit
        hook.chmod(stat.S_IRUSR | stat.S_IWUSR)

        assert read_fix_command(str(tmp_path)) == "git diff --check HEAD"

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only hook test")
    def test_falls_back_to_pre_commit_hook_windows(self, tmp_path):
        """Tier 2 (Windows): Invokes hook through sh/bash if available."""
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'hook'\n")

        with patch("shutil.which", return_value="/usr/bin/sh"):
            result = read_fix_command(str(tmp_path))
            assert result == f"/usr/bin/sh {hook}"

    def test_falls_back_to_git_diff_check(self, tmp_path):
        """Tier 3: No AI_CONTEXT.md, no hook → universal fallback."""
        assert read_fix_command(str(tmp_path)) == "git diff --check HEAD"

    def test_ai_context_without_marker_falls_through(self, tmp_path):
        """AI_CONTEXT.md exists but has no KOPIPASTA_FIX_CMD marker."""
        ctx = tmp_path / "AI_CONTEXT.md"
        ctx.write_text("# Project\nJust a normal context file.\n")

        # No hook either
        assert read_fix_command(str(tmp_path)) == "git diff --check HEAD"

    def test_ai_context_takes_priority_over_hook(self, tmp_path):
        """Tier 1 beats Tier 2: explicit config wins over hook presence."""
        ctx = tmp_path / "AI_CONTEXT.md"
        ctx.write_text("<!-- KOPIPASTA_FIX_CMD: cargo clippy -->")

        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'hook'\n")
        if platform.system() != "Windows":
            hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

        assert read_fix_command(str(tmp_path)) == "cargo clippy"


# ============================================================
# 2. generate_fix_prompt — Prompt Assembly
# ============================================================


class TestGenerateFixPrompt:
    """Tests the fix prompt template rendering."""

    def test_minimal_prompt_no_files_no_diff(self):
        """Error output alone produces a valid prompt."""
        result = generate_fix_prompt(
            command="ruff check .",
            error_output="src/main.py:10:5 E302 expected 2 blank lines",
            git_diff="",
            affected_files=[],
            env_vars={},
        )
        assert "$ ruff check ." in result
        assert "E302 expected 2 blank lines" in result
        assert "git diff" not in result  # Empty diff should be omitted by template
        assert "Affected Files" not in result  # No files

    def test_includes_git_diff_when_present(self):
        """Git diff section appears when diff content is provided."""
        diff = textwrap.dedent("""\
            diff --git a/src/main.py b/src/main.py
            --- a/src/main.py
            +++ b/src/main.py
            @@ -10,1 +10,2 @@
            +import os
        """)
        result = generate_fix_prompt(
            command="make check",
            error_output="error",
            git_diff=diff,
            affected_files=[],
            env_vars={},
        )
        assert "## Current Uncommitted Changes" in result
        assert "+import os" in result

    def test_includes_affected_files(self, tmp_path):
        """Affected files are rendered with correct headers and content."""
        test_file = tmp_path / "src" / "main.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def hello():\n    print('hi')\n")

        file_tuple = (str(test_file), False, None, None)

        result = generate_fix_prompt(
            command="pytest",
            error_output="FAILED test_main.py::test_hello",
            git_diff="",
            affected_files=[file_tuple],
            env_vars={},
        )
        assert "## Affected Files" in result
        assert "def hello():" in result
        assert "```python" in result

    @patch("builtins.input", return_value="m")
    def test_env_vars_masked_in_error_output(self, mock_input):
        """Sensitive values in error output get masked."""
        result = generate_fix_prompt(
            command="deploy",
            error_output="Connection failed: token=sk-abc123secret",
            git_diff="also sk-abc123secret here",
            affected_files=[],
            env_vars={"API_KEY": "sk-abc123secret"},
        )
        assert "deploy" in result
        # With input mocked to 'm' (mask), the secret should be replaced with asterisks
        assert "sk-abc123secret" not in result
        assert "***************" in result

    def test_snippet_mode_file(self, tmp_path):
        """Files in snippet mode only include the first N lines."""
        test_file = tmp_path / "big.py"
        lines = [f"line_{i} = {i}" for i in range(200)]
        test_file.write_text("\n".join(lines))

        file_tuple = (str(test_file), True, None, None)  # use_snippet=True

        result = generate_fix_prompt(
            command="test",
            error_output="err",
            git_diff="",
            affected_files=[file_tuple],
            env_vars={},
        )
        # Snippet mode: should have early lines but not line 199
        assert "line_0 = 0" in result
        assert "line_199 = 199" not in result

    def test_chunked_file(self, tmp_path):
        """Files with pre-selected chunks render only those chunks."""
        test_file = tmp_path / "module.py"
        test_file.write_text("full content here\n")

        chunks = ["def targeted_func():\n    pass", "class Foo:\n    bar = 1"]
        file_tuple = (str(test_file), False, chunks, None)

        result = generate_fix_prompt(
            command="mypy .",
            error_output="err",
            git_diff="",
            affected_files=[file_tuple],
            env_vars={},
        )
        assert "def targeted_func():" in result
        assert "class Foo:" in result
        assert "full content here" not in result

    def test_instructions_section_present(self):
        """The prompt always ends with actionable instructions."""
        result = generate_fix_prompt("cmd", "err", "", [], {})
        assert "Analyze the error output" in result
        assert "Unified Diffs" in result


# ============================================================
# 3. find_paths_in_text — Path Detection from Error Output
# ============================================================


class TestFindPathsInErrorOutput:
    """Tests path extraction specifically in the context of error/traceback output."""

    def test_python_traceback(self):
        """Detects file paths in a Python traceback."""
        traceback = textwrap.dedent("""\
            Traceback (most recent call last):
              File "src/main.py", line 42, in <module>
                result = process(data)
              File "src/utils.py", line 15, in process
                return transform(data)
            TypeError: expected str, got int
        """)
        valid = ["src/main.py", "src/utils.py", "src/other.py"]
        found = find_paths_in_text(traceback, valid)
        assert "src/main.py" in found
        assert "src/utils.py" in found
        assert "src/other.py" not in found

    def test_eslint_output(self):
        """Detects paths in ESLint-style output."""
        output = textwrap.dedent("""\
            src/components/App.tsx
              10:5  error  'foo' is not defined  no-undef
              22:1  warning  Unexpected console  no-console

            src/index.ts
              3:1  error  Missing semicolon  semi
        """)
        valid = ["src/components/App.tsx", "src/index.ts", "src/other.ts"]
        found = find_paths_in_text(output, valid)
        assert "src/components/App.tsx" in found
        assert "src/index.ts" in found
        assert "src/other.ts" not in found

    def test_ruff_output(self):
        """Detects paths in ruff check output."""
        output = textwrap.dedent("""\
            kopipasta/config.py:12:1: E302 Expected 2 blank lines, found 1
            kopipasta/prompt.py:45:80: E501 Line too long (95 > 88)
            Found 2 errors.
        """)
        valid = ["kopipasta/config.py", "kopipasta/prompt.py", "kopipasta/main.py"]
        found = find_paths_in_text(output, valid)
        assert "kopipasta/config.py" in found
        assert "kopipasta/prompt.py" in found
        assert "kopipasta/main.py" not in found

    def test_go_compiler_output(self):
        """Detects paths in Go compiler errors."""
        output = textwrap.dedent("""\
            # myproject/cmd/server
            cmd/server/main.go:15:2: undefined: handler
            cmd/server/routes.go:8:5: imported and not used: "fmt"
        """)
        valid = ["cmd/server/main.go", "cmd/server/routes.go", "pkg/handler.go"]
        found = find_paths_in_text(output, valid)
        assert "cmd/server/main.go" in found
        assert "cmd/server/routes.go" in found

    def test_no_false_positives_on_plain_text(self):
        """Plain English text should not match project paths."""
        output = "Everything looks good. No errors found."
        valid = ["src/main.py", "tests/test_main.py"]
        found = find_paths_in_text(output, valid)
        assert found == []

    def test_windows_paths_in_output(self):
        """Backslash paths in output match forward-slash project paths."""
        output = r'  File "src\utils\parser.py", line 5'
        valid = ["src/utils/parser.py", "src/main.py"]
        found = find_paths_in_text(output, valid)
        assert "src/utils/parser.py" in found

    def test_duplicate_paths_reported_once(self):
        """Same path appearing multiple times yields one entry."""
        output = textwrap.dedent("""\
            src/main.py:10:1: E302
            src/main.py:20:1: E303
            src/main.py:30:1: E302
        """)
        valid = ["src/main.py"]
        found = find_paths_in_text(output, valid)
        assert found == ["src/main.py"]

    def test_pre_commit_output(self):
        """Detects paths in pre-commit hook output."""
        output = textwrap.dedent("""\
            ruff.....................................................................Failed
            - hook id: ruff
            - exit code: 1

            kopipasta/tree_selector.py:3:1: F401 [*] `platform` imported but unused
            kopipasta/config.py:55:5: E722 Do not use bare `except`
        """)
        valid = [
            "kopipasta/tree_selector.py",
            "kopipasta/config.py",
            "kopipasta/main.py",
        ]
        found = find_paths_in_text(output, valid)
        assert "kopipasta/tree_selector.py" in found
        assert "kopipasta/config.py" in found
        assert "kopipasta/main.py" not in found