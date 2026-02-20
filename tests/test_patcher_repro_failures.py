import ast
import os
import pytest
from pathlib import Path
from kopipasta.patcher import apply_patches, parse_llm_output


@pytest.fixture
def repro_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repro"
    d.mkdir()
    return d


def test_repro_duplicate_context_ambiguity(repro_dir, capsys):
    """
    Reproduces Failure 1: Duplicate Context.
    The file has two identical blocks. The patch targets the SECOND one
    using line numbers.
    The context (generic closing tags) is identical for both.
    """
    file_path = repro_dir / "duplicate.tsx"
    # Create a file with two identical structures
    # Block 1 starts at line 1
    # Block 2 starts at line 10
    content = (
        "// Generic Start\n"
        "  </TableBody>\n"
        "</Table>\n"
        "\n" * 5 + "// Generic Middle\n"
        "  </TableBody>\n"
        "</Table>\n"
    )
    file_path.write_text(content)

    # Patch targets the SECOND block (around line 10)
    # The context is just closing tags, which exist at line 2 and line 10.
    llm_output = """
```diff
// FILE: duplicate.tsx
@@ -10,2 +10,3 @@
   </TableBody>
+  <NewElement />
 </Table>
```
"""
    patches = parse_llm_output(llm_output)

    cwd = os.getcwd()
    os.chdir(repro_dir)
    try:
        apply_patches(patches)
    finally:
        os.chdir(cwd)

    result = file_path.read_text()

    # We expect the second block to be patched
    # Block 1 should remain untouched
    expected_block_1 = "// Generic Start\n  </TableBody>\n</Table>"
    assert expected_block_1 in result

    # We strip whitespace for the assertion just to be safe about indentation matching
    assert "<NewElement />" in result

    # Check strict location
    lines = result.splitlines()
    # Line 0: // Generic Start
    # Line 1:   </TableBody>
    # Line 2: </Table> (unchanged)
    assert lines[2].strip() == "</Table>"

    # Line 8: // Generic Middle
    # Line 9:   </TableBody>
    # Line 10:  <NewElement /> (Inserted!)
    # Line 11:  </Table>

    assert "NewElement" in lines[10]


def test_repro_whitespace_mismatch(repro_dir, capsys):
    """
    Reproduces Failure 2: Low Match Ratio / Whitespace Mismatch.
    File has 4 spaces, Patch has 2 spaces.
    """
    file_path = repro_dir / "indent.py"
    file_path.write_text(
        "def main():\n    # deeply indented\n    return True\n", encoding="utf-8"
    )

    # Patch uses 2 spaces for context, but file has 4
    llm_output = """
```diff
# FILE: indent.py
@@ -1,3 +1,3 @@
 def main():
-  # deeply indented
+  # patched
   return True
```
"""
    cwd = os.getcwd()
    os.chdir(repro_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)
    finally:
        os.chdir(cwd)

    captured = capsys.readouterr()

    # Ensure it wasn't skipped
    assert "Skipping hunk" not in captured.out

    # Ensure content was updated
    content = file_path.read_text()
    assert "# patched" in content


def test_fuzzy_match_bad_indentation_causes_syntax_error(repro_dir, capsys):
    """
    Reproduces Bug 1: Fuzzy match with wrong indentation producing invalid Python.

    The file uses 4-space indentation. The patch's original_lines use 2-space
    indentation, so exact and loose matches both fail. The fuzzy matcher accepts
    the hunk (ratio >= 0.6) and writes new_lines that also use 2-space
    indentation — producing a syntax error in the resulting Python file.

    Expected behaviour: the patcher should either refuse to apply the hunk or
    preserve valid Python. Currently it silently applies the 2-space lines into
    a 4-space context, creating an IndentationError.
    """
    file_path = repro_dir / "service.py"
    file_path.write_text(
        "class MyService:\n"
        "    def process(self):\n"
        "        result = self._fetch()\n"
        "        if result:\n"
        "            return result\n"
        "        return None\n",
        encoding="utf-8",
    )

    # The patch uses 2-space indentation in both original and new lines.
    # The file uses 4-space indentation, so neither exact nor loose match fires.
    # Fuzzy match fires (method/variable names are similar enough), and writes
    # new_lines (with 2-space indentation) directly into the 4-space block.
    llm_output = """
```diff
# FILE: service.py
@@ -3,4 +3,5 @@
   result = self._fetch()
   if result:
+    self._log(result)
     return result
   return None
```
"""
    patches = parse_llm_output(llm_output)
    cwd = os.getcwd()
    os.chdir(repro_dir)
    try:
        apply_patches(patches)
    finally:
        os.chdir(cwd)

    result_content = file_path.read_text(encoding="utf-8")

    # The resulting file must still be valid Python.
    # If the bug is present, ast.parse will raise IndentationError / SyntaxError.
    try:
        ast.parse(result_content)
    except SyntaxError as e:
        pytest.fail(
            f"Fuzzy match produced syntactically invalid Python: {e}\n"
            f"File content:\n{result_content}"
        )


def test_fuzzy_match_replaces_unmodified_context_lines(repro_dir, capsys):
    """
    Reproduces Bug 2: Fuzzy match silently deletes lines that were context-only
    (not marked with '-' in the diff).

    The fuzzy fallback computes:
        end_idx = match.a + len(hunk_original)
    But match.size may cover only a subset of hunk_original.  The replacement
    therefore removes MORE lines from the original than were matched, wiping out
    context lines that should have been preserved unchanged.
    """
    file_path = repro_dir / "utils.py"
    file_path.write_text(
        "def helper():\n"
        "    step_one()\n"
        "    step_two()\n"
        "    step_three()\n"
        "    step_four()\n"
        "    return 'done'\n",
        encoding="utf-8",
    )

    # The hunk targets lines 2-4 (step_one + step_two + a line that does NOT
    # exist in the file).  Exact/loose match fails because DOES_NOT_EXIST() is
    # absent.  Fuzzy match fires with a partial match covering step_one+step_two
    # (2 of 3 original_lines, ratio ≈ 0.67 ≥ 0.6).  The replacement range is
    # then [match.a, match.a + 3], which reaches into step_three() — a line
    # that was never marked '-' — and deletes it.
    llm_output = """
```diff
# FILE: utils.py
@@ -2,3 +2,3 @@
     step_one()
     step_two()
-    DOES_NOT_EXIST()
+    step_new()
```
"""
    patches = parse_llm_output(llm_output)
    cwd = os.getcwd()
    os.chdir(repro_dir)
    try:
        apply_patches(patches)
    finally:
        os.chdir(cwd)

    result_content = file_path.read_text(encoding="utf-8")
    result_lines = result_content.splitlines()

    # step_three() and step_four() were pure context — they must survive.
    assert "    step_three()" in result_lines, (
        "Bug 2: fuzzy match deleted context-only line 'step_three()'.\n"
        f"Result:\n{result_content}"
    )
    assert "    step_four()" in result_lines, (
        "Bug 2: fuzzy match deleted context-only line 'step_four()'.\n"
        f"Result:\n{result_content}"
    )
    # The intended substitution must still be present.
    assert "    step_new()" in result_lines, (
        f"The intended replacement 'step_new()' was not applied.\n"
        f"Result:\n{result_content}"
    )
