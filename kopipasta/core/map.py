"""`kopipasta map` — the symbol skeleton, without spending a model call. Spec §3.

The cheapest useful thing this tool knows how to do. `ask --all` frontloads a
repository into someone else's context window and bills for it; `map` renders
the same skeleton locally and hands it back, so a caller can decide *what to
read* before deciding *what to pay for*.

    kopipasta map --json > map.json          # the whole repo, signatures only
    kopipasta map kopipasta/core --json      # one subsystem

Three rules it inherits rather than reinvents:

**The selection grammar is the same one (spec §4)**, so a selection that works
here works verbatim in `ask`. What it does *not* inherit is the role
distinction: `map` renders a skeleton whichever flag selected the file, because
a verb named for one rendering that silently produced three would be a trap.
`-x` still excludes, and `--from-file` still closes the triage loop.

**Nothing vanishes silently (spec §5).** Under `--budget` files fall to
path-only exactly as they do in `ask` — and a path-only file is still *listed*,
with an empty symbol list. A map that quietly omitted a file would be worse
than no map at all: the caller would conclude the file does not exist.

**No session, no cache, no network.** There is nothing to record, because
nothing was asked and nothing was spent. `map` never writes to `.kopipasta/`.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Sequence

from kopipasta.cache import find_project_root
from kopipasta.config import read_gitignore
from kopipasta.core import budget as budgetmod
from kopipasta.core.errors import EXIT_OK, BudgetExceeded, KopipastaError
from kopipasta.core.resolver import MAP, SelectionSpec, resolve
from kopipasta.file import extract_symbols
from kopipasta.output import (
    HelpToStdoutParser,
    emit,
    emit_json,
    narrate,
    stdout_reserved_for_output,
)


def build_parser() -> argparse.ArgumentParser:
    from kopipasta.core.ask import add_selection_args

    p = HelpToStdoutParser(
        prog="kopipasta map",
        description="Print the symbol skeleton of this repository. No model, no cost.",
    )
    p.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="files or directories to map (default: the whole project)",
    )
    add_selection_args(p)
    b = p.add_argument_group("budget")
    b.add_argument(
        "--budget",
        metavar="SIZE",
        help="cap the output, e.g. 400k tokens or 40kc characters.\n"
        "Unset by default: every selected file is mapped.",
    )
    b.add_argument(
        "--strict-budget",
        action="store_true",
        help="exit 6 instead of dropping files to path-only",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="stdout becomes a single JSON object (spec §8)",
    )
    p.epilog = (
        (p.epilog or "")
        + "\n\nEvery selected file is rendered as a skeleton, whichever flag selected it.\n"
        "With no selectors at all, the whole project is mapped."
    )
    return p


def run(argv: Sequence[str]) -> int:
    """Parse, render, report. Returns the exit code; never raises."""
    with stdout_reserved_for_output():
        return _run_parsed(argv)


def _run_parsed(argv: Sequence[str]) -> int:
    from kopipasta.core.ask import report_failure

    argv = list(argv)
    try:
        args = build_parser().parse_args(argv)
    except KopipastaError as exc:
        # See ask._run_parsed: the parser raises rather than exiting 2, and
        # --json has to be read off argv because `args` does not exist yet.
        return report_failure(exc, json_mode="--json" in argv)
    try:
        return _map(args)
    except KopipastaError as exc:
        return report_failure(exc, json_mode=args.json)


def _spec_from(args: argparse.Namespace) -> SelectionSpec:
    """Positional paths are selectors too.

    `kopipasta map kopipasta/core` is the obvious way to type it, and a verb
    that answered it with "nothing was selected" would be answering a question
    nobody asked. They join the map role, which is the only role this verb has.
    """
    spec = SelectionSpec(
        edit=args.edit or [],
        ref=args.ref or [],
        map=(args.map or []) + list(args.paths or []),
        snippet=args.snippet or [],
        exclude=args.exclude or [],
        all=args.all,
        changed=args.changed,
        changed_since=args.changed_since,
        from_file=args.from_file,
    )
    if spec.is_empty():
        # The whole repo is the point of the verb (spec §3: "cheap whole-repo
        # map"), so an empty command line is the common case, not an error.
        spec.all = True
    return spec


def _map(args: argparse.Namespace) -> int:
    root = str(find_project_root())
    ignore = read_gitignore()
    selection = resolve(_spec_from(args), ignore, root)

    # One role, whatever the flags said. `-e` here would otherwise render a
    # whole file into something advertised as a skeleton.
    for entry in selection.entries.values():
        entry.role = MAP

    budget_tokens = budgetmod.parse_budget(args.budget)
    # Measured before the ladder runs. `demote_to_fit` mutates the selection,
    # so asking afterwards reports the size that *did* fit and produces the
    # nonsense "needs ~1,878 tokens, over the 2,000 budget". Found by running
    # it, not by reading it.
    wanted = budgetmod.estimate_tokens(budgetmod.selection_chars(selection))
    demotions = budgetmod.demote_to_fit(selection, budget_tokens)
    if demotions and args.strict_budget:
        raise BudgetExceeded(wanted, budget_tokens or 0, [d.path for d in demotions])

    ordered = _skeleton(selection, demotions)
    text = _render(ordered)

    if budget_tokens:
        # The ladder works from file sizes and cannot see the path line every
        # file costs — including the path-only ones, which are all path line.
        # One corrective pass with that overhead measured rather than guessed
        # is the difference between a budget that holds and one that is merely
        # reported as exceeded. The overhead does not change as files demote,
        # so a single pass converges.
        overhead = len(text) - budgetmod.selection_chars(selection)
        room = budget_tokens - budgetmod.estimate_tokens(overhead)
        extra = budgetmod.demote_to_fit(selection, max(1, room))
        if extra and args.strict_budget:
            # A second place that demotes is a second place that has to honour
            # the flag whose entire purpose is to forbid demoting.
            raise BudgetExceeded(
                budgetmod.estimate_tokens(len(text)),
                budget_tokens,
                [d.path for d in demotions + extra],
            )
        if extra:
            demotions = budgetmod.collapse(demotions + extra)
            ordered = _skeleton(selection, demotions)
            text = _render(ordered)

    symbols = sum(len(v) for v in ordered.values())
    mapped = sum(1 for v in ordered.values() if v)

    payload: Dict[str, Any] = {
        "ok": True,
        "files": len(ordered),
        "with_symbols": mapped,
        "symbols": symbols,
        "chars": len(text),
        "est_tokens": budgetmod.estimate_tokens(len(text)),
        "map": ordered,
    }
    if demotions:
        payload["path_only"] = [d.path for d in demotions]
    if selection.unmatched():
        payload["unmatched"] = [
            {"flag": f, "pattern": p} for f, p in selection.unmatched()
        ]

    if args.json:
        emit_json(payload)
    else:
        emit(text)

    for flag, pattern in selection.unmatched():
        # Never silent: a typo'd selector that still leaves files selected is
        # the shape that produces a confident answer from the wrong context.
        narrate(f"kopipasta: {flag} {pattern} matched no files.")
    if demotions:
        narrate(
            f"kopipasta: over budget — {len(demotions)} file(s) listed as path-only, "
            "with no symbols."
        )
    narrate(
        f"kopipasta: {len(ordered)} files, {symbols} symbols, "
        f"~{payload['est_tokens']:,} tokens"
    )
    if budget_tokens and payload["est_tokens"] > budget_tokens:
        # Every file is down to its path and it still does not fit. Said out
        # loud rather than left in the numbers: the alternative to admitting
        # this is dropping files, and a map that omits files is a lie.
        narrate(
            f"kopipasta: still over the {budget_tokens:,} budget with every file at "
            "path-only — the path list alone is bigger than the budget."
        )
    return EXIT_OK


def _skeleton(selection, demotions) -> Dict[str, List[str]]:
    """path -> symbols, in path order. A demoted file is listed with none.

    "path-only" means the path survives. A caller who cannot see a file at all
    would conclude it does not exist, which is the one thing a map must never
    say — so demotion costs the symbols, never the line.
    """
    out: Dict[str, List[str]] = {d.path: [] for d in demotions}
    for entry in selection.by_role(MAP):
        try:
            out[entry.rel] = extract_symbols(entry.path)
        except Exception:  # noqa: BLE001 - a file that will not parse has no skeleton
            out[entry.rel] = []
    return dict(sorted(out.items()))


def _render(skeleton: Dict[str, List[str]]) -> str:
    """The text artifact: a path per file, its symbols indented beneath it.

    Indentation rather than a nested tree because the consumer greps it. A
    file with no symbols is one bare line, which is exactly what it is worth.
    """
    out: List[str] = []
    for rel, symbols in skeleton.items():
        out.append(rel)
        out += [f"    {s}" for s in symbols]
    return "\n".join(out)


__all__ = ["build_parser", "run"]
