import os
from pathlib import Path
import pytest
from kopipasta.patcher import apply_patches, parse_llm_output


@pytest.fixture
def edge_case_dir(tmp_path: Path) -> Path:
    d = tmp_path / "edge_cases"
    d.mkdir()
    return d


def test_multiple_hunks_same_file(edge_case_dir, capsys):
    """
    Ensures multiple hunks in one file are applied correctly,
    specifically checking that reverse-order application prevents index shifting.
    """
    file_path = edge_case_dir / "multi.py"
    # A file with distinct sections
    file_path.write_text(
        "def top():\n    pass\n\n"
        "def middle():\n    return 'original'\n\n"
        "def bottom():\n    pass\n"
    )

    # Patch: Change top and bottom, leave middle
    llm_output = """
```python
# FILE: multi.py
@@ -1,2 +1,2 @@
 def top():
-    pass
+    print("top changed")

@@ -6,2 +6,2 @@
 def bottom():
-    pass
+    print("bottom changed")
```
"""
    original_cwd = os.getcwd()
    os.chdir(edge_case_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)

        content = file_path.read_text()
        assert 'print("top changed")' in content
        assert "return 'original'" in content
        assert 'print("bottom changed")' in content
    finally:
        os.chdir(original_cwd)


def test_patch_with_backticks_in_string(edge_case_dir):
    """Regression test: Ensure code containing markdown blocks isn't truncated."""
    file_path = edge_case_dir / "meta.py"
    file_path.write_text("prompt = ''\n")

    llm_output = '''
```python
# FILE: meta.py
def get_prompt():
    return """
```python
print("hello")
```
"""
```
'''
    original_cwd = os.getcwd()
    os.chdir(edge_case_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)

        content = file_path.read_text()
        # Should contain the nested backticks
        assert "```python" in content
        assert 'print("hello")' in content
    finally:
        os.chdir(original_cwd)


def test_diff_context_mismatch_diagnostics(edge_case_dir, capsys):
    """
    Test that we get useful output when a hunk fails due to missing context.
    """
    file_path = edge_case_dir / "mismatch.py"
    file_path.write_text("def foo():\n    print('a')\n")

    # Patch expects print('b'), so it should fail
    llm_output = """
```python
# FILE: mismatch.py
@@ -1,2 +1,2 @@
 def foo():
-    print('b')
+    print('c')
```
"""
    original_cwd = os.getcwd()
    os.chdir(edge_case_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)

        captured = capsys.readouterr()
        # Verify we see the specific reason
        assert "Skipping hunk #1" in captured.out
        # Verify the file was NOT changed
        assert file_path.read_text() == "def foo():\n    print('a')\n"
    finally:
        os.chdir(original_cwd)


def test_safety_check_shrinking_file(edge_case_dir, capsys, monkeypatch):
    """
    Tests that the safety check triggers when a large file is about to be
    overwritten by a very small content (potential snippet error).
    """
    import click

    # Setup a large file (> 200 chars)
    file_path = edge_case_dir / "large.py"
    file_path.write_text("a" * 1000)

    # Patch replacing it with "b" (very small)
    llm_output = """
```python
# FILE: large.py
b
```
"""
    # Mock click.confirm to return False (Don't overwrite)
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)

    cwd = os.getcwd()
    os.chdir(edge_case_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)
    finally:
        os.chdir(cwd)

    captured = capsys.readouterr()
    assert "Safety Check" in captured.out
    assert "Skipped large.py" in captured.out
    assert file_path.read_text() == "a" * 1000  # Unchanged


def test_safety_check_confirmed(edge_case_dir, capsys, monkeypatch):
    """Tests that the user can override the safety check."""
    import click

    file_path = edge_case_dir / "large_confirmed.py"
    file_path.write_text("a" * 1000)
    llm_output = "```python\n# FILE: large_confirmed.py\nb\n```"

    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)

    cwd = os.getcwd()
    os.chdir(edge_case_dir)
    try:
        apply_patches(parse_llm_output(llm_output))
    finally:
        os.chdir(cwd)

    captured = capsys.readouterr()
    assert "Overwrote" in captured.out
    assert "b" in file_path.read_text()
