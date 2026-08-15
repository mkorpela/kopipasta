"""The demotion ladder, and an estimator honest enough to drive it — spec §5.

Frontloading a whole codebase is not literally possible for real repos, even
at 1M tokens. The tool already renders a file three ways, so that becomes the
budget policy: over budget, files walk *down* a ladder rather than disappear.

    full content  ->  AST skeleton  ->  path-only (still in the structure tree)

Two properties make this safe to run unattended:

**Nothing vanishes silently.** A demoted file is still visible in the project
structure, and every demotion is reported — on stderr and in `--json`. Silent
truncation is what makes an answer confidently wrong, so the caller must
always be able to see what the oracle did *not* look at.

**The estimator is pessimistic on purpose.** `ops.estimate_tokens` assumes 3.6
chars/token; a real 46,102-char payload from this repo measured 18,474 tokens,
i.e. 2.50 chars/token — the estimator was 44% low (findings §2.5). Under-
counting is the dangerous direction: it silently overshoots the very window
`--budget` exists to protect. Dense code plus a minified JSON structure blob
tokenise far worse than prose, and this payload is mostly both.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from kopipasta.core.errors import UsageError
from kopipasta.core.resolver import MAP, REF, SNIPPET, Entry, Selection
from kopipasta.file import extract_symbols

#: Measured against real payloads from this repo, not assumed. See §2.5.
CHARS_PER_TOKEN = 2.5

#: `# FILE: path` + fenced block + blank lines.
FRAME_CHARS = 40

#: What a `-s` render costs: prompt.get_file_snippet caps at 50 lines/4096 bytes.
SNIPPET_CHARS = 4096

PATH_ONLY = "path-only"


def estimate_tokens(chars: int) -> int:
    """Chars -> tokens, biased pessimistic. Never used to *report* real usage."""
    return int(chars / CHARS_PER_TOKEN)


def parse_budget(text: Optional[str]) -> Optional[int]:
    """`400k` tokens, `1.5m` tokens, `40kc` literal characters -> tokens.

    Bare numbers are tokens because that is the unit the limit is expressed in
    everywhere else — the model's window, the price list, the usage report.
    """
    if text is None:
        return None
    raw = str(text).strip().lower()
    if not raw:
        return None
    as_chars = raw.endswith("c")
    if as_chars:
        raw = raw[:-1]
    mult = 1
    if raw.endswith("k"):
        mult, raw = 1_000, raw[:-1]
    elif raw.endswith("m"):
        mult, raw = 1_000_000, raw[:-1]
    try:
        value = float(raw) * mult
    except ValueError:
        raise UsageError(
            f"could not read --budget {text!r}.",
            detail="Expected a token count, optionally suffixed k/m, or 'c' for characters.",
            hint="--budget 400k      # 400,000 tokens\n--budget 40kc      # 40,000 characters",
        ) from None
    if value <= 0:
        raise UsageError(
            f"--budget must be positive, got {text!r}.",
            hint="Drop the flag entirely to run without a budget.",
        )
    return int(value / CHARS_PER_TOKEN) if as_chars else int(value)


@dataclass
class Demotion:
    path: str  # relative to the project root
    frm: str
    to: str
    saved_tokens: int

    def as_json(self) -> dict:
        return {"path": self.path, "from": self.frm, "to": self.to}

    def __str__(self) -> str:
        return f"{self.path}: {self.frm} -> {self.to}"


def _file_chars(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def render_chars(entry: Entry, role: Optional[str] = None) -> int:
    """What this entry costs, rendered in `role` (default: its current role)."""
    role = role or entry.role
    if role == PATH_ONLY:
        return 0  # the path is already in the structure tree; nothing is added
    if role == MAP:
        try:
            return sum(len(s) + 4 for s in extract_symbols(entry.path))
        except Exception:  # a file that will not parse simply has no skeleton
            return 0
    if role == SNIPPET:
        return min(_file_chars(entry.path), SNIPPET_CHARS) + FRAME_CHARS + len(entry.rel)
    return _file_chars(entry.path) + FRAME_CHARS + len(entry.rel)


def selection_chars(selection: Selection) -> int:
    return sum(render_chars(e) for e in selection.entries.values())


#: The ladder's stages, as (role, bulk-or-None) filters, in the order of
#: spec §5: `-e` never; then `-r`; then everything `--all` dragged in; then
#: explicitly named skeletons, because someone who typed a path meant it.
_STAGES: Tuple[Tuple[str, Optional[bool]], ...] = (
    (REF, None),
    (SNIPPET, None),
    (MAP, True),
    (MAP, False),
)


def _stage_entries(selection: Selection, role: str, bulk: Optional[bool]) -> List[Entry]:
    """One stage's candidates, largest first, computed when the stage starts.

    *When* matters. Building all four lists up front looks equivalent and is
    not: a `-r` file demoted to a skeleton in stage 1 would be missing from
    the stage-4 list that was snapshotted before it changed role, so it could
    never reach path-only and the ladder would stop one rung short of the
    budget it was given. Found by dogfooding the fix to the stage above it.
    """
    chosen = (
        e
        for e in selection.entries.values()
        if e.role == role and (bulk is None or e.bulk is bulk)
    )
    return sorted(chosen, key=render_chars, reverse=True)


def demote_to_fit(selection: Selection, budget_tokens: Optional[int]) -> List[Demotion]:
    """Walk the ladder until the selection fits, and report every step.

    Mutates `selection`: full-text roles become MAP, and MAP entries drop out
    of the selection entirely (they remain in the structure tree, which is
    what "path-only" means).

    Best-effort by construction — it works from file sizes, not from the
    rendered payload, so the project structure blob and the mode instructions
    are not counted here. The exact figure is measured after rendering and
    `--strict-budget` is enforced against *that*, so an underestimate here
    cannot become a silent overshoot.
    """
    if not budget_tokens:
        return []
    budget_chars = int(budget_tokens * CHARS_PER_TOKEN)
    total = selection_chars(selection)
    if total <= budget_chars:
        return []

    steps: List[Demotion] = []
    for role, bulk in _STAGES:
        for entry in _stage_entries(selection, role, bulk):
            if total <= budget_chars:
                return collapse(steps)
            before = render_chars(entry)
            if entry.role in (REF, SNIPPET):
                after = render_chars(entry, MAP)
                steps.append(
                    Demotion(entry.rel, entry.role, MAP, estimate_tokens(before - after))
                )
                entry.role = MAP
                total -= before - after
            else:
                steps.append(Demotion(entry.rel, MAP, PATH_ONLY, estimate_tokens(before)))
                selection.entries.pop(entry.path, None)
                total -= before
    return collapse(steps)


def collapse(steps: Sequence[Demotion]) -> List[Demotion]:
    """One line per file, not one per rung.

    A `-r` file can now fall the whole ladder in one run — full to skeleton in
    an early stage, skeleton to path-only in a later one — and reporting that
    as two demotions makes "demoted 30 files" a count of 30 events across
    fewer files. The caller cares where a file ended up, not how it got there.
    """
    merged: Dict[str, Demotion] = {}
    for step in steps:
        first = merged.get(step.path)
        if first is None:
            merged[step.path] = Demotion(step.path, step.frm, step.to, step.saved_tokens)
        else:
            first.to = step.to
            first.saved_tokens += step.saved_tokens
    return list(merged.values())


def summarise(demotions: Sequence[Demotion], limit: int = 8, label: str = "over budget") -> str:
    """The stderr narration. Names files, because a count is not actionable."""
    if not demotions:
        return ""
    saved = sum(d.saved_tokens for d in demotions)
    head = [f"  {d}" for d in demotions[:limit]]
    more = len(demotions) - limit
    if more > 0:
        head.append(f"  ... and {more} more")
    return "\n".join(
        [f"kopipasta: {label} — demoted {len(demotions)} file(s), ~{saved:,} tokens:", *head]
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "Demotion",
    "collapse",
    "PATH_ONLY",
    "demote_to_fit",
    "estimate_tokens",
    "parse_budget",
    "render_chars",
    "selection_chars",
    "summarise",
]
