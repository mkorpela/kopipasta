import os
import pytest
from pathlib import Path
from kopipasta.patcher import parse_llm_output, apply_patches


@pytest.fixture
def search_replace_dir(tmp_path: Path) -> Path:
    d = tmp_path / "search_replace"
    d.mkdir()
    return d


def test_parse_search_replace_block_with_markdown_header(search_replace_dir):
    """
    Tests parsing of <<<< ==== >>>> blocks when the filename is provided
    via a Markdown header (### filename) instead of a comment inside the block.
    """
    llm_output = """
### kopipasta/test_file.py

```python
<<<<
def original():
    return True
====
def patched():
    return False
>>>>
```
"""
    patches = parse_llm_output(llm_output)

    assert len(patches) == 1
    p = patches[0]
    assert p["file_path"] == "kopipasta/test_file.py"
    assert p["type"] == "diff"

    hunks = p["content"]
    assert len(hunks) == 1
    assert hunks[0]["original_lines"] == ["def original():", "    return True"]
    assert hunks[0]["new_lines"] == ["def patched():", "    return False"]
    assert hunks[0]["start_line"] is None


def test_apply_search_replace_patch(search_replace_dir):
    """
    Tests applying the search/replace patch to a real file.
    """
    file_path = search_replace_dir / "app.py"
    file_path.write_text(
        "import os\n\n" "def main():\n" "    print('hello')\n" "    return 0\n"
    )

    llm_output = """
### app.py

```python
<<<<
def main():
    print('hello')
    return 0
====
def main():
    print('world')
    return 1
>>>>
```
"""

    original_cwd = os.getcwd()
    os.chdir(search_replace_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)

        content = file_path.read_text()
        assert "print('world')" in content
        assert "return 1" in content
        assert "print('hello')" not in content
    finally:
        os.chdir(original_cwd)


def test_search_replace_multiple_hunks(search_replace_dir):
    """Tests multiple search/replace blocks in one file."""
    file_path = search_replace_dir / "multi.py"
    file_path.write_text("A\nB\nC\nD\nE\n")

    llm_output = """
### multi.py
```
<<<<
B
====
Bb
>>>>

<<<<
D
====
Dd
>>>>
```
"""
    original_cwd = os.getcwd()
    os.chdir(search_replace_dir)
    try:
        apply_patches(parse_llm_output(llm_output))
        content = file_path.read_text()
        assert content == "A\nBb\nC\nDd\nE\n"
    finally:
        os.chdir(original_cwd)


def test_mixed_header_styles(search_replace_dir):
    """
    Ensures that if explicit header is present, it takes precedence over markdown header,
    and search/replace still works.
    """
    llm_output = """
### wrong_name.py

```python
# FILE: right_name.py
<<<<
old
====
new
>>>>
```
"""
    patches = parse_llm_output(llm_output)
    assert patches[0]["file_path"] == "right_name.py"
    assert patches[0]["content"][0]["original_lines"] == ["old"]
