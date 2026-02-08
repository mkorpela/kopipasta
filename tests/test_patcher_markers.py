import pytest
from kopipasta.patcher import parse_llm_output

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