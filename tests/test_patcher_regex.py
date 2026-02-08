from kopipasta.patcher import parse_llm_output


def test_parse_llm_output_with_nested_backticks():
    """
    Tests that the parser does not truncate code blocks that contain
    triple backticks inside string literals (common in prompt generation code).
    """
    # Simulate LLM output where the code block contains "```" inside a python string
    llm_output = """
Here is the fix for the prompt generator:

```python
# FILE: kopipasta/prompt.py
def generate_prompt():
    # This line contains backticks inside the string
    prompt += f"### {path}\\n\\n```{lang}\\n{content}\\n```\\n\\n"
    return prompt
```

Hope this helps!
"""

    patches = parse_llm_output(llm_output)

    assert len(patches) == 1
    content = patches[0]["content"]

    # The parser should capture the full line, including the ending formatting
    assert 'prompt += f"### {path}\\n\\n```{lang}\\n{content}\\n```\\n\\n"' in content
    assert "return prompt" in content

    # It should NOT be truncated at the first backtick
    assert not content.strip().endswith('prompt += f"### {path}\\n\\n')


def test_parse_llm_output_outside_header():
    """
    Tests that the parser detects file headers placed immediately BEFORE
    the code block (e.g. # FILE: ... \n ```).
    """
    llm_output = """
    Here is the first file:
    
    # FILE: outside_indent.py
    ```python
    print("found me outside")
    ```

    And here is one with some blank lines before it:
    
    // FILE: spaced_out.js
    
    
    ```javascript
    console.log("also found");
    ```
    """
    patches = parse_llm_output(llm_output)

    assert len(patches) == 2

    assert patches[0]["file_path"] == "outside_indent.py"
    assert 'print("found me outside")' in patches[0]["content"]

    assert patches[1]["file_path"] == "spaced_out.js"
    assert 'console.log("also found")' in patches[1]["content"]


def test_nested_fences_explicit_length():
    """
    Tests that the parser handles nested fences when outer fence is longer (standard markdown).
    This ensures robust parsing without relying on heuristics.
    """
    llm_output = """
    ````python
    # FILE: nested_explicit.py
    code = \"\"\"
    ```
    inner
    ```
    \"\"\"
    ````
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    content = patches[0]["content"]

    # Should contain the inner fences
    assert "```" in content
    assert "inner" in content
    assert '"""' in content
