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
        "def main():\n" "    # deeply indented\n" "    return True\n", encoding="utf-8"
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
