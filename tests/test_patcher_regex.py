import pytest
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
