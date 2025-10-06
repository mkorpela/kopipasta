import os
import pytest
from pathlib import Path
from kopipasta.tree_selector import TreeSelector

@pytest.fixture
def mock_project(tmp_path: Path) -> Path:
    """Creates a mock project structure for testing TreeSelector."""
    proj = tmp_path / "selector_project"
    proj.mkdir()
    (proj / "main.py").write_text("print('hello')")
    (proj / "README.md").write_text("# Test Project")
    sub = proj / "src"
    sub.mkdir()
    (sub / "component.js").write_text("console.log('test');")
    # Change CWD into the mock project for the duration of the test
    original_cwd = os.getcwd()
    os.chdir(proj)
    yield proj
    os.chdir(original_cwd)

def test_preselects_files_from_command_line(mock_project: Path):
    """
    Tests that TreeSelector correctly pre-selects files passed to it.
    """
    main_py_abs = os.path.abspath("main.py")
    component_js_abs = os.path.abspath("src/component.js")
    
    files_to_preselect = [main_py_abs, component_js_abs]
    
    # Instantiate the selector and manually run the pre-selection logic
    selector = TreeSelector(ignore_patterns=[], project_root_abs=str(mock_project))
    
    # We pass all potential paths to build_tree
    selector.root = selector.build_tree(["."])
    selector._preselect_files(files_to_preselect)

    # Assertions
    assert len(selector.selected_files) == 2
    assert main_py_abs in selector.selected_files
    assert component_js_abs in selector.selected_files
    
    assert not selector.selected_files[main_py_abs][0]
    assert not selector.selected_files[component_js_abs][0]
    
    expected_char_count = os.path.getsize(main_py_abs) + os.path.getsize(component_js_abs)
    assert selector.char_count == expected_char_count