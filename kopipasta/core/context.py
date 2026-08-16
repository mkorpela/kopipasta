"""Resolved selection -> the rendered payload, as `(prefix, suffix)` — spec §6.

The split is not cosmetic. The repo content is a stable prefix that is reused
verbatim across the turns of a session; the question is the varying tail. That
boundary *is* the cache breakpoint — Anthropic places its `cache_control`
there, Gemini's `cachedContents` resource holds exactly that text — so a
renderer that interpolated the task into the middle of the payload would
destroy reuse on every turn. Hence two strings, never one.

The prefix is zoned (spec §11.4). "Editable" versus "read-only" has to be
visible to the model for the same reason it is enforceable by the patcher: a
patch against a reference-only file is a rejected patch, and the model cannot
respect a distinction it was never shown.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from kopipasta.core.budget import CHARS_PER_TOKEN, estimate_tokens
from kopipasta.core.modes import Mode
from kopipasta.core.resolver import EDIT, MAP, REF, SNIPPET, Entry, Selection
from kopipasta.file import extract_symbols, read_file_contents
from kopipasta.prompt import (
    get_file_snippet,
    get_language_for_file,
    get_project_structure,
    handle_env_variables,
)

LEGEND = (
    "The tree below lists every non-ignored file in the project, including "
    "files whose contents were not sent. A file mapped to a list of "
    "signatures was included as a skeleton: those signatures are all you have "
    "of it. A file mapped to [] was either sent in full under one of the zone "
    "headings below, or not sent at all — if you cannot find it in a zone, "
    "you have not seen it. Never infer the contents of a file you were not "
    "given: name it in missing_context instead."
)


@dataclass
class Payload:
    prefix: str
    suffix: str
    #: Chars per token for the provider that will read this. Carried on the
    #: payload rather than looked up here, because "how big is this" has a
    #: different answer for Anthropic and Gemini — by nearly 50%.
    cpt: float = CHARS_PER_TOKEN

    @property
    def text(self) -> str:
        return f"{self.prefix}\n{self.suffix}"

    @property
    def chars(self) -> int:
        return len(self.prefix) + 1 + len(self.suffix)

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.chars, self.cpt)


@dataclass
class Update:
    """A file that is new or changed since the session's prefix was built."""

    rel: str
    role: str
    content: str
    note: str


@dataclass
class Turn:
    """One earlier exchange, replayed so the conversation has a memory."""

    n: int
    question: str
    answer: str


def _masker(env_vars: Optional[Dict[str, str]]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Always mask, never ask.

    In the TUI a human decides per variable. Here the payload is on its way to
    a third-party API and the caller may be a process that cannot answer, so
    the conservative answer is pre-selected rather than prompted for: masking
    leaks nothing, and a question nobody can answer is a hang (spec §12).
    """
    env_vars = env_vars or {}
    return env_vars, {key: "m" for key in env_vars}


def _block(rel: str, path: str, content: str, note: str = "") -> List[str]:
    return [
        f"# FILE: {rel}{note}",
        f"```{get_language_for_file(path)}",
        content,
        "```",
        "",
    ]


def role_content(path: str, role: str) -> str:
    """What a file looks like in this role. One definition, used everywhere.

    A `-s` file is 50 lines in the prefix and must be 50 lines when it is
    resent in a later turn's suffix too — two renderers for one role is how a
    snippet quietly becomes a whole file on turn 2. The same applies to a
    skeleton, which lives inside the structure tree in the prefix and has to
    be expressible on its own when it arrives later.
    """
    if role == SNIPPET:
        return get_file_snippet(path)
    if role == MAP:
        return "\n".join(extract_symbols(path)) or "(no symbols extracted)"
    return read_file_contents(path)


def role_note(role: str) -> str:
    """The caveat that has to travel with the content, in every zone."""
    if role == SNIPPET:
        return "first 50 lines only"
    if role == MAP:
        return "skeleton: signatures only"
    return ""


def _zone(title: str, note: str, entries: Sequence[Entry], mask) -> List[str]:
    if not entries:
        return []
    out = [f"## {title}", "", note, ""]
    for e in entries:
        caveat = role_note(e.role)
        out += _block(
            e.rel, e.path, mask(role_content(e.path, e.role)), f" ({caveat})" if caveat else ""
        )
    return out


def render_prefix(
    selection: Selection,
    *,
    ignore: Sequence[str],
    root: str,
    env_vars: Optional[Dict[str, str]] = None,
    project_context: Optional[str] = None,
) -> str:
    """The stable half: project rules, the structure tree, and the file zones."""
    env, decisions = _masker(env_vars)

    def mask(text: str) -> str:
        return handle_env_variables(text, env, decisions)

    out: List[str] = []
    if project_context:
        out += ["# Project Constitution (AI_CONTEXT.md)", "", project_context, ""]

    map_paths = [e.path for e in selection.by_role(MAP)]
    out += [
        "# Project Overview",
        "",
        "## Project Structure",
        "",
        LEGEND,
        "",
        "```json",
        get_project_structure(list(ignore), [root], map_paths),
        "```",
        "",
    ]
    out += _zone(
        "Active Workspace (Editable)",
        "These files are the working set. Changes belong here.",
        selection.by_role(EDIT),
        mask,
    )
    out += _zone(
        "Reference Context (Read-Only)",
        "Read these for dependencies and call sites. Do not propose changes to them.",
        selection.by_role(REF),
        mask,
    )
    out += _zone(
        "Snippets (partial files)",
        "Only the first lines of each file are shown. Ask for the rest if you need it.",
        selection.by_role(SNIPPET),
        mask,
    )
    return "\n".join(out)


def render_suffix(
    question: str,
    mode: Mode,
    *,
    history: Sequence[Turn] = (),
    updates: Sequence[Update] = (),
    env_vars: Optional[Dict[str, str]] = None,
) -> str:
    """The varying half: the conversation so far, what changed, and the task.

    Everything that varies between turns lives here so the prefix stays
    byte-identical and the provider-side cache keeps hitting.
    """
    env, decisions = _masker(env_vars)
    out: List[str] = []

    if history:
        out += [
            "## Conversation So Far",
            "",
            "Earlier turns of this session, against the same context as above.",
            "",
        ]
        for turn in history:
            out += [
                f"### Turn {turn.n} — question",
                "",
                turn.question.strip(),
                "",
                f"### Turn {turn.n} — your answer",
                "",
                turn.answer.strip(),
                "",
            ]

    if updates:
        out += [
            "## Updates Since The Context Above",
            "",
            "These files have changed or arrived since the context above was assembled.",
            "Where a file appears in both, THIS copy is the current one.",
            "",
        ]
        for u in updates:
            caveat = role_note(u.role)
            note = f"{u.note}; {caveat}" if caveat else u.note
            out += _block(
                u.rel, u.rel, handle_env_variables(u.content, env, decisions), f" ({note})"
            )

    out += ["## Task Instructions", "", question.strip(), "", mode.instructions, ""]
    return "\n".join(out)


def content_hash(path: str) -> str:
    """What a file's contents were when we sent them — spec §7 dedup.

    Truncated to 16 hex chars: this compares one file against its own earlier
    self within one session, so collision resistance beyond that is theatre.
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return ""
