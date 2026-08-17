"""A patch the model never fenced — spec §14.

The failure these cover was found live, not by a test: `ask --mode patch`
returned a correct search/replace patch with no ``` fence, and the parser,
which only looks inside fences, reported zero patches. The caller saw "the
backend is not a completion" and was sent to reconfigure a backend that had
just done its job perfectly.

Tolerating the shape is the fix. Doing it without inventing patches out of
prose is the constraint, because the missing fence is also the missing end
marker: `# FILE: x` followed by two paragraphs must never become "replace x
with two paragraphs".
"""

from patch_asserts import hunks_of

from kopipasta.patcher import parse_llm_output

SEARCH_REPLACE = """\
# FILE: src/calc.py
<<<<<<< SEARCH
def add(a, b):
    return a + b
=======
def add(a, b):
    return a + b + 1
>>>>>>> REPLACE
"""


def test_an_unfenced_search_replace_patch_is_still_a_patch():
    patches = parse_llm_output(SEARCH_REPLACE)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "src/calc.py"
    assert patches[0]["type"] == "diff"
    assert hunks_of(patches[0])[0]["new_lines"] == [
        "def add(a, b):",
        "    return a + b + 1",
    ]


def test_the_prose_around_an_unfenced_patch_is_not_part_of_it():
    """Models explain themselves before and after. Neither half is code."""
    patches = parse_llm_output(
        "Here is the fix; the off-by-one is in add().\n\n"
        + SEARCH_REPLACE
        + "\nLet me know if you would like tests for this.\n"
    )
    assert len(patches) == 1
    new = hunks_of(patches[0])[0]["new_lines"]
    assert "Let me know" not in "\n".join(new)
    assert "off-by-one" not in "\n".join(new)


def test_an_unfenced_unified_diff_is_still_a_patch():
    patches = parse_llm_output(
        "# FILE: src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b + 1\n"
    )
    assert len(patches) == 1
    assert patches[0]["type"] == "diff"


def test_prose_with_a_file_header_never_becomes_a_whole_file_rewrite():
    """The dangerous half of tolerance.

    Inside a fence the closing fence says where the new content ends. Outside
    one nothing does, so a header followed by text is not a rewrite — it is a
    model talking about a file. Guessing here destroys the file.
    """
    assert (
        parse_llm_output(
            "# FILE: src/calc.py\n"
            "This module adds two numbers. It could use a docstring,\n"
            "and the parameter names could be clearer.\n"
        )
        == []
    )


def test_a_fenced_patch_is_not_reparsed_by_the_fallback():
    """The fallback is the last resort, not a second opinion. If the fenced
    parse found the patch, parsing the document again could only duplicate
    it — and applying a hunk twice is not idempotent."""
    patches = parse_llm_output(f"```python\n{SEARCH_REPLACE}```\n")
    assert len(patches) == 1


def test_a_response_with_no_patch_in_it_yields_no_patch():
    assert parse_llm_output("I looked at src/calc.py and it is already correct.") == []
