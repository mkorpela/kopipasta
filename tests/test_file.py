import codecs
from pathlib import Path

import pytest

from kopipasta.file import (
    decode_note,
    decode_text,
    is_binary,
    is_ignored,
    lossy_reads,
    read_file_contents,
)


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


# -- the encoding is sniffed, not assumed -----------------------------------
#
# PowerShell 5.1's `>` redirection writes UTF-16LE. `git diff > changes.txt`
# therefore produces a perfectly readable file that is not UTF-8, and reading
# it as UTF-8 with errors="replace" turned it into a wall of U+FFFD. A review
# was run over that wall and came back confident: the model was never told it
# was reading nothing, because the warning went to stderr and the payload said
# nothing at all. Every one of these is that bug.


@pytest.mark.parametrize(
    "encoding, bom",
    [
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-32-le", codecs.BOM_UTF32_LE),
        ("utf-32-be", codecs.BOM_UTF32_BE),
        ("utf-8", codecs.BOM_UTF8),
    ],
)
def test_a_bom_marked_file_is_decoded_by_its_bom(tmp_path: Path, encoding, bom):
    original = "diff --git a/x.py b/x.py\n+added line\n"
    target = tmp_path / "changes.txt"
    target.write_bytes(bom + original.encode(encoding))

    assert read_file_contents(str(target)) == original


def test_the_utf32_bom_is_not_read_as_utf16(tmp_path: Path):
    """BOM_UTF32_LE starts with BOM_UTF16_LE. Testing the short mark first
    decodes a UTF-32 file as UTF-16 and yields plausible-looking garbage
    rather than an error, which is the worst available outcome."""
    original = "hello\n"
    target = tmp_path / "wide.txt"
    target.write_bytes(codecs.BOM_UTF32_LE + original.encode("utf-32-le"))

    assert read_file_contents(str(target)) == original


def test_a_bomless_utf16_file_is_sniffed_from_its_null_bytes(tmp_path: Path):
    original = "line one\nline two\nline three\n"
    target = tmp_path / "nobom.txt"
    target.write_bytes(original.encode("utf-16-le"))

    assert read_file_contents(str(target)) == original


def test_sniffing_does_not_fire_on_ordinary_utf8(tmp_path: Path):
    """The heuristic must never touch the common path."""
    original = "def f():\n    return 1\n"
    target = tmp_path / "code.py"
    target.write_text(original, encoding="utf-8")

    text, encoding, replacements = decode_text(target.read_bytes())

    assert (text, encoding, replacements) == (original, "utf-8", 0)


def test_crlf_is_normalised_the_way_text_mode_normalised_it(tmp_path: Path):
    """These reads were text-mode until the encoding fix made them binary.
    Losing universal newlines put CRLF in the payload and in the memory
    prologue, so one file rendered to different bytes on different platforms."""
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\rthree\n")

    assert read_file_contents(str(target)) == "one\ntwo\nthree\n"


def test_a_utf16_file_is_not_mistaken_for_a_binary(tmp_path: Path):
    """UTF-16 text is more than half NUL bytes, so the null-byte test called it
    binary and the file vanished from the selection without a word."""
    target = tmp_path / "notes.rst"
    target.write_bytes(codecs.BOM_UTF16_LE + "hello\n".encode("utf-16-le"))

    assert is_binary(str(target)) is False


def test_a_real_binary_is_still_binary(tmp_path: Path):
    target = tmp_path / "blob.rst"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00")

    assert is_binary(str(target)) is True


def test_a_lossy_decode_is_reported_against_the_file(tmp_path: Path):
    """The caveat has to be reachable by the renderer, because a warning on
    stderr never reaches the model doing the reasoning."""
    target = tmp_path / "prose.md"
    target.write_bytes(b"x \x97 y\n")

    read_file_contents(str(target))

    note = decode_note(str(target))
    assert note and "unreadable" in note
    assert str(target.resolve()) in {str(Path(p)) for p in lossy_reads()}


def test_a_clean_reread_clears_an_earlier_lossy_note(tmp_path: Path):
    """The note is per-file state. A file repaired between two runs in one
    process must not keep carrying a caveat it no longer earns."""
    target = tmp_path / "prose.md"
    target.write_bytes(b"x \x97 y\n")
    read_file_contents(str(target))
    assert decode_note(str(target))

    target.write_text("x - y\n", encoding="utf-8")
    read_file_contents(str(target))

    assert decode_note(str(target)) is None
