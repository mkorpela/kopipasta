"""Patterns from argv -> a resolved selection — spec §4.

The TUI's three-state selection model maps one-to-one onto flags, which is
what makes its core concept work headlessly:

    -p/--pin       full content, never demoted  the working set
    -r/--ref       full content                 dependencies and call sites
    -m/--map       AST skeleton                 signatures and one docstring line
    -s/--snippet   first 50 lines               a coarse peek
    -x/--exclude   dropped                      applied last, wins over everything

`--pin` is a claim about attention and budget priority, not about permission.
It was `-e/--edit`, and it did carry a write permission: `apply` refused any
patch against a file outside it. That permission was a prediction made before
the question was asked, so it now lives on `apply --only`, where the proposed
patch is in hand. What survives the move is the never-demoted guarantee, which
could not move: the budget ladder runs at assembly time, before a patch exists.

Two properties the rest of the pipeline depends on:

**Resolution is by role precedence, not argv order.** Flags are documented as
order-independent, so `-m '**/*.py' --pin kopipasta/patcher.py` has to mean the
same thing whichever way round it is typed: skeleton the tree, but that one
file in full. Detail wins — pin > ref > snippet > map.

**Every pattern carries its match count.** A typo'd glob that selects nothing
is the most dangerous failure in the tool: the model answers from the project
structure alone and the answer reads fine. Counting per pattern is what lets
the caller be told *which* selector was wrong rather than "0 files".

**A role a file cannot be rendered in is not a role it keeps.** `-m` promises
a skeleton, and `extract_symbols` only has one for Python and the TS/JS
family. Everything else — a `.md`, a `.sql`, a `.py` that will not parse —
resolved to a role that renders to nothing at all, so the file left no trace
in the payload while `sent: {map: N}` counted it. See `_fall_back_from_map`.
"""

from __future__ import annotations

import glob as globlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from kopipasta.core.errors import EmptySelection, UsageError
from kopipasta.file import decode_text, extract_symbols, is_binary, is_ignored
from kopipasta.proc import TEXT

#: The focus role. It is a statement about *attention and priority*, not about
#: permission: pinned files are sent in full and are never demoted to fit a
#: budget. It does not restrict what a patch may touch — `apply --only` does
#: that, at the point where the proposed changes are actually known.
PIN = "pin"
REF = "ref"
MAP = "map"
SNIPPET = "snippet"

#: Least detail first. Later assignments win, so this order *is* the precedence.
ROLE_ORDER: Tuple[str, ...] = (MAP, SNIPPET, REF, PIN)

ROLE_FLAG = {PIN: "--pin", REF: "-r", MAP: "-m", SNIPPET: "-s"}

#: Roles whose rendering is the file's actual text. These are the expensive
#: ones, the ones the budget ladder demotes, and the ones session dedup can
#: skip resending.
FULL_TEXT_ROLES = (PIN, REF, SNIPPET)

#: Roles as they may appear in a selection.json written by an older version.
#: "edit" was renamed to "pin" when the write-permission meaning moved to
#: `apply --only`; the records outlive the rename, so reads normalise.
LEGACY_ROLES = {"edit": PIN}


@dataclass
class Entry:
    """One file, with the role it was selected in and where that came from."""

    path: str  # absolute
    rel: str  # relative to the project root, forward slashes
    role: str
    #: True when the file arrived via --all or a directory/glob expansion
    #: rather than being named. The budget ladder demotes bulk before
    #: explicit: someone who typed a path meant that path.
    bulk: bool = True
    #: The TUI's "selected patches": specific hunks of a file rather than the
    #: whole of it. No flag selects this — it exists so the interactive
    #: selection can be rendered by the same renderer as everything else,
    #: rather than by a second one that drifts.
    chunks: Optional[List[str]] = None

    @property
    def flag(self) -> str:
        return ROLE_FLAG.get(self.role, "")


@dataclass
class Selection:
    root: str
    entries: Dict[str, Entry] = field(default_factory=dict)
    #: (flag, pattern, matched) in the order the caller wrote them, including
    #: the ones that matched nothing.
    patterns: List[Tuple[str, str, int]] = field(default_factory=list)
    #: Files named by `-m` that have no skeleton to render, and so were moved
    #: to the snippet role. Reported, never silent: the caller asked for one
    #: rendering and got another.
    unmappable: List[str] = field(default_factory=list)

    def by_role(self, *roles: str) -> List[Entry]:
        return sorted(
            (e for e in self.entries.values() if e.role in roles), key=lambda e: e.rel
        )

    def counts(self) -> Dict[str, int]:
        """Most detail first, which is also the order a reader cares about."""
        out = {r: 0 for r in reversed(ROLE_ORDER)}
        for e in self.entries.values():
            out[e.role] = out.get(e.role, 0) + 1
        return out

    def unmatched(self) -> List[Tuple[str, str]]:
        return [(f, p) for f, p, n in self.patterns if n == 0]

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class SelectionSpec:
    """The selection flags, as parsed. One field per flag in spec §4."""

    pin: List[str] = field(default_factory=list)
    ref: List[str] = field(default_factory=list)
    map: List[str] = field(default_factory=list)
    snippet: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    all: bool = False
    changed: bool = False
    changed_since: Optional[str] = None
    from_file: Optional[str] = None

    def is_empty(self) -> bool:
        return not any(
            (
                self.pin,
                self.ref,
                self.map,
                self.snippet,
                self.all,
                self.changed,
                self.changed_since,
                self.from_file,
            )
        )


# --------------------------------------------------------------------------
# expansion
# --------------------------------------------------------------------------
def _usable(path: str, ignore: Sequence[str], root: str) -> bool:
    return (
        os.path.isfile(path)
        and not is_ignored(path, list(ignore), root)
        and not is_binary(path)
    )


def expand(pattern: str, ignore: Sequence[str], root: str) -> List[str]:
    """One pattern -> absolute paths. Globs, directories and literal paths.

    `.gitignore` and binary filtering always apply, so a glob cannot drag
    `node_modules` or a `.png` into a payload by accident.
    """
    pattern = os.path.expanduser(pattern)
    base = pattern if os.path.isabs(pattern) else os.path.join(root, pattern)
    if os.path.isdir(base):
        base = os.path.join(base, "**", "*")
    hits = globlib.glob(base, recursive=True)
    return sorted(
        {os.path.abspath(h) for h in hits if _usable(os.path.abspath(h), ignore, root)}
    )


def walk_all(ignore: Sequence[str], root: str) -> List[str]:
    """Every non-ignored, non-binary file under the project root."""
    out: List[str] = []
    patterns = list(ignore)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [
            d for d in dirs if not is_ignored(os.path.join(dirpath, d), patterns, root)
        ]
        for f in files:
            p = os.path.join(dirpath, f)
            if not is_ignored(p, patterns, root) and not is_binary(p):
                out.append(p)
    return sorted(out)


def _git(root: str, *args: str) -> List[str]:
    git = shutil.which("git")
    if not git:
        raise UsageError(
            "git is not on PATH.",
            detail="--changed and --changed-since need it.",
            hint="Select files explicitly with -e/-r/-m instead.",
        )
    try:
        r = subprocess.run(
            [git, *args], cwd=root, capture_output=True, timeout=30, **TEXT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageError(f"could not run git: {exc}") from exc
    if r.returncode != 0:
        raise UsageError(
            f"git {' '.join(args)} failed.",
            detail=(r.stderr or "").strip()[:400],
            hint="Check the ref exists, and that this is a git repository.",
        )
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def changed_files(root: str, since: Optional[str] = None) -> List[str]:
    """Working-tree changes, or `git diff --name-only <ref>...HEAD`.

    Untracked files are included for the working-tree case: a file you just
    created is exactly the file you are working on, and leaving it out would
    silently answer the question from a stale tree.
    """
    if since:
        names = _git(root, "diff", "--name-only", f"{since}...HEAD")
    else:
        names = _git(root, "diff", "--name-only", "HEAD")
        names += _git(root, "ls-files", "--others", "--exclude-standard")
    return [os.path.join(root, n) for n in dict.fromkeys(names)]


def read_path_list(path: str, root: str) -> List[str]:
    """`--from-file` / `@file`: newline-delimited paths, `#` comments allowed.

    This closes the loop — the `suggested_selection` from a triage answer is
    written straight back in as the selection for the follow-up call.
    """
    target = path if os.path.isabs(path) else os.path.join(root, path)
    try:
        # A UTF-16 path list decoded as UTF-8 becomes a wall of U+FFFD, and
        # every path in it then "matched no files" — a wrong answer that looks
        # like an ordinary empty selection.
        with open(target, "rb") as fh:
            lines = decode_text(fh.read())[0].splitlines()
    except OSError as exc:
        raise UsageError(
            f"could not read the path list {path}.",
            detail=str(exc),
            hint="--from-file takes a file of newline-delimited paths.",
        ) from exc
    return [
        ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _patterns_for(values: Sequence[str], root: str) -> List[str]:
    """Expand any `@file` argument into the patterns it contains."""
    out: List[str] = []
    for v in values:
        if v.startswith("@"):
            out.extend(read_path_list(v[1:], root))
        else:
            out.append(v)
    return out


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
def resolve(
    spec: SelectionSpec, ignore: Sequence[str], root: str, *, changed_role: str = PIN
) -> Selection:
    """Build the selection, recording what every pattern matched.

    Raises EmptySelection when nothing at all was selected — an error rather
    than a warning, because a plausible answer produced from nothing is the
    worst failure shape this tool has.
    """
    sel = Selection(root=root)

    def assign(paths: Sequence[str], role: str, *, bulk: bool) -> int:
        n = 0
        for p in paths:
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            sel.entries[p] = Entry(path=p, rel=rel, role=role, bulk=bulk)
            n += 1
        return n

    # --all and the git selectors first: they are the background, and anything
    # named explicitly afterwards should be able to override them.
    if spec.all:
        # Read-only, and *whole*. The product is a large context window with
        # the codebase actually inside it; starting every file at the skeleton
        # rung spent the ladder's entire range before the caller asked for
        # anything, left the oracle reasoning about signatures, and sent
        # nothing at all for a language with no skeletons to extract.
        # `--budget` is the throttle, and demotion is what it pulls.
        sel.patterns.append(
            ("--all", "", assign(walk_all(ignore, root), REF, bulk=True))
        )

    if spec.changed or spec.changed_since:
        flag = "--changed-since" if spec.changed_since else "--changed"
        found = [
            p
            for p in changed_files(root, spec.changed_since)
            if _usable(p, ignore, root)
        ]
        sel.patterns.append(
            (flag, spec.changed_since or "", assign(found, changed_role, bulk=False))
        )

    if spec.from_file:
        listed = read_path_list(spec.from_file, root)
        n = 0
        for pattern in listed:
            n += assign(expand(pattern, ignore, root), REF, bulk=False)
        sel.patterns.append(("--from-file", spec.from_file, n))

    # Explicit roles, least detail first, so the most detailed assignment wins
    # regardless of the order the flags were typed in.
    for role in ROLE_ORDER:
        for pattern in _patterns_for(getattr(spec, role) or [], root):
            hits = expand(pattern, ignore, root)
            sel.patterns.append((ROLE_FLAG[role], pattern, len(hits)))
            assign(hits, role, bulk=False)

    # Exclusion is applied last and wins over everything, including --pin.
    for pattern in _patterns_for(spec.exclude or [], root):
        hits = expand(pattern, ignore, root)
        dropped = 0
        for p in hits:
            if sel.entries.pop(p, None) is not None:
                dropped += 1
        sel.patterns.append(("-x", pattern, dropped))

    if not sel.entries:
        raise EmptySelection(sel.patterns, candidates=_candidates(ignore, root))
    _fall_back_from_map(sel)
    return sel


def has_skeleton(path: str) -> bool:
    """Can this file be rendered as a skeleton at all?

    `extract_symbols` covers Python and the TS/JS family and returns nothing
    for everything else, for a file that will not parse, and for one with no
    top-level symbols. All three are indistinguishable from "not selected"
    once rendered, which is why this question has to be asked before a role is
    kept rather than after the payload is built.
    """
    try:
        return bool(extract_symbols(path))
    except Exception:  # noqa: BLE001 - a file that will not parse has no skeleton
        return False


def _fall_back_from_map(sel: Selection) -> None:
    """A map role a file cannot be rendered in becomes a snippet.

    The map role is the one role whose rendering does not exist for every
    file, and a MAP entry with no symbols contributes *nothing* to the
    payload: it appears in the structure tree as `[]`, which the payload's own
    legend defines as "not sent at all". So `-m docs/spec.md` reported
    `map: 1`, recorded the file in `selection.json`, and sent the model zero
    bytes of it — a selected file the caller was told had been sent.

    Someone who typed a path meant that path, so it falls back to the cheapest
    rendering every file has: the first 50 lines. Files dragged in by `--all`
    are left alone, because path-only in the structure tree is exactly what
    `--all` promises (spec §5) and 4KB apiece across a whole repository is
    not.
    """
    for entry in sel.entries.values():
        if entry.role == MAP and not entry.bulk and not has_skeleton(entry.path):
            entry.role = SNIPPET
            sel.unmappable.append(entry.rel)
    sel.unmappable.sort()


def _candidates(ignore: Sequence[str], root: str) -> List[str]:
    """Real paths, for "did you mean?". Only ever computed on the error path."""
    try:
        return [
            os.path.relpath(p, root).replace(os.sep, "/")
            for p in walk_all(ignore, root)
        ]
    except OSError:  # pragma: no cover - a walk that fails is not worth failing over
        return []
