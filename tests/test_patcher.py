import os
import pytest
from pathlib import Path
from kopipasta.patcher import apply_patches, parse_llm_output


@pytest.fixture
def mock_llm_output():
    return """
Some intro text.

Here is the first file, which should be updated:
```python
// FILE: src/main.py
def main():
    # A new comment
    print("Hello, new patched world!")
```

And here is another one with a different comment style. This is a new file.
```javascript
/* FILE: web/app.js */
console.log("Hello from JS");
```

A block without a file path, which should be ignored:
```
Just some text.
```

And a final file with a hash comment that should fail to apply.
```
# FILE: config.txt
completely_different_key=foo
```
    """


def test_parse_llm_output(mock_llm_output):
    patches = parse_llm_output(mock_llm_output)
    assert len(patches) == 3
    patch_map = {path: content for path, content in patches}

    assert "src/main.py" in patch_map
    assert '# A new comment' in patch_map["src/main.py"]

    assert "web/app.js" in patch_map
    assert 'console.log("Hello from JS");' in patch_map["web/app.js"]

    assert "config.txt" in patch_map
    assert "completely_different_key=foo" in patch_map["config.txt"]


@pytest.fixture
def patch_test_dir(tmp_path: Path) -> Path:
    test_dir = tmp_path / "patch_project"
    test_dir.mkdir()
    src_dir = test_dir / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text('def main():\n    print("Hello, old world!")\n')
    (test_dir / "config.txt").write_text("original_key=bar\n")
    return test_dir


def test_apply_patches_intelligent(patch_test_dir: Path, mock_llm_output, capsys):
    patches = parse_llm_output(mock_llm_output)

    # Change CWD into the mock project for the duration of the test
    original_cwd = os.getcwd()
    os.chdir(patch_test_dir)
    try:
        apply_patches(patches)

        # Check existing file was intelligently patched, not overwritten
        updated_main_py = patch_test_dir / "src/main.py"
        assert updated_main_py.exists()
        main_py_content = updated_main_py.read_text()
        assert 'print("Hello, new patched world!")' in main_py_content
        # This implicitly checks it wasn't a full overwrite
        assert main_py_content.startswith('def main():')
        assert not (patch_test_dir / "src/main.py.bak").exists()

        # Check new file was created
        new_app_js = patch_test_dir / "web/app.js"
        assert new_app_js.exists()
        assert "Hello from JS" in new_app_js.read_text()

        # Check that the un-patchable file was left unchanged
        config_txt = patch_test_dir / "config.txt"
        assert "original_key=bar" in config_txt.read_text()
        assert "completely_different_key=foo" not in config_txt.read_text()

        # Check for correct console output
        captured = capsys.readouterr()
        output = captured.out
        assert "Patched src/main.py" in output
        assert "Created web/app.js" in output
        assert "Failed to apply patch to config.txt" in output
        assert "Snippet did not match content confidently" in output

    finally:
        os.chdir(original_cwd)
