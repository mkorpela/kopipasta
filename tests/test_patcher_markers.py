import pytest

from kopipasta.patcher import (
    declared_file_paths,
    parse_llm_output,
    skipped_file_paths,
)


def test_reset_marker_clears_previous_patches():
    """
    Tests that <<<RESET>>> causes the parser to discard all patches
    found before the marker.
    """
    llm_output = """
    I will first suggest a bad change:
    ```python
    # FILE: bad.py
    print("this should be ignored")
    ```

    Wait, I changed my mind. Let's reset.
    <<<RESET>>>

    Here is the actual fix:
    ```python
    # FILE: good.py
    print("this is the real content")
    ```
    """
    patches = parse_llm_output(llm_output)

    # Should only contain good.py
    assert len(patches) == 1
    assert patches[0]["file_path"] == "good.py"
    assert "real content" in patches[0]["content"]

    # bad.py should be nowhere to be found
    assert not any(p["file_path"] == "bad.py" for p in patches)


def test_multiple_resets():
    """Ensures multiple resets work correctly, keeping only the final set."""
    llm_output = """
    # FILE: v1.py
    content
    <<<RESET>>>
    # FILE: v2.py
    content
    <<<RESET>>>
    ```python
    # FILE: v3.py
    content
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "v3.py"


def test_delete_marker_parsing():
    """Tests that the delete marker is correctly identified across formats."""
    llm_output = """
    ```python
    # FILE: to_delete_1.py
    <<<DELETE>>>
    ```

    ### to_delete_2.js
    ```javascript
    <<<DELETE>>>
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 2
    for patch in patches:
        assert patch["type"] == "delete"
        assert patch["content"] == ""


def test_reset_inside_code_block_is_ignored():
    """
    <<<RESET>>> should only work when it's outside a code block,
    otherwise it might be valid code content (e.g., in a test about resets).
    """
    llm_output = """
    ```python
    # FILE: parser_test.py
    def test_reset():
        marker = "<<<RESET>>>"
        return marker
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert "<<<RESET>>>" in patches[0]["content"]


# -- declared vs parsed: the silent drop ------------------------------------
#
# A response declared kopipasta/core/apply.py with four well-formed hunks and
# tests/test_apply.py with five. The apply.py block was not fenced, so it was
# discarded and the envelope said `"patches_proposed": 1`. The skipping is
# deliberate; the silence was not, and these pin the difference.


def test_declared_file_paths_sees_a_header_the_parser_drops():
    """A header with no body the parser accepts yields no patch at all.

    Measured, not assumed: a bare `# FILE:` at column 0 followed by a
    SEARCH/REPLACE pair *does* parse. What does not is a header the parser
    cannot attach a change to, and an indented one. Either way the model
    named a file and nothing came back for it, which is what must be seen.
    """
    text = "# FILE: a.py\nI would change the imports here.\n"
    assert declared_file_paths(text) == ["a.py"]
    assert parse_llm_output(text) == []


def test_declared_file_paths_sees_an_indented_header():
    """Indented blocks are dropped whole. This is the shape that cost a
    real turn: every hunk well-formed, the entire file silently missing."""
    text = (
        "    # FILE: a.py\n    <<<<<<< SEARCH\n    old\n"
        "    =======\n    new\n    >>>>>>> REPLACE\n"
    )
    assert declared_file_paths(text) == ["a.py"]
    assert parse_llm_output(text) == []


@pytest.mark.parametrize(
    "header",
    [
        "# FILE: a.py",
        "// FILE: a.py",
        "-- FILE: a.py",
        "/* FILE: a.py */",
        "<!-- FILE: a.py -->",
    ],
)
def test_declared_file_paths_accepts_every_comment_style(header):
    """Reusing the parser's own regex is what buys this for free. A second
    regex would drift toward reporting nothing was dropped."""
    assert declared_file_paths(header + "\n") == ["a.py"]


def test_declared_file_paths_dedupes_and_keeps_first_seen_order():
    text = "# FILE: b.py\n# FILE: a.py\n# FILE: b.py\n"
    assert declared_file_paths(text) == ["b.py", "a.py"]


def test_declared_file_paths_treats_dotted_and_bare_paths_as_one():
    assert declared_file_paths("# FILE: ./a.py\n# FILE: a.py\n") == ["a.py"]


def test_skipped_file_paths_names_the_file_that_was_dropped():
    """The real failure in miniature: two files declared, one comes back.

    The envelope used to report `"patches_proposed": 1` here and say nothing
    about a.py, so the caller applied half a change believing it whole.
    """
    text = "# FILE: a.py\nsome prose\n\n```python\n# FILE: b.py\nprint(1)\n```\n"
    patches = parse_llm_output(text)
    assert [p["file_path"] for p in patches] == ["b.py"]
    assert skipped_file_paths(text, patches) == ["a.py"]


def test_skipped_file_paths_is_empty_when_everything_parsed():
    text = "```python\n# FILE: b.py\nprint('hi')\n```\n"
    patches = parse_llm_output(text)
    assert skipped_file_paths(text, patches) == []
