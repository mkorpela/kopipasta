from kopipasta.patcher import parse_llm_output


def test_nested_indented_markdown_block():
    """
    Reproduces the bug where a nested code block inside a list item (indented)
    prematurely closes the parent code block.
    """
    llm_output = """
Here is the updated roadmap:

```markdown
# FILE: ROADMAP.md
# Roadmap

- Item 1:
  ```bash
  echo "nested block"
  ```

## Next Section
This content should still be part of ROADMAP.md, but is currently cut off.
```
"""
    patches = parse_llm_output(llm_output)

    assert len(patches) == 1
    content = patches[0]["content"]

    # The nested block should be preserved
    assert 'echo "nested block"' in content

    # CRITICAL: The content AFTER the nested block should be present
    assert "## Next Section" in content
    assert "currently cut off" in content
