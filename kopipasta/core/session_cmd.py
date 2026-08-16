"""`kopipasta session` — read and retire the conversations on disk. Spec §7.

The session record is deliberately plain files in the repo, so most of what a
human wants is already `ls` and `cat`. This verb exists for the four questions
those cannot answer:

    session ls              which conversations exist, and what did they cost
    session show [id]       what happened in one, as pointers rather than payloads
    session diff [id]       has the code moved since the oracle read it
    session rm  [id|--all]  delete one, and hand back what it is renting
    session reap            hand back rented caches nothing is using any more

`diff` is the one with no equivalent. A session's answer is only as good as
the bytes it was given, and those bytes are recorded by hash — so "the oracle
is reasoning about a version of `patcher.py` that no longer exists" is a
question this tool can answer and `git status` cannot.

`rm` and `reap` exist because a Gemini cache is *rented*: billed per
token-hour until its TTL expires. Deleting a session directory without handing
its lease back leaves the meter running with nothing on disk to say what is
being paid for, and a sweep that ignores live leases deletes the cache turn 2
was about to reuse.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional, Sequence

from kopipasta.cache import find_project_root, get_project_key
from kopipasta.core.context import content_hash
from kopipasta.core.errors import EXIT_OK, KopipastaError, UsageError
from kopipasta.core.session import (
    STATE_DIR,
    Session,
    clear_current,
    list_sessions,
    live_leases,
    read_lease,
    read_meta,
    read_selection,
    read_turns,
    remove_session,
    session_dir,
)
from kopipasta.output import (
    HelpToStdoutParser,
    emit,
    emit_json,
    narrate,
    stdout_reserved_for_output,
)

#: How much of a question or answer belongs in a listing. Pointers, not
#: payloads (spec §2) — the full text is one `cat` away, and putting it here
#: would put a 4,000-token answer back into the context this tool exists to
#: protect.
HEAD_CHARS = 100


def build_parser() -> argparse.ArgumentParser:
    p = HelpToStdoutParser(
        prog="kopipasta session",
        description="Inspect and retire the conversations in .kopipasta/sessions/.",
    )
    # On the top level as well as on every subcommand, so that `session --json`
    # — the shape an agent reaches for when it has forgotten the subcommand —
    # is answered with an error object rather than argparse prose about an
    # unrecognized argument.
    p.add_argument(
        "--json",
        action="store_true",
        help="stdout becomes a single JSON object (spec §8)",
    )
    subs = p.add_subparsers(dest="sub")

    def sub(name: str, help: str) -> argparse.ArgumentParser:
        s = subs.add_parser(name, help=help)
        # SUPPRESS, not store_true: a subparser default overwrites whatever the
        # parent already parsed, so a plain `--json` on the subparser would
        # turn `session --json ls` back into text output.
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return s

    sub("ls", "list conversations, newest last")

    show = sub("show", "one conversation: turns, usage, artifact paths")
    show.add_argument(
        "id",
        nargs="?",
        default="current",
        metavar="ID",
        help="session id, or 'current' (default)",
    )

    diff = sub(
        "diff", "which files have changed on disk since the context was assembled"
    )
    diff.add_argument("id", nargs="?", default="current", metavar="ID")

    rm = sub("rm", "delete a conversation, handing back what it rents")
    rm.add_argument("id", nargs="?", metavar="ID")
    rm.add_argument(
        "--all", action="store_true", help="every conversation in this project"
    )

    reap = sub("reap", "hand back provider caches this project is no longer using")
    reap.add_argument("--dry-run", action="store_true", help="report, delete nothing")

    p.epilog = (
        "Sessions live in .kopipasta/sessions/<id>/ as plain files: the request, the\n"
        "response and the record of exactly what was sent. `rm -rf .kopipasta` is a\n"
        "complete reset."
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    return p


def run(argv: Sequence[str]) -> int:
    """Parse, report. Returns the exit code; never raises."""
    with stdout_reserved_for_output():
        return _run_parsed(argv)


def _run_parsed(argv: Sequence[str]) -> int:
    from kopipasta.core.ask import report_failure

    argv = list(argv)
    try:
        args = build_parser().parse_args(argv)
    except KopipastaError as exc:
        return report_failure(exc, json_mode="--json" in argv)
    if not getattr(args, "sub", None):
        # argparse would accept a bare `kopipasta session` and hand back an
        # empty namespace. Nothing sensible happens next, so say what the
        # options are instead of picking one.
        return report_failure(
            UsageError(
                "kopipasta session needs a subcommand.",
                hint="kopipasta session ls\n"
                "kopipasta session show [id]\n"
                "kopipasta session diff [id]\n"
                "kopipasta session rm [id|--all]\n"
                "kopipasta session reap",
            ),
            json_mode="--json" in argv,
        )
    try:
        return _dispatch(args)
    except KopipastaError as exc:
        return report_failure(exc, json_mode=args.json)


def _dispatch(args: argparse.Namespace) -> int:
    root = str(find_project_root())
    if args.sub == "ls":
        return _ls(root, args)
    if args.sub == "show":
        return _show(root, args)
    if args.sub == "diff":
        return _diff(root, args)
    if args.sub == "rm":
        return _rm(root, args)
    return _reap(root, args)


# --------------------------------------------------------------------------
# resolving which session is meant
# --------------------------------------------------------------------------
def _resolve(root: str, session_id: Optional[str]) -> str:
    """A session id, or what `current` points at.

    `current` is followed here and never in `ask`, and the difference is not
    an inconsistency: reading a racy pointer to *report* on a session costs
    nothing, while resuming one silently lands a question in somebody else's
    conversation.
    """
    known = list_sessions(root)
    if not session_id or session_id == "current":
        resolved = Session.read_current(root)
        if not resolved:
            raise UsageError(
                "there is no 'current' session.",
                detail=f"No conversation is recorded in {STATE_DIR}/."
                if not known
                else f"{len(known)} session(s) exist but none is current.",
                hint="kopipasta session ls          # what exists\n"
                "kopipasta ask -q '...'        # start one",
            )
        return resolved
    if session_id not in known:
        # Names the ones that do exist: with dated ids, a typo and a
        # half-remembered id are the common cases and both are recoverable
        # from a list.
        raise UsageError(
            f"no session {session_id!r} in this project.",
            detail=("Known: " + ", ".join(known[-8:])) if known else "There are none.",
            hint="kopipasta session ls",
        )
    return session_id


def _lease_json(root: str, session_id: str) -> Optional[Dict[str, Any]]:
    lease = read_lease(root, session_id)
    if not lease:
        return None
    return {
        "provider": lease.get("provider"),
        "model": lease.get("model"),
        "tokens": lease.get("tokens"),
        "expires_in_s": lease["expires_in_s"],
        "expired": lease["expired"],
    }


def _summary(root: str, session_id: str, current: Optional[str]) -> Dict[str, Any]:
    meta = read_meta(root, session_id)
    totals = meta.get("totals") or {}
    return {
        "id": session_id,
        "current": session_id == current,
        "turns": meta.get("turns", len(read_turns(root, session_id))),
        "created": meta.get("created"),
        "updated": meta.get("updated"),
        "backend": meta.get("backend"),
        "model": meta.get("model"),
        "git_head": meta.get("git_head"),
        "totals": totals,
        "cache": _lease_json(root, session_id),
        "path": os.path.relpath(session_dir(root, session_id), root).replace(
            os.sep, "/"
        ),
    }


# --------------------------------------------------------------------------
# ls
# --------------------------------------------------------------------------
def _ls(root: str, args: argparse.Namespace) -> int:
    current = Session.read_current(root)
    sessions = [_summary(root, s, current) for s in list_sessions(root)]

    if args.json:
        emit_json({"ok": True, "current": current, "sessions": sessions})
        return EXIT_OK

    if not sessions:
        # Not an error: no conversations is the normal state of a fresh repo.
        emit("")
        narrate(f"kopipasta: no sessions in {STATE_DIR}/sessions/.")
        return EXIT_OK

    rows = [
        f"{'':1} {'ID':<18} {'TURNS':>5}  {'UPDATED':<19} {'IN':>9} {'CACHED':>9}  BACKEND"
    ]
    for s in sessions:
        totals = s["totals"]
        rows.append(
            f"{'*' if s['current'] else ' ':1} {s['id']:<18} {s['turns']:>5}  "
            f"{(s['updated'] or '-'):<19} {totals.get('input', 0):>9,} "
            f"{totals.get('cached', 0):>9,}  {s['backend'] or '-'}"
            + (_lease_note(s["cache"]) or "")
        )
    emit("\n".join(rows))
    narrate("kopipasta: * is `current` — the session `apply current` would use.")
    return EXIT_OK


def _lease_note(cache: Optional[Dict[str, Any]]) -> str:
    if not cache or cache.get("expired"):
        return ""
    return f"  [cache {int(cache.get('tokens') or 0):,} tok, {cache['expires_in_s']:.0f}s left]"


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------
def _show(root: str, args: argparse.Namespace) -> int:
    session_id = _resolve(root, args.id)
    summary = _summary(root, session_id, Session.read_current(root))
    turn_no, files = read_selection(root, session_id)

    turns: List[Dict[str, Any]] = []
    for rec in read_turns(root, session_id):
        n = int(rec.get("turn") or 0)
        turns.append(
            {
                "turn": n,
                "at": rec.get("at"),
                "mode": rec.get("mode"),
                "question": _head(rec.get("question")),
                "answer": _head(rec.get("answer")),
                "request": f"{summary['path']}/{n:03d}-request.md",
                "response": f"{summary['path']}/{n:03d}-response.md",
            }
        )

    roles: Dict[str, int] = {}
    for rec in files.values():
        role = str(rec.get("role") or "?")
        roles[role] = roles.get(role, 0) + 1

    payload = {
        **summary,
        "ok": True,
        "context": {"turn": turn_no, "files": len(files), "roles": roles},
        "turns": turns,
    }
    if args.json:
        emit_json(payload)
        return EXIT_OK

    lines = [
        f"session {session_id}{'  (current)' if summary['current'] else ''}",
        f"  backend   {summary['backend'] or '-'}  model {summary['model'] or '-'}",
        f"  created   {summary['created'] or '-'}   updated {summary['updated'] or '-'}",
        f"  git head  {summary['git_head'] or '-'}",
        f"  totals    in {summary['totals'].get('input', 0):,} "
        f"cached {summary['totals'].get('cached', 0):,} "
        f"out {summary['totals'].get('output', 0):,}",
        f"  context   {len(files)} file(s) as of turn {turn_no}: "
        + (", ".join(f"{k} {v}" for k, v in sorted(roles.items())) or "none recorded"),
    ]
    cache = summary["cache"]
    if cache:
        state = "expired" if cache["expired"] else f"{cache['expires_in_s']:.0f}s left"
        lines.append(f"  cache     {cache['tokens']:,} tokens, {state}")
    for t in turns:
        lines += [
            "",
            f"  turn {t['turn']}  {t['at'] or ''}  --mode {t['mode'] or '?'}",
            f"    q: {t['question']}",
            f"    a: {t['answer']}",
            f"    {t['request']}",
            f"    {t['response']}",
        ]
    emit("\n".join(lines))
    return EXIT_OK


def _head(text: Any) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:HEAD_CHARS] + ("…" if len(flat) > HEAD_CHARS else "")


# --------------------------------------------------------------------------
# diff — has the code moved since the oracle read it
# --------------------------------------------------------------------------
def _diff(root: str, args: argparse.Namespace) -> int:
    """Compare the recorded content hashes against the files on disk.

    The question this answers is "is the answer I am holding still about the
    code I have". Nothing else in the toolchain can answer it: `git status`
    knows what changed since the last commit, and the session knows what it
    read — but only the session record knows *which bytes* were sent.
    """
    session_id = _resolve(root, args.id)
    turn_no, files = read_selection(root, session_id)

    stale: List[Dict[str, str]] = []
    fresh = 0
    for rel, rec in sorted(files.items()):
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            stale.append({"path": rel, "state": "gone", "role": rec.get("role", "?")})
            continue
        if content_hash(path) != rec.get("hash"):
            stale.append(
                {"path": rel, "state": "changed", "role": rec.get("role", "?")}
            )
        else:
            fresh += 1

    payload = {
        "ok": True,
        "session": session_id,
        "turn": turn_no,
        "checked": len(files),
        "fresh": fresh,
        "stale": stale,
    }
    if args.json:
        emit_json(payload)
        return EXIT_OK

    if not files:
        emit("")
        narrate(
            f"kopipasta: session {session_id} has no recorded selection to compare."
        )
        return EXIT_OK
    if not stale:
        emit(
            f"{session_id}: all {fresh} recorded file(s) are unchanged since turn {turn_no}."
        )
        return EXIT_OK
    emit("\n".join([f"{s['state']:<8} {s['path']}  ({s['role']})" for s in stale]))
    narrate(
        f"kopipasta: {len(stale)} of {len(files)} file(s) have moved since turn {turn_no}. "
        "Answers from this session are about the older copy."
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# rm — and hand back what the session was renting
# --------------------------------------------------------------------------
def _rm(root: str, args: argparse.Namespace) -> int:
    if args.all and args.id:
        raise UsageError(
            "give an id or --all, not both.",
            hint="kopipasta session rm 2026-08-15-a3f9\nkopipasta session rm --all",
        )
    if not args.all and not args.id:
        # Never defaults to `current`. A destructive verb that guesses its
        # target from a racy pointer is a footgun, and the id is one `ls` away.
        raise UsageError(
            "kopipasta session rm needs an id, or --all.",
            hint="kopipasta session ls          # the ids\n"
            "kopipasta session rm --all    # every conversation in this project",
        )

    targets = list_sessions(root) if args.all else [_resolve(root, args.id)]
    current = Session.read_current(root)

    removed: List[str] = []
    released: List[Dict[str, Any]] = []
    for session_id in targets:
        # The lease first: the resource name lives only in the session's
        # cache.json, so deleting the directory first would leave a rented
        # cache with nothing on disk to say what is being paid for.
        lease = read_lease(root, session_id)
        if lease and not lease["expired"] and _release(lease):
            released.append(
                {
                    "session": session_id,
                    "provider": lease.get("provider"),
                    "tokens": lease.get("tokens"),
                }
            )
        if remove_session(root, session_id):
            removed.append(session_id)

    current_cleared = bool(current and current in removed)
    if current_cleared:
        clear_current(root)

    payload = {
        "ok": True,
        "removed": removed,
        "released": released,
        "current_cleared": current_cleared,
    }
    if args.json:
        emit_json(payload)
        return EXIT_OK
    emit("\n".join(removed))
    narrate(
        f"kopipasta: removed {len(removed)} session(s)"
        + (f", handed back {len(released)} provider cache(s)" if released else "")
        + ("; `current` now points at nothing" if current_cleared else "")
    )
    return EXIT_OK


def _release(lease: Dict[str, Any]) -> bool:
    """Best effort. A cache we could not hand back has a TTL; a failed
    delete must not stop a session from being removed."""
    from kopipasta.core.backend import release_lease

    try:
        return release_lease(lease)
    except Exception:  # noqa: BLE001 - reported by the caller counting releases
        return False


# --------------------------------------------------------------------------
# reap — the rented caches nothing is using any more
# --------------------------------------------------------------------------
def _reap(root: str, args: argparse.Namespace) -> int:
    """Hand back caches this project rented and no live session is holding.

    A non-zero count is not evidence of a bug. A named session leaves its
    cache rented on purpose so turn 2 can reuse it, and this is what makes
    sweeping safe: those are named in `keep` and are left alone.

    **Scoped to this project, and there is no flag to widen it.** A lease
    lives in the project that took it, so a machine-wide sweep can read this
    project's leases and no others — it would delete a cache another repo is
    holding mid-conversation, which is the exact money bug this function was
    written to prevent, one scope up. The asymmetry decides it: an abandoned
    cache costs storage rent bounded by a TTL of at most an hour, while a
    destroyed live one costs a full re-creation on every following turn
    (16,329 tokens, measured). Recovering from a crash needs no wider scope
    anyway — the leases are still on disk, so `reap` inside that project is
    already correct.
    """
    from kopipasta.core.backend import CACHE_PREFIX, GeminiBackend, _cache_label

    leases = live_leases(root)
    label = get_project_key(root)
    wanted = f"{CACHE_PREFIX}{_cache_label(label)}-"

    items = GeminiBackend.list_caches()
    mine = [it for it in items if str(it.get("displayName", "")).startswith(wanted)]
    held = [it for it in mine if str(it.get("name", "")) in leases]
    candidates = [it for it in mine if str(it.get("name", "")) not in leases]

    reaped = 0
    if not args.dry_run:
        reaped = GeminiBackend.reap_orphans(keep=list(leases), label=label)

    payload = {
        "ok": True,
        "dry_run": args.dry_run,
        "scope": "this project",
        "reaped": reaped,
        "would_reap": len(candidates),
        "held_by_sessions": [
            {
                "session": leases[str(it["name"])]["session"],
                "expires_in_s": leases[str(it["name"])]["expires_in_s"],
            }
            for it in held
        ],
        "other_projects": len(items) - len(mine),
    }
    if args.json:
        emit_json(payload)
        return EXIT_OK

    emit(str(reaped if not args.dry_run else len(candidates)))
    narrate(
        f"kopipasta: {'would hand back' if args.dry_run else 'handed back'} "
        f"{len(candidates) if args.dry_run else reaped} cache(s); "
        f"{len(held)} still leased by a session; "
        f"{payload['other_projects']} belong to other projects."
    )
    if held:
        for it in held:
            lease = leases[str(it["name"])]
            narrate(
                f"  kept: session {lease['session']}, {lease['expires_in_s']:.0f}s left"
            )
    return EXIT_OK


__all__ = ["build_parser", "run"]
