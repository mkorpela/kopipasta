import os
from kopipasta.patcher import find_paths_in_text
from kopipasta.tree_selector import TreeSelector


def test_find_paths_in_text_basic():
    valid_paths = ["src/main.py", "README.md", "docs/api.md"]
    text = "You should check src/main.py and README.md for details."

    found = find_paths_in_text(text, valid_paths)
    assert "src/main.py" in found
    assert "README.md" in found
    assert "docs/api.md" not in found


def test_find_paths_in_text_delimiters():
    valid_paths = ["app.py", "config.json"]
    text = 'Look at "app.py", (config.json), and [app.py].'

    found = find_paths_in_text(text, valid_paths)
    assert "app.py" in found
    assert "config.json" in found
    # Ensure no duplicates if implementation doesn't handle them (set handles it usually)
    assert len(found) == 2


def test_find_paths_in_text_cross_platform_slashes():
    # Simulate Windows local paths
    valid_paths = ["src\\utils\\helper.py", "tests\\test_main.py"]
    # LLM usually output forward slashes
    text = "Check src/utils/helper.py and tests/test_main.py"

    found = find_paths_in_text(text, valid_paths)
    assert "src\\utils\\helper.py" in found
    assert "tests\\test_main.py" in found


def test_find_paths_in_text_shadowing():
    # Longest path should match, and we should be careful about sub-paths
    valid_paths = ["src/main.py", "main.py"]
    text = "The file is located at src/main.py"

    found = find_paths_in_text(text, valid_paths)
    # Both are technically in the text, but usually we want the specific one.
    # Because of our regex boundaries, 'main.py' won't match 'src/main.py'
    # because of the '/' prefix.
    assert "src/main.py" in found
    assert "main.py" not in found


def test_get_all_unignored_files(tmp_path):
    # Setup mock project
    (tmp_path / "visible.py").write_text("content")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hidden.py").write_text("content")
    (tmp_path / "ignored.tmp").write_text("content")
    (tmp_path / "binary.exe").write_bytes(b"\x00\x01\x02")

    # Create selector with ignore pattern
    selector = TreeSelector(
        ignore_patterns=["*.tmp", ".git/"], project_root_abs=str(tmp_path)
    )

    # Change CWD for relpath calculation
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        paths = selector._get_all_unignored_files()
    finally:
        os.chdir(old_cwd)

    assert "visible.py" in paths
    assert "ignored.tmp" not in paths
    assert ".git/hidden.py" not in paths
    assert "binary.exe" not in paths  # is_binary check
