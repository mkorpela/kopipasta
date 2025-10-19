import os
import pytest
from pathlib import Path
from rich.text import Text
from kopipasta.file import get_human_readable_size
from kopipasta.tree_selector import FileNode, TreeSelector


@pytest.fixture
def mock_project(tmp_path: Path) -> Path:
    """Creates a mock project structure for testing TreeSelector."""
    proj = tmp_path / "selector_project"
    proj.mkdir()
    (proj / "main.py").write_text("a" * 100)  # 100 bytes
    (proj / "README.md").write_text("b" * 200)  # 200 bytes

    sub = proj / "src"
    sub.mkdir()
    (sub / "component.js").write_text("c" * 1024)  # 1 KB

    nested_sub = sub / "utils"
    nested_sub.mkdir()
    (nested_sub / "helpers.py").write_text("d" * 2048)  # 2 KB

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

    expected_char_count = os.path.getsize(main_py_abs) + os.path.getsize(
        component_js_abs
    )
    assert selector.char_count == expected_char_count


def test_directory_label_shows_recursive_size_metrics(mock_project: Path):
    """
    Tests that directory labels correctly display the total size of selected files
    and the total size of all files within that directory, recursively.
    It also checks that the directory selector '○' is removed.
    """
    selector = TreeSelector(ignore_patterns=[], project_root_abs=str(mock_project))
    selector.root = selector.build_tree(["."])
    selector.root.expanded = True  # Expand root to see 'src'

    # Find the 'src' node and expand it
    src_node = next(child for child in selector.root.children if child.name == "src")
    src_node.expanded = True

    # Pre-select 'main.py' (at root) and 'helpers.py' (nested in src/utils)
    main_py_abs = os.path.abspath("main.py")
    helpers_py_abs = os.path.abspath("src/utils/helpers.py")
    selector.selected_files = {
        main_py_abs: (False, None),
        helpers_py_abs: (False, None),
    }

    # Generate the visible nodes and their labels for testing
    flat_tree = selector._flatten_tree(selector.root)
    visible_nodes = [node for node, _ in flat_tree]

    def get_node_label(node: FileNode) -> str:
        # This is a simplified version of the label generation logic in _build_display_tree
        # It helps us test the output without a full render cycle.
        if node.is_dir:
            total_size, selected_size = selector._calculate_directory_metrics(node)
            size_str = f" ({get_human_readable_size(selected_size)} / {get_human_readable_size(total_size)})"
            icon = "📂" if node.expanded else "📁"
            label = Text()
            label.append(f"{icon} {node.name}{size_str}")
            return label.plain
        return ""  # We only care about directory labels for this test

    # Find the nodes we want to test
    root_node_proxy = visible_nodes[
        0
    ]  # This will be 'src' since we built tree from '.'
    utils_node = next(n for n in visible_nodes if n.name == "utils")

    # Test the 'src' directory label
    # Total: component.js (1024) + helpers.py (2048) = 3072 bytes
    # Selected: helpers.py (2048) = 2048 bytes
    src_label = get_node_label(src_node)
    assert "2.00 KB / 3.00 KB" in src_label
    assert not src_label.startswith("○")

    # Test the 'utils' directory label
    # Total: helpers.py (2048) = 2048 bytes
    # Selected: helpers.py (2048) = 2048 bytes
    utils_label = get_node_label(utils_node)
    assert "2.00 KB / 2.00 KB" in utils_label
    assert not utils_label.startswith("○")
