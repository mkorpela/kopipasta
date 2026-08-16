"""No captured subprocess may decode with the platform's default encoding.

Field report 2.1 was a blocker on Windows and invisible everywhere else:
`subprocess.run(..., text=True)` decodes with `locale.getpreferredencoding()`,
which is cp1252 on a stock Windows box. Any tool that emits a box-drawing
rule — vitest, eslint, ruff, pytest, git with unicode paths — then killed the
reader thread with `UnicodeDecodeError`, and the caller received an empty
`output` alongside a non-zero exit code.

The per-call regression tests live next to their verbs. This one is a
*structural* guard: it is not about any one call site but about the class of
bug, because the next `subprocess.run(..., text=True)` anyone adds will have
exactly the same defect and no test of its own. It reads the source with `ast`
rather than importing, so a call site that is only reachable on another
platform is still checked.
"""

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "kopipasta"

#: Anything that hands us decoded text instead of bytes.
TEXT_FLAGS = ("text", "universal_newlines")

#: The one blessed spelling, `kopipasta.proc.TEXT`, splatted at the call site.
SHARED = "TEXT"

SPAWNERS = ("subprocess.run", "subprocess.Popen", "subprocess.check_output")


def _sources():
    return sorted(PACKAGE.rglob("*.py"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Name):
            return f"{owner.id}.{func.attr}"
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _splats_the_shared_constant(node: ast.Call) -> bool:
    return any(
        k.arg is None and isinstance(k.value, ast.Name) and k.value.id == SHARED
        for k in node.keywords
    )


def _text_calls():
    """(path, lineno, keywords, uses_shared) for every subprocess in text mode."""
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in SPAWNERS:
                continue
            keywords = {k.arg for k in node.keywords if k.arg}
            shared = _splats_the_shared_constant(node)
            if shared or keywords & set(TEXT_FLAGS):
                yield path, node.lineno, keywords, shared


def _offenders(required: str):
    return [
        f"{path.relative_to(PACKAGE.parent)}:{lineno}"
        for path, lineno, keywords, shared in _text_calls()
        if not shared and required not in keywords
    ]


def test_every_text_mode_subprocess_names_its_encoding():
    offenders = _offenders("encoding")
    assert not offenders, (
        "these subprocess calls decode with the platform default, which is "
        "cp1252 on Windows and will raise UnicodeDecodeError on ordinary tool "
        "output. Pass **TEXT from kopipasta.proc: " + ", ".join(offenders)
    )


def test_every_text_mode_subprocess_survives_undecodable_bytes():
    """utf-8 alone is not enough: a lone 0x9d is still a decode error.

    `errors='replace'` is the part that turns a crash into a smudge. There is
    no case where losing the whole stream beats losing one character.
    """
    offenders = _offenders("errors")
    assert not offenders, (
        "these subprocess calls have no `errors=` policy, so a single "
        "malformed byte discards the entire captured stream. Pass **TEXT from "
        "kopipasta.proc: " + ", ".join(offenders)
    )


def test_the_shared_constant_is_what_the_guard_assumes_it_is():
    """The guard waves `**TEXT` through, so `TEXT` itself has to be checked
    somewhere or the whole thing is a rubber stamp."""
    from kopipasta.proc import TEXT

    assert TEXT == {"text": True, "encoding": "utf-8", "errors": "replace"}


def test_the_guard_can_actually_see_the_call_sites():
    """A structural test that matches nothing passes forever. Pin that it
    matches something, so a refactor cannot silently disarm it."""
    found = list(_text_calls())
    assert len(found) >= 8
    assert all(shared for *_, shared in found), (
        "one spelling, in one place — an inline encoding= is right but it is "
        "also the thing that drifts"
    )
