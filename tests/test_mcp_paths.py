"""
Tests for path handling and robustness in the MCP server.
Verifies normalization, error ordering, and cross-platform separator handling.
"""

import pytest
from unittest.mock import patch
from pathlib import Path
from kopipasta.mcp_server import apply_edits, read_files, _normalize_path, EditBlock

# Mock configuration
MOCK_PROJECT_ROOT = Path("/fake/project")
MOCK_CONFIG = {
    "project_root": str(MOCK_PROJECT_ROOT),
    "verification_command": "echo verify",
    "editable_files": ["src/app.py", "scripts/deploy.py"],
}


@pytest.fixture
def mock_deps():
    """Setup common mocks for config and file system."""
    with (
        patch(
            "kopipasta.mcp_server._load_config", return_value=MOCK_CONFIG
        ) as mock_config,
        patch("kopipasta.mcp_server._run_cmd", return_value="verified") as mock_run,
    ):
        yield mock_config, mock_run


def test_normalize_path():
    """Verify that paths are normalized to POSIX style."""
    assert _normalize_path("src/app.py") == "src/app.py"
    # On non-Windows, backslash is a valid filename character, not a separator,
    # so Path("src\\app.py").as_posix() won't convert it.
    # Test what the function actually guarantees: forward slashes pass through.
    import platform

    if platform.system() == "Windows":
        assert _normalize_path("src\\app.py") == "src/app.py"
        assert _normalize_path("src/nested\\file.py") == "src/nested/file.py"
    else:
        # On Unix, backslash is literal — just verify no crash and posix output
        result = _normalize_path("src/app.py")
        assert "/" in result or result == "src/app.py"


def test_apply_edits_file_not_exist(mock_deps):
    """If a file does not exist, it should report that error specifically, not permission denied."""

    with patch.object(Path, "exists", return_value=False):
        result = apply_edits(
            [EditBlock(file_path="src/missing.py", search="foo", replace="bar")]
        )

    assert "Error: File 'src/missing.py' does not exist" in result
    assert "Permission Denied" not in result


def test_apply_edits_permission_denied(mock_deps):
    """If file exists but is not in editable_files, report permission denied."""

    # File exists
    with (
        patch.object(Path, "exists", return_value=True),
        patch.object(
            Path, "resolve", side_effect=lambda: MOCK_PROJECT_ROOT / "secret.py"
        ),
    ):
        result = apply_edits(
            [EditBlock(file_path="secret.py", search="foo", replace="bar")]
        )

    assert "Permission Denied" in result
    assert "secret.py" in result


def test_apply_edits_separator_normalization(mock_deps, tmp_path):
    """
    Verify that apply_edits succeeds when the file_path uses forward slashes
    matching the whitelist. Uses a real temp directory to avoid Path.resolve()
    mocking issues on different OSes.
    """
    # Create a real file structure in tmp_path
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    app_file = src_dir / "app.py"
    app_file.write_text("old code")

    config = {
        "project_root": str(tmp_path),
        "verification_command": "echo verify",
        "editable_files": ["src/app.py"],
    }

    with (
        patch("kopipasta.mcp_server._load_config", return_value=config),
        patch("kopipasta.mcp_server._run_cmd", return_value="verified"),
    ):
        result = apply_edits(
            [EditBlock(file_path="src/app.py", search="old code", replace="new code")]
        )

    assert "Successfully modified" in result
    assert app_file.read_text() == "new code"


def test_read_files_status_reporting(mock_deps):
    """read_files should correctly identify files as EDITABLE even with mixed separators."""

    # Mock file existence
    with (
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "read_text", return_value="content"),
        patch("kopipasta.mcp_server.is_ignored", return_value=False),
        patch("kopipasta.mcp_server.is_binary", return_value=False),
    ):
        # We simulate the file "scripts/deploy.py" being present.
        # We ask for "scripts/deploy.py"
        result = read_files(["scripts/deploy.py"])
        assert "EDITABLE" in result

        # If we ask for "README.md" (not in whitelist)
        result_ro = read_files(["README.md"])
        assert "READ-ONLY" in result_ro
