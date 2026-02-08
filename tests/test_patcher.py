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

    main_py_patch = next(p for p in patches if p["file_path"] == "src/main.py")
    assert main_py_patch["type"] == "full"
    assert "# A new comment" in main_py_patch["content"]

    app_js_patch = next(p for p in patches if p["file_path"] == "web/app.js")
    assert app_js_patch["type"] == "full"
    assert 'console.log("Hello from JS");' in app_js_patch["content"]

    config_txt_patch = next(p for p in patches if p["file_path"] == "config.txt")
    assert config_txt_patch["type"] == "full"
    assert "completely_different_key=foo" in config_txt_patch["content"]


@pytest.fixture
def patch_test_dir(tmp_path: Path) -> Path:
    test_dir = tmp_path / "patch_project"
    test_dir.mkdir()
    src_dir = test_dir / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text('def main():\n    print("Hello, old world!")\n')
    (test_dir / "config.txt").write_text("original_key=bar\n")
    return test_dir


def test_apply_patches_overwrite(
    patch_test_dir: Path, mock_llm_output, capsys, monkeypatch
):
    """
    Tests that 'full' blocks overwrite the existing file content entirely.
    This is the safer, deterministic behavior replacing the old fuzzy matcher.
    """
    patches = parse_llm_output(mock_llm_output)

    # Change CWD into the mock project for the duration of the test
    original_cwd = os.getcwd()
    os.chdir(patch_test_dir)
    try:
        apply_patches(patches)

        # Check existing file was overwritten
        updated_main_py = patch_test_dir / "src/main.py"
        assert updated_main_py.exists()
        main_py_content = updated_main_py.read_text()

        # It should contain the new content
        assert 'print("Hello, new patched world!")' in main_py_content

        # It should NOT contain the old content (overwrite behavior)
        assert 'print("Hello, old world!")' not in main_py_content

        # Check new file was created
        new_app_js = patch_test_dir / "web/app.js"
        assert new_app_js.exists()
        assert "Hello from JS" in new_app_js.read_text()

        # Check for correct console output
        captured = capsys.readouterr()
        output = captured.out
        assert "Overwrote src/main.py" in output
        assert "Created web/app.js" in output

    finally:
        os.chdir(original_cwd)


def test_explicit_deletion(patch_test_dir: Path, capsys, monkeypatch):
    """
    Tests handling of the <<<DELETE>>> marker.
    """
    import click
    # Mock confirmation to return True
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)

    # Setup file to be deleted
    file_to_delete = patch_test_dir / "unwanted.py"
    file_to_delete.write_text("print('delete me')")
    
    assert file_to_delete.exists()

    llm_output = """
```python
# FILE: unwanted.py
<<<DELETE>>>
```
"""
    cwd = os.getcwd()
    os.chdir(patch_test_dir)
    try:
        patches = parse_llm_output(llm_output)
        apply_patches(patches)
    finally:
        os.chdir(cwd)

    assert not file_to_delete.exists()
    
    captured = capsys.readouterr()
    assert "Deleted unwanted.py" in captured.out


# --- Diff-based patching ---


@pytest.fixture
def mock_llm_diff_output():
    return r"""
Here are the changes for the Python file. The line numbers are totally wrong.
```diff
// FILE: src/app.py
@@ -99,7 +99,8 @@
 def main():
     \"""This is the main function.\"""
     user = get_user("test")
-    print(f"Hello, {user}!")
+    # Log the user for debugging
+    print(f"Greetings, {user}!")
     return 0
 
 if __name__ == "__main__":
```

And for the documentation.
```diff
# FILE: docs/usage.md
@@ -1,3 +1,4 @@
 # Usage
 
-Run the tool from your terminal.
+Run the tool from your project's root directory.
+It's easy and fun!
```

Finally, a TypeScript file for a new feature.
```diff
// FILE: web/api/service.ts
@@ -5,5 +5,8 @@
 export class ApiService {
     constructor(private endpoint: string) {}
 
-    async fetchData(): Promise<any> {
-        return fetch(this.endpoint);
-    }
+    async fetchData(id: string): Promise<any> {
+        const response = await fetch(`${this.endpoint}/${id}`);
+        return response.json();
+    }
 }
```
"""


@pytest.fixture
def diff_test_dir(tmp_path: Path) -> Path:
    test_dir = tmp_path / "diff_project"
    test_dir.mkdir()

    # Python file
    src_dir = test_dir / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text(
        """def get_user(name: str) -> str:
    return name.upper()

def main():
    \"\"\"This is the main function.\"\"\"
    user = get_user("test")
    print(f"Hello, {user}!")
    return 0

if __name__ == "__main__":
    main()
"""
    )

    # Markdown file
    docs_dir = test_dir / "docs"
    docs_dir.mkdir()
    (docs_dir / "usage.md").write_text(
        """# Usage

Run the tool from your terminal.
"""
    )

    # TypeScript file
    web_api_dir = test_dir / "web" / "api"
    web_api_dir.mkdir(parents=True, exist_ok=True)
    (web_api_dir / "service.ts").write_text(
        """interface IApiService {
    fetchData(id: string): Promise<any>;
}

export class ApiService {
    constructor(private endpoint: string) {}

    async fetchData(): Promise<any> {
        return fetch(this.endpoint);
    }
}
"""
    )
    return test_dir


def test_apply_patches_from_diff(diff_test_dir: Path, mock_llm_diff_output, capsys):
    # Change CWD
    original_cwd = os.getcwd()
    os.chdir(diff_test_dir)
    try:
        patches = parse_llm_output(mock_llm_diff_output)
        apply_patches(patches)

        # Assert Python file changed
        py_content = (diff_test_dir / "src/app.py").read_text()
        assert 'print(f"Greetings, {user}!")' in py_content
        assert "# Log the user for debugging" in py_content
        assert 'print(f"Hello, {user}!")' not in py_content

        # Assert Markdown file changed
        md_content = (diff_test_dir / "docs/usage.md").read_text()
        assert "project's root directory" in md_content
        assert "It's easy and fun!" in md_content
        assert "Run the tool from your terminal." not in md_content

        # Assert TypeScript file changed
        ts_content = (diff_test_dir / "web/api/service.ts").read_text()
        assert "async fetchData(id: string): Promise<any>" in ts_content
        assert "return response.json()" in ts_content
        assert "async fetchData(): Promise<any>" not in ts_content

        # Check console output
        captured = capsys.readouterr()
        output = captured.out
        assert "Patched src/app.py (1/1 hunks applied)" in output
        assert "Patched docs/usage.md (1/1 hunks applied)" in output
        assert "Patched web/api/service.ts (1/1 hunks applied)" in output

    finally:
        os.chdir(original_cwd)
