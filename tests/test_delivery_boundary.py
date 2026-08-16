"""One prompt, two destinations — and the destinations do not mix.

The renderer is shared on purpose: the clipboard prompt and the `ask` payload
are the same bytes (`test_shared_rendering.py`). What must *not* be shared is
where they go. The TUI puts its prompt on the clipboard for a human to paste;
`ask` posts its payload to a provider and bills for it. Those are different
acts with different failure modes, and code that can do either is code that
will eventually do the wrong one.

The rule, in the direction of spec §13 — both surfaces are thin views over a
core that never prompts:

    kopipasta/core/**   may not import a surface module, reach the clipboard,
                        or pull in the terminal UI
    the TUI path        may not reach a backend

Enforced by import graph rather than by review, because the failure is silent:
nothing breaks when the agent CLI loads `prompt_toolkit`, it just quietly
becomes true that the headless path depends on the interactive one.
"""

import ast
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CORE = REPO / "kopipasta" / "core"

#: Modules that exist to serve a human at a terminal. Nothing under core/ may
#: import one: core is what both surfaces are built on, not a peer of either.
SURFACE_MODULES = {
    "kopipasta.main",
    "kopipasta.tree_selector",
    "kopipasta.clipboard",
    "kopipasta.prompt",
    "kopipasta.selection",
}

#: Third-party stacks that belong to one destination and not the other.
#: `pyperclip` *is* the clipboard; `prompt_toolkit` is the task editor and
#: `jinja2` the user's template, both of which only the TUI renders through.
#:
#: Deliberately absent: `rich`, `click` and `pygments`. Those arrive through
#: `patcher.py`, which both surfaces genuinely share — it is the asset every
#: surface routes through rather than around (spec §14) — and it colours a
#: diff and confirms a destructive write for whichever one is driving. Shared
#: *functionality* is the goal here; only the destinations are separated.
TUI_ONLY_PACKAGES = ("pyperclip", "prompt_toolkit", "jinja2")


def _imports_of(path: pathlib.Path):
    """Every module this file imports, at module scope or inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def _loaded_by(module: str) -> set:
    """Which modules a fresh interpreter loads to import `module`.

    A subprocess because import is a one-time side effect: anything the test
    session already imported would hide the very coupling being measured.

    `PYTHONPATH` is seeded from the session's own `sys.path` so the child
    resolves the same packages the parent did — `conftest.isolate_home`
    redirects HOME, which would otherwise cost the child any dependency
    installed into the user site directory.
    """
    code = (
        "import sys, importlib;"
        f"importlib.import_module({module!r});"
        "print('\\n'.join(sorted(sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
    )
    assert out.returncode == 0, out.stderr
    loaded = set(out.stdout.split())
    return loaded | {m.split(".")[0] for m in loaded}


# -- core is nobody's peer --------------------------------------------------


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_never_imports_a_surface_module(path):
    """The dependency runs surface -> core, never back.

    `core/context.py` imported `kopipasta.prompt` for four pure helpers that
    happened to live in a module which also owns the Jinja template and the
    prompt_toolkit task editor. One import, and the headless agent path
    depended on the terminal UI.
    """
    offenders = sorted(set(_imports_of(path)) & SURFACE_MODULES)
    assert not offenders, (
        f"{path.name} imports {offenders}; move what it needs into core/ "
        "instead of reaching up into a surface."
    )


# -- the agent path posts; it does not copy ---------------------------------


def test_the_agent_path_never_reaches_the_clipboard():
    """`ask` sends its payload to a provider. There is no human at a terminal
    to paste anything, and a headless run that mutated the operator's
    clipboard would be a side effect nobody asked for."""
    assert "pyperclip" not in _loaded_by("kopipasta.core.ask")


def test_the_agent_path_does_not_drag_in_the_terminal_ui():
    """Importing the agent CLI must not load the interactive stack.

    Not a performance complaint. It is the observable form of the coupling:
    while `ask` loads `prompt_toolkit`, the headless path depends on the
    interactive one, and nothing fails to tell you.
    """
    loaded = _loaded_by("kopipasta.core.ask")
    leaked = sorted(p for p in TUI_ONLY_PACKAGES if p in loaded)
    assert not leaked, f"kopipasta.core.ask pulled in {leaked}"


@pytest.mark.parametrize(
    "module",
    ["kopipasta.core.context", "kopipasta.core.resolver", "kopipasta.core.map"],
)
def test_the_shared_renderer_stands_alone(module):
    """The renderer both surfaces share belongs to neither of them."""
    loaded = _loaded_by(module)
    assert not sorted(p for p in TUI_ONLY_PACKAGES if p in loaded)


# -- the clipboard path copies; it does not post ----------------------------


def test_the_clipboard_path_never_reaches_a_backend():
    """Building the TUI's prompt must not be able to call a model.

    The TUI's whole contract is that it hands the human a string; anything
    that could post on its own would be spending money the human did not
    authorise in the act of pressing `q`.
    """
    loaded = _loaded_by("kopipasta.prompt")
    assert "kopipasta.core.backend" not in loaded
    assert "requests" not in loaded


def test_only_the_surfaces_touch_the_clipboard():
    """`copy_to_clipboard` is called by the TUI and by nothing else."""
    callers = {
        path.relative_to(REPO).as_posix()
        for path in (REPO / "kopipasta").rglob("*.py")
        if "copy_to_clipboard" in path.read_text(encoding="utf-8")
    }
    assert callers == {
        "kopipasta/clipboard.py",  # defines it
        "kopipasta/main.py",  # the final prompt
        "kopipasta/tree_selector.py",  # extend-context
    }


def test_only_the_agent_path_builds_a_backend():
    """`backend.build` is reached from core verbs and nowhere else."""
    callers = {
        path.relative_to(REPO).as_posix()
        for path in (REPO / "kopipasta").rglob("*.py")
        if "backend import" in path.read_text(encoding="utf-8")
        or "import backend" in path.read_text(encoding="utf-8")
    }
    assert all(c.startswith("kopipasta/core/") for c in callers), sorted(callers)
