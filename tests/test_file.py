from pathlib import Path

import pytest

from kopipasta.file import is_ignored, read_file_contents


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


# -- a file that is not valid UTF-8 is degraded, not discarded ---------------
#
# It used to return a sixty-character error string in place of the whole file.
# Measured on this repo: docs/FIELD_REPORT_AMBIENT.md picked up a cp1252 tail,
# and a --all selection carried the placeholder instead of the document.
# Repairing the encoding moved the payload from 330,768 to 337,995 tokens, so
# the model had been handed an error message where 7,227 tokens of prose
# belonged -- while the envelope still counted the file among those sent.


def test_a_file_that_is_not_utf8_still_delivers_its_content(tmp_path: Path):
    """0x97 is a cp1252 em-dash, the exact byte that caused this."""
    target = tmp_path / "prose.md"
    target.write_bytes(b"# Title\n\nA sentence \x97 with an em-dash, and more prose.\n")

    out = read_file_contents(str(target))

    assert not out.startswith("<.."), "the whole file must not become a placeholder"
    assert "and more prose" in out, "content after the bad byte must survive"
    assert "# Title" in out, "content before the bad byte must survive"
    assert "\ufffd" in out, "the undecodable byte becomes a replacement character"


def test_the_degraded_read_names_the_file_it_could_not_decode(tmp_path, capsys):
    """ "A file could not be decoded" is unusable in a 91-file selection."""
    target = tmp_path / "prose.md"
    target.write_bytes(b"x \x97 y\n")

    read_file_contents(str(target))

    assert "prose.md" in capsys.readouterr().out


def test_a_file_that_cannot_be_opened_at_all_is_still_a_placeholder(tmp_path: Path):
    """OSError is the unrecoverable case: there are no bytes to salvage, so the
    placeholder stays. Only the decode failure is recoverable."""
    out = read_file_contents(str(tmp_path / "does_not_exist.md"))

    assert out.startswith("<..")
    assert "does_not_exist.md" in out


def test_a_valid_utf8_file_is_returned_verbatim(tmp_path: Path):
    """The common path must be untouched, replacement characters included: a
    file legitimately containing U+FFFD must not be reported as degraded."""
    target = tmp_path / "ok.md"
    target.write_text("clean \u2014 text\n", encoding="utf-8")

    assert read_file_contents(str(target)) == "clean \u2014 text\n"
