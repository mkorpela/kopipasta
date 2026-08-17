"""Resolved selection -> the rendered payload, as `(prefix, suffix)` — spec §6.

**This is the only place a prompt's context is rendered.** Both surfaces come
through `render_context`: the TUI, which wraps it in the user's template and
puts it on the clipboard, and `ask`, which splits it at the task and posts it
to a model. There were two renderers here once, and they drifted — the TUI
sent one flat `## File Contents` list while `ask` sent zones, so the same
selection produced two different prompts and only one of them told the model
which files it was allowed to change. A shared renderer is what makes "paste
this into a chat window" and "send this to the oracle" the same question.

The split is not cosmetic. The repo content is a stable prefix that is reused
verbatim across the turns of a session; the question is the varying tail. That
boundary *is* the cache breakpoint — Anthropic places its `cache_control`
there, Gemini's `cachedContents` resource holds exactly that text — so a
renderer that interpolated the task into the middle of the payload would
destroy reuse on every turn. Hence two strings, never one. The TUI needs one
string and joins them at the same point, which is also where its cursor goes.

The prefix is zoned (spec §11.4). "Editable" versus "read-only" has to be
visible to the model for the same reason it is enforceable by the patcher: a
patch against a reference-only file is a rejected patch, and the model cannot
respect a distinction it was never shown. The TUI has tracked exactly this
distinction all along — Delta is the active focus, Base is synced context, and
the Ralph loop enforces them as editable and read-only — but flattened it away
at render time, so the model was never told.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from kopipasta.core.budget import CHARS_PER_TOKEN, estimate_tokens
from kopipasta.core.modes import Mode
from kopipasta.core.render import (
    get_file_snippet,
    get_language_for_file,
    get_project_structure,
    handle_env_variables,
)
from kopipasta.core.resolver import MAP, PIN, REF, SNIPPET, Entry, Selection
from kopipasta.file import decode_note, extract_symbols, read_file_contents

#: The quad-memory layers, in the order the clipboard prompt has always
#: emitted them: global kernel, project constitution, working memory
#: (AI_CONTEXT.md §1). `ask` used to send only the middle one, so a prompt
#: tuned against a profile and a live session file behaved differently on the
#: surface with nobody watching for the difference.
MEMORY_LAYERS: Tuple[Tuple[str, str], ...] = (
    ("user_profile", "# User Profile & Preferences"),
    ("project_context", "# Project Constitution (AI_CONTEXT.md)"),
    ("session_state", "# Current Working Session (AI_SESSION.md)"),
)


def render_memory(
    *,
    user_profile: Optional[str] = None,
    project_context: Optional[str] = None,
    session_state: Optional[str] = None,
) -> str:
    """The memory prologue, byte for byte as the clipboard has always had it.

    Reproduces what the template's three `{% if %}` blocks produced, down to
    the blank lines, because the clipboard prompt is the specification here —
    it is the one a human has read, tuned and come to trust. A layer that does
    not exist contributes nothing at all, not an empty heading announcing it.
    """
    values = {
        "user_profile": user_profile,
        "project_context": project_context,
        "session_state": session_state,
    }
    return "".join(
        f"{heading}\n{values[key]}\n\n" for key, heading in MEMORY_LAYERS if values[key]
    )


LEGEND = (
    "The tree below lists every non-ignored file in the project, including "
    "files whose contents were not sent. A file mapped to a list of "
    "signatures was included as a skeleton: those signatures are all you have "
    "of it. A file mapped to [] was either sent under one of the zone "
    "headings below — in full, or as its first lines, whichever that heading "
    "says — or not sent at all. If you cannot find it in a zone, you have not "
    "seen it. Never infer the contents of a file you were not given: name it "
    "in missing_context instead."
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


def _masker(
    env_vars: Optional[Dict[str, str]],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Always mask, never ask.

    In the TUI a human decides per variable. Here the payload is on its way to
    a third-party API and the caller may be a process that cannot answer, so
    the conservative answer is pre-selected rather than prompted for: masking
    leaks nothing, and a question nobody can answer is a hang (spec §12).
    """
    env_vars = env_vars or {}
    return env_vars, {key: "m" for key in env_vars}


def _block(rel: str, path: str, content: str, note: str = "") -> List[str]:
    # One trailing newline is dropped because the closing fence supplies the
    # line break itself. Most files end with one, so keeping it put a blank
    # line inside every code block in the payload — a line per file, paid for
    # on every turn. A file that does *not* end with a newline is unaffected:
    # the join still puts the fence on its own line.
    return [
        f"# FILE: {rel}{note}",
        f"```{get_language_for_file(path)}",
        content[:-1] if content.endswith("\n") else content,
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


def entry_content(entry: Entry) -> str:
    """What this entry's block contains. Chunks win over the role's render."""
    if entry.chunks is not None:
        return "\n".join(entry.chunks)
    return role_content(entry.path, entry.role)


def entry_note(entry: Entry) -> str:
    """The caveat for this entry, which is the role's unless it is chunked.

    Only meaningful *after* the entry's content has been read: a decode caveat
    is discovered by reading, not by inspecting the path. `_zone` orders the
    two calls accordingly.
    """
    if entry.chunks is not None:
        base = "selected patches"
    else:
        base = role_note(entry.role)
    damage = decode_note(entry.path)
    if not damage:
        return base
    return f"{base}; {damage}" if base else damage


#: The zones, in the order the model reads them: what the task centres on,
#: what surrounds it, then what it has only part of. One definition, because a
#: second surface with its own headings is a second prompt.
#:
#: These headings direct *attention*, and deliberately claim no authority over
#: what may be changed. They used to: this zone was "Active Workspace
#: (Editable)", the next was "Reference Context (Read-Only)", and `apply`
#: enforced exactly that split. The claim was a prediction made before the
#: question was asked — the caller had to guess the blast radius up front, and
#: `triage`, whose whole job is to find out which files matter, emitted a
#: selection that was by construction forbidden from being changed. The
#: permission now lives on `apply --only`, where the proposed patch is in hand
#: and the answer is known rather than guessed.
ZONES: Tuple[Tuple[str, str, str], ...] = (
    (
        PIN,
        "Working Set (Focus Here)",
        "The task centres on these files. They are sent whole and are never "
        "trimmed to fit a budget.",
    ),
    (
        REF,
        "Supporting Context",
        "Dependencies and call sites, sent whole. Change one only if the task "
        "genuinely needs it, and say why.",
    ),
    (
        SNIPPET,
        "Snippets (partial files)",
        "Only the first lines of each file are shown. Ask for the rest if you need it.",
    ),
)


def _zone(title: str, note: str, entries: Sequence[Entry], mask) -> List[str]:
    if not entries:
        return []
    out = [f"## {title}", "", note, ""]
    for e in entries:
        # Content first: a lossy decode is only known once the bytes have been
        # read, and the caveat that says so is the whole point of reporting it.
        body = mask(entry_content(e))
        caveat = entry_note(e)
        out += _block(e.rel, e.path, body, f" ({caveat})" if caveat else "")
    return out


def render_context(
    selection: Selection,
    *,
    ignore: Sequence[str],
    root: str,
    env_vars: Optional[Dict[str, str]] = None,
    search_paths: Optional[Sequence[str]] = None,
) -> str:
    """The structure tree and the file zones — the body every prompt shares.

    Called by `render_prefix` for `ask` and by the TUI's template renderer for
    the clipboard. Both get the same bytes for the same selection, which is
    the point: a prompt that behaves differently depending on which command
    produced it is two prompts wearing one name.

    `search_paths` exists for the TUI, which can be pointed at a subset of the
    tree; `ask` always walks from the project root.
    """
    env, decisions = _masker(env_vars)

    def mask(text: str) -> str:
        return handle_env_variables(text, env, decisions)

    map_paths = [e.path for e in selection.by_role(MAP)]
    out: List[str] = [
        "# Project Overview",
        "",
        "## Project Structure",
        "",
        LEGEND,
        "",
        "```json",
        get_project_structure(list(ignore), list(search_paths or [root]), map_paths),
        "```",
        "",
    ]
    for role, title, note in ZONES:
        out += _zone(title, note, selection.by_role(role), mask)
    return "\n".join(out)


def render_prefix(
    selection: Selection,
    *,
    ignore: Sequence[str],
    root: str,
    env_vars: Optional[Dict[str, str]] = None,
    project_context: Optional[str] = None,
    user_profile: Optional[str] = None,
    session_state: Optional[str] = None,
) -> str:
    """The stable half: the memory layers, the structure tree, and the zones.

    Everything above the task, in the order the clipboard prompt puts it. The
    task and the instruction tail are the suffix, which is the only part the
    two surfaces are allowed to differ on.
    """
    memory = render_memory(
        user_profile=user_profile,
        project_context=project_context,
        session_state=session_state,
    )
    return memory + render_context(
        selection, ignore=ignore, root=root, env_vars=env_vars
    )


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
                u.rel,
                u.rel,
                handle_env_variables(u.content, env, decisions),
                f" ({note})",
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
