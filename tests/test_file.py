import pytest
from pathlib import Path
from kopipasta.file import is_ignored


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Creates a mock project structure for testing ignore patterns."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Root .gitignore
    (project_dir / ".gitignore").write_text("*.log\nnode_modules/\n")
    (project_dir / "file.log").touch()
    (project_dir / "main.py").touch()
    (project_dir / "node_modules").mkdir()
    (project_dir / "node_modules" / "some_lib").touch()

    # Subdirectory with its own .gitignore
    sub_dir = project_dir / "src"
    sub_dir.mkdir()
    (sub_dir / ".gitignore").write_text("*.tmp\n__pycache__/\n")
    (sub_dir / "component.js").touch()
    (sub_dir / "component.tmp").touch()
    (sub_dir / "__pycache__").mkdir()
    (sub_dir / "__pycache__" / "cache_file").touch()

    # Nested subdirectory to test cascading
    nested_dir = sub_dir / "api"
    nested_dir.mkdir()
    (nested_dir / "endpoint.py").touch()
    (nested_dir / "endpoint.log").touch()  # Should be ignored by root .gitignore
    (nested_dir / "endpoint.tmp").touch()  # Should be ignored by subdir .gitignore

    return project_dir


def test_is_ignored_with_nested_gitignores(project_root: Path):
    """
    Tests that is_ignored correctly respects .gitignore files from the current
    directory up to the project root.
    """
    # Test cases: path, expected_result
    test_cases = [
        # Root level ignores
        ("file.log", True),
        ("main.py", False),
        ("node_modules/some_lib", True),
        ("node_modules", True),
        # Subdirectory level ignores
        ("src/component.js", False),
        ("src/component.tmp", True),
        ("src/__pycache__/cache_file", True),
        ("src/__pycache__", True),
        # Nested subdirectory, checking cascading ignores
        ("src/api/endpoint.py", False),
        ("src/api/endpoint.log", True),  # Ignored by root .gitignore
        ("src/api/endpoint.tmp", True),  # Ignored by src/.gitignore
    ]

    # The ignore patterns would be dynamically loaded by the new logic,
    # so we pass an empty list and let the function handle discovery.
    for rel_path, expected in test_cases:
        full_path = project_root / rel_path
        assert is_ignored(str(full_path), [], str(project_root)) == expected, (
            f"Failed on path: {rel_path}"
        )
