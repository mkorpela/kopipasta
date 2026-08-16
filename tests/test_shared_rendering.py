"""One renderer, two surfaces — the TUI's clipboard and `ask`'s payload.

There were two renderers here once. The TUI emitted a flat `## File Contents`
list; `ask` emitted zones. The same selection therefore produced two different
prompts, and only one of them told the model which files it was allowed to
change — while the TUI had tracked exactly that distinction all along, in the
Delta/Base states the Ralph loop already enforces as editable and read-only.

The tests below are the thing that stops that happening again. They do not
check that the two prompts *look* similar; they check that the shared body is
byte-identical, so a change to one surface that does not reach the other fails
here rather than in someone's chat window.
"""


import pytest

from kopipasta.core.context import render_context
from kopipasta.core.resolver import EDIT, MAP, REF, SNIPPET, SelectionSpec, resolve
from kopipasta.prompt import DEFAULT_TEMPLATE, generate_prompt_template
from kopipasta.selection import FileState, SelectionManager


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
    )
    (tmp_path / "src" / "main.py").write_text(
        'def main():\n    """Entry point."""\n    return 0\n'
    )
    (tmp_path / "src" / "util.py").write_text(
        "\n".join(f"# line {i}" for i in range(120)) + "\n"
    )
    (tmp_path / "src" / "tree.py").write_text('def walk(p):\n    """Walk."""\n    return p\n')
    (tmp_path / "README.md").write_text("# readme\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    from kopipasta import file as filemod

    filemod._is_ignored_cache.clear()
    filemod._is_binary_cache.clear()
    filemod._gitignore_cache.clear()
    # The template is read from disk; make sure a stale one from a previous
    # run cannot decide what this test measures.
    from kopipasta.prompt import get_template_path

    path = get_template_path()
    path.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    return tmp_path


def _tui_selection(project):
    """The same four files, selected the way a human selects them."""
    manager = SelectionManager()
    manager.set_state(str(project / "src" / "calc.py"), FileState.DELTA)
    manager.set_state(str(project / "src" / "main.py"), FileState.BASE)
    manager.set_state(str(project / "src" / "util.py"), FileState.DELTA, is_snippet=True)
    manager.set_state(str(project / "src" / "tree.py"), FileState.MAP)
    return manager.to_selection(str(project))


def _cli_selection(project):
    """The same four files, selected the way an agent selects them."""
    return resolve(
        SelectionSpec(
            edit=["src/calc.py"],
            ref=["src/main.py"],
            snippet=["src/util.py"],
            map=["src/tree.py"],
        ),
        [],
        str(project),
    )


def test_the_two_vocabularies_resolve_to_the_same_roles(project):
    """Delta is -e, Base is -r, a snippet is -s, a map is -m."""
    tui = {e.rel: e.role for e in _tui_selection(project).entries.values()}
    cli = {e.rel: e.role for e in _cli_selection(project).entries.values()}
    assert tui == cli
    assert tui == {
        "src/calc.py": EDIT,
        "src/main.py": REF,
        "src/util.py": SNIPPET,
        "src/tree.py": MAP,
    }


def test_the_clipboard_prompt_and_the_payload_share_one_body(project):
    """The whole point: the same selection renders to the same bytes.

    Not "the same headings" or "the same files" — the same string. Anything
    weaker lets the two surfaces drift a line at a time.
    """
    body = render_context(_cli_selection(project), ignore=[], root=str(project))
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert body in tui


def test_the_clipboard_prompt_carries_the_zones(project):
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert "## Active Workspace (Editable)" in tui
    assert "## Reference Context (Read-Only)" in tui
    assert "## Snippets (partial files)" in tui
    # Delta is editable, Base is not: the boundary the Ralph loop enforces is
    # now the one the model is shown.
    assert tui.index("Active Workspace") < tui.index("src/calc.py")
    assert tui.index("Reference Context") < tui.index("src/main.py")
    assert tui.index("src/calc.py") < tui.index("Reference Context")


def test_the_clipboard_prompt_carries_the_tree_and_its_legend(project):
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert "## Project Structure" in tui
    assert "Never infer the contents of a file you were not given" in tui
    # A mapped file is a skeleton in the tree, not a zone block.
    assert "def walk(p)" in tui
    assert "# FILE: src/tree.py" not in tui
    # Every non-ignored file is named, including the ones not sent.
    assert '"README.md"' in tui


def test_a_file_list_with_no_roles_still_renders(project):
    """The cached-selection path has no Delta/Base to preserve, so everything
    lands in the active workspace rather than in no zone at all."""
    tui, _ = generate_prompt_template(
        files_to_include=[(str(project / "src" / "calc.py"), False, None, "python")],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        root=str(project),
    )
    assert "## Active Workspace (Editable)" in tui
    assert "# FILE: src/calc.py" in tui


def test_the_cursor_still_lands_between_the_task_and_the_instructions(project):
    tui, cursor = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert tui[:cursor].endswith("## Task Instructions\n\n")
    assert tui[cursor:].startswith("\n\n## Instructions for Achieving the Task")


def test_the_instruction_tail_names_the_zones_it_relies_on(project):
    """The tail told the model to look for '## File Contents', a heading that
    no longer exists. An instruction pointing at a missing section is worse
    than no instruction: it reads as authoritative."""
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert "## File Contents" not in tui
    assert "## Active Workspace (Editable)" in tui.split("## Task Instructions")[1]


def test_secrets_are_masked_on_the_clipboard_path_too(project):
    """Masking lives in the shared renderer, so it cannot apply to one surface
    and not the other."""
    (project / "src" / "conf.py").write_text('TOKEN = "sk-live-abcdefghijklmnop"\n')
    manager = SelectionManager()
    manager.set_state(str(project / "src" / "conf.py"), FileState.DELTA)
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={"API_TOKEN": "sk-live-abcdefghijklmnop"},
        search_paths=[str(project)],
        selection=manager.to_selection(str(project)),
        root=str(project),
    )
    assert "sk-live-abcdefghijklmnop" not in tui
    assert "*" * 10 in tui


def test_selected_patches_survive_the_shared_renderer(project):
    """Chunk selection has no flag on the CLI side, so it is exactly the kind
    of TUI-only feature a shared renderer would quietly drop."""
    manager = SelectionManager()
    manager.set_state(
        str(project / "src" / "calc.py"),
        FileState.DELTA,
        chunks=["def add(a, b):", "    return a + b"],
    )
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=manager.to_selection(str(project)),
        root=str(project),
    )
    assert "# FILE: src/calc.py (selected patches)" in tui
    assert "def add(a, b):\n    return a + b" in tui


def test_web_content_is_still_its_own_section(project):
    """It has no counterpart in a repository selection, so it stays a template
    slot rather than being forced into a zone."""
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={
            "http://example.com": (("http://example.com", False, None, "html"), "<p>hi</p>")
        },
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert "## Web Content" in tui
    assert "http://example.com" in tui
    assert "<p>hi</p>" in tui


def test_a_template_written_before_zones_still_renders(project):
    """`{{ files }}` and `{{ structure }}` are still passed, so a customised
    template keeps working — it just misses the zones."""
    from kopipasta.prompt import get_template_path

    get_template_path().write_text(
        "## File Contents\n{% for f in files %}# FILE: {{ f.path }}{{ f.description }}\n"
        "```{{ f.language }}\n{{ f.content }}\n```\n{% endfor %}"
        "## Task Instructions\n{{ cursor_marker }}\n",
        encoding="utf-8",
    )
    tui, _ = generate_prompt_template(
        files_to_include=[],
        ignore_patterns=[],
        web_contents={},
        env_vars={},
        search_paths=[str(project)],
        selection=_tui_selection(project),
        root=str(project),
    )
    assert "# FILE: src/calc.py" in tui
    assert "# FILE: src/util.py (first 50 lines only)" in tui
    assert "## Active Workspace" not in tui  # no zones, as the template asked
