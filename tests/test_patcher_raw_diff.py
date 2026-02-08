from kopipasta.patcher import parse_llm_output


def test_raw_git_diff_parsing():
    """
    Tests parsing a block that contains a raw 'git diff' output
    without explicit '# FILE:' comments.
    """
    # Construct fence to avoid breaking markdown parsers when pasting this test
    fence = "`" * 3
    llm_output = f"""
Here is the git diff output:

{fence}diff
diff --git a/src/main.py b/src/main.py
index 1234567..abcdefg 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,2 +10,3 @@
 def main():
-    pass
+    print("Hello Raw Diff")

diff --git a/README.md b/README.md
index 1111111..2222222 100644
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # Title
+Added description.
{fence}
"""
    patches = parse_llm_output(llm_output)

    assert len(patches) == 2

    # Check first file
    p1 = next(p for p in patches if p["file_path"] == "src/main.py")
    assert p1["type"] == "diff"
    # The parser chunks it into Hunks.
    assert len(p1["content"]) == 1
    assert p1["content"][0]["new_lines"] == [
        "def main():",
        '    print("Hello Raw Diff")',
    ]

    # Check second file
    p2 = next(p for p in patches if p["file_path"] == "README.md")
    assert p2["type"] == "diff"
    assert p2["content"][0]["new_lines"] == ["# Title", "Added description."]


def test_raw_diff_no_git_header():
    """
    Tests parsing a unified diff that lacks the 'diff --git' line
    but has the '---' / '+++' headers.
    """
    fence = "`" * 3
    llm_output = f"""
{fence}diff
--- a/config.json
+++ b/config.json
@@ -2,2 +2,2 @@
 {{
-  "debug": false
+  "debug": true
 }}
{fence}
"""
    patches = parse_llm_output(llm_output)
    assert len(patches) == 1
    assert patches[0]["file_path"] == "config.json"
    assert patches[0]["content"][0]["new_lines"] == ["{", '  "debug": true', "}"]


def test_mixed_explicit_and_raw_diff():
    """
    Ensures that if explicit FILE headers are present, we don't accidentally
    trigger raw diff parsing logic (or that it handles it gracefully).
    """
    fence = "`" * 3
    llm_output = f"""
{fence}python
# FILE: explicit.py
print("explicit")
{fence}

{fence}diff
diff --git a/implicit.py b/implicit.py
--- a/implicit.py
+++ b/implicit.py
@@ -1 +1 @@
-old
+new
{fence}
"""
    patches = parse_llm_output(llm_output)
    assert len(patches) == 2
    assert patches[0]["file_path"] == "explicit.py"
    assert patches[1]["file_path"] == "implicit.py"
