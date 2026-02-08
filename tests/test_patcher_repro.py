from kopipasta.patcher import parse_llm_output


def test_fail_indented_header():
    """
    Current behavior: Regex starts with ^ so it fails if there is whitespace.
    """
    llm_output = """
    Here is the code:
    ```python
      # FILE: indented.py
      print("found me")
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "indented.py"


def test_fail_multiple_files_single_block():
    """
    Current behavior: Takes the first regex match and assumes the whole block belongs to it.
    Result: Merges file1 and file2 contents into file1.
    """
    llm_output = """
    ```python
    # FILE: file1.py
    a = 1

    # FILE: file2.py
    b = 2
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 2
    assert patches[0]["file_path"] == "file1.py"
    assert "b = 2" not in patches[0]["content"]
    assert patches[1]["file_path"] == "file2.py"
    assert "a = 1" not in patches[1]["content"]


def test_fail_html_comment_style():
    """
    Current behavior: Regex only looks for #, //, /*. Misses HTML/XML style.
    """
    llm_output = """
    ```html
    <!-- FILE: index.html -->
    <div></div>
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "index.html"


def test_fail_sql_comment_style():
    """
    Current behavior: Misses SQL style (--).
    """
    llm_output = """
    ```sql
    -- FILE: query.sql
    SELECT * FROM table;
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "query.sql"


def test_fail_filenames_with_spaces():
    """
    Current behavior: Regex uses \\S+ which stops at space.
    """
    llm_output = """
    ```text
    # FILE: my cool file.txt
    content
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "my cool file.txt"


def test_fail_header_on_fence_line():
    """
    Current behavior: Regex expects code block content to start after a newline.
    Some LLMs put the comment on the same line as the backticks.
    """
    llm_output = """
    ```python # FILE: fence.py
    print("fence")
    ```
    """
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "fence.py"
