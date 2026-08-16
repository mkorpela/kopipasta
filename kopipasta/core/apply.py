"""`kopipasta apply` — put a model's patch on disk, safely — spec §11.

Deliberately a separate verb from `ask`. `ask` is a read-only context oracle:
it reports how many patches came back and saves the artifact, and stops there.
Mixing the two would mean the command that spends money on a question is also
the command that edits the worktree, and a caller retrying a timeout would
re-apply a patch it had already applied.

The safety model is one idea: **a clean worktree is the undo**. Everything
else — the editable zone, the hunk accounting, `--verify`, `--revert-on-fail`
— narrows the window in which that undo is needed, but the reason a 400-line
one-shot patch is safe to try at all is that `git diff` shows exactly what it
did and `git checkout .` puts it back.

Two things this had to be built on before it could be honest, both found by
dogfooding `ask` at this repo:

- `apply_patches` returned a bare list of modified files, and a file where one
  of three hunks matched was written to disk and reported as a success. Exit 4
  ("partially applied") could not be produced, so a half-patched file exited 0.
- The `current` pointer was only written when `--json` was off, so the agent
  workflow in spec §1 — `ask --json` then `apply current` — had no handle to
  the artifact it had just made.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from kopipasta.cache import find_project_root
from kopipasta.core.errors import (
    EXIT_OK,
    EXIT_PATCH_FAILED,
    EXIT_PATCH_PARTIAL,
    EXIT_VERIFY,
    KopipastaError,
    UsageError,
)
from kopipasta.core.session import SESSIONS_DIR, Session
from kopipasta.interaction import NoHumanAttached
from kopipasta.output import (
    HelpToStdoutParser,
    emit,
    emit_json,
    narrate,
    stdout_reserved_for_output,
)
from kopipasta.patcher import apply_patches, normalise_path, parse_llm_output


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------
class DirtyWorktree(KopipastaError):
    """A file this patch would write already has uncommitted changes.

    The undo is `git checkout` of the patched files, so applying over
    uncommitted work in one of them puts that work one failed `--verify` away
    from being destroyed. Only the files the patch targets can be harmed that
    way; the rest of the worktree is none of this guard's business.

    Not retryable: nothing about running it again changes the answer. The
    caller has to commit, stash, or accept the risk with --dirty-ok.
    """

    slug = "dirty_worktree"
    retryable = False

    def __init__(self, files: Sequence[str]) -> None:
        shown = ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
        super().__init__(
            f"{len(files)} file(s) this patch would write have uncommitted changes.",
            detail=f"Modified: {shown}",
            hint="git stash            # then re-run\n"
            "kopipasta apply … --dirty-ok   # apply anyway, with no clean undo",
            files=list(files),
        )


class NothingToApply(KopipastaError):
    slug = "no_patches"
    retryable = False


class PatchFailed(KopipastaError):
    """Spec §8: 5 when the worktree is untouched, 4 when it is not.

    The distinction is the whole point — 5 means "retry is safe", 4 means "you
    have a mess to clean up" — so the exit code is carried on the instance
    rather than decided by whoever catches it.
    """

    slug = "patch_failed"
    retryable = False

    def __init__(self, exit_code: int, summary: str, **fields: Any) -> None:
        super().__init__(summary, **fields)
        self.exit_code = exit_code


class VerifyFailed(KopipastaError):
    slug = "verify_failed"
    retryable = False
    exit_code = EXIT_VERIFY


# --------------------------------------------------------------------------
# git, only as much as we need
# --------------------------------------------------------------------------
def _git(root: str, *args: str, timeout: float = 60.0) -> Tuple[int, str, str]:
    """Run git, or report it as absent. Never raises for a missing binary.

    Bounded on purpose (spec §12): a git subprocess that hangs is the same
    unbounded stall as a prompt with nobody to answer it, just wearing a
    different costume.
    """
    exe = shutil.which("git")
    if not exe:
        return 127, "", "git not found on PATH"
    try:
        p = subprocess.run(
            [exe, *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out after {timeout}s"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def dirty_files(root: str) -> Optional[Set[str]]:
    """Paths with uncommitted changes, or None when this is not a git repo.

    None and empty-set are different answers and must not be collapsed: no
    repo means there is no undo to protect, which is a thing to say out loud
    rather than a reason to claim the worktree is clean.
    """
    code, out, _ = _git(root, "status", "--porcelain")
    if code != 0:
        return None
    files = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        # "XY path" and "XY old -> new" for renames; we want the live path.
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            files.add(path.strip('"'))
    return files


# --------------------------------------------------------------------------
# where the patch comes from
# --------------------------------------------------------------------------
def _latest_response(root: str, session_id: str) -> str:
    directory = os.path.join(root, SESSIONS_DIR, session_id)
    try:
        responses = sorted(
            f for f in os.listdir(directory) if f.endswith("-response.md")
        )
    except OSError:
        raise UsageError(
            f"session {session_id!r} has no directory under {SESSIONS_DIR}/.",
            hint="kopipasta ask -q '...'      # start one",
        ) from None
    if not responses:
        raise UsageError(
            f"session {session_id!r} has no response to apply yet.",
            hint="The session exists but no turn completed.",
        )
    return os.path.join(directory, responses[-1])


def resolve_target(
    root: str, target: str, session_flag: Optional[str]
) -> Tuple[str, Optional[str]]:
    """(text, session_id). `session_id` is None for a file or stdin.

    Knowing which session the patch came from is what lets the editable zone
    be enforced at all — the roles live in that session's selection record.
    """
    if session_flag:
        return _read(_latest_response(root, session_flag)), session_flag
    if target == "-":
        return sys.stdin.read(), None
    if target == "current":
        session_id = Session.read_current(root)
        if not session_id:
            raise UsageError(
                "there is no 'current' session to apply.",
                detail=f"No conversation is recorded in {SESSIONS_DIR}/.",
                hint="kopipasta ask --mode patch -q '...'   # produce one\n"
                "kopipasta apply <file>                # or apply a file",
            )
        return _read(_latest_response(root, session_id)), session_id
    if os.path.isdir(target):
        raise UsageError(f"{target!r} is a directory, not a patch.")
    if not os.path.exists(target):
        raise UsageError(
            f"no such file: {target}",
            hint="kopipasta apply current   # the last session's answer\n"
            "kopipasta apply -         # read the patch from stdin",
        )
    return _read(target), None


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def editable_set(root: str, session_id: str) -> Optional[Set[str]]:
    """The Active Workspace of the session's latest turn (spec §11).

    `None` means *no record to enforce against* — no selection file, or one we
    cannot read. An **empty set** is the opposite answer: the record exists and
    says nothing was editable, which is exactly what a triage session (all `-r`
    and `-m`, no `-e`) looks like, and every modification should be refused.

    Collapsing the two with `editable or None` turned the strictest case into
    the most permissive one. Found by dogfooding this file at the oracle.
    """
    path = os.path.join(root, SESSIONS_DIR, session_id, "selection.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    latest = max(data, key=lambda k: int(k) if str(k).isdigit() else -1)
    files = (data.get(latest) or {}).get("files") or {}
    return {
        rel
        for rel, rec in files.items()
        if isinstance(rec, dict) and rec.get("role") == "edit"
    }


# --------------------------------------------------------------------------
# verification and rollback
# --------------------------------------------------------------------------
def run_verify(root: str, command: str, timeout: float) -> Tuple[int, str]:
    """Run the verify command, bounded. Its output is narration, not artifact."""
    try:
        p = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"--verify timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    return p.returncode, "\n".join(tail[-20:])


def revert(
    root: str, result, was_dirty: Optional[Set[str]]
) -> Tuple[List[str], List[str]]:
    """Undo what we just did. Returns (reverted, declined).

    Only files this run touched, and only those that were clean beforehand.
    A file the caller had already modified is theirs, not ours: reverting it
    under --dirty-ok would destroy uncommitted work to tidy up after a failed
    test run, which is a far worse outcome than leaving the patch in place.
    """
    reverted: List[str] = []
    declined: List[str] = []
    # Both sides normalised: git says `app.py`, the model may have written
    # `./app.py`, and a raw comparison answers "was this already dirty?" with
    # a confident no — then reverts the caller's uncommitted work.
    already_dirty = {normalise_path(p) for p in (was_dirty or set())}
    for outcome in result.outcomes:
        if not outcome.wrote:
            continue
        path = outcome.path
        if normalise_path(path) in already_dirty:
            declined.append(path)
            continue
        if outcome.action == "created":
            try:
                os.remove(os.path.join(root, path))
                reverted.append(path)
            except OSError:
                declined.append(path)
            continue
        code, _, _ = _git(root, "checkout", "--", path)
        (reverted if code == 0 else declined).append(path)
    return reverted, declined


# --------------------------------------------------------------------------
# the verb
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = HelpToStdoutParser(
        prog="kopipasta apply",
        description="Apply a model's patch to the worktree, with a way back out.",
    )
    p.add_argument(
        "target",
        nargs="?",
        default="current",
        metavar="TARGET",
        help="a patch file, '-' for stdin, or 'current' (default): "
        "the latest session's response",
    )
    p.add_argument(
        "--session", metavar="ID", help="apply that session's latest response"
    )

    s = p.add_argument_group("safety")
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be applied and touch nothing",
    )
    s.add_argument(
        "--dirty-ok",
        action="store_true",
        help="apply even though the worktree has uncommitted changes",
    )
    s.add_argument(
        "--allow-delete", action="store_true", help="permit patches that delete files"
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="overwrite even when the shrink/hallucination guard fires",
    )
    s.add_argument(
        "--any-file",
        action="store_true",
        help="do not restrict edits to the session's editable set",
    )

    v = p.add_argument_group("verification")
    v.add_argument(
        "--verify", metavar="CMD", help="run this after applying; exit 7 if it fails"
    )
    v.add_argument(
        "--revert-on-fail",
        action="store_true",
        help="restore the files we touched when --verify fails",
    )
    v.add_argument(
        "--verify-timeout",
        type=float,
        default=1800.0,
        metavar="SECONDS",
        help="cap the verify command (default: 1800)",
    )

    p.add_argument(
        "--json",
        action="store_true",
        help="stdout becomes a single JSON object (spec §8)",
    )
    return p


def run(argv: Sequence[str]) -> int:
    """Parse, apply, report. Returns the exit code; never raises."""
    with stdout_reserved_for_output():
        return _run_parsed(argv)


def _run_parsed(argv: Sequence[str]) -> int:
    from kopipasta.core.ask import report_failure
    from kopipasta.core.errors import InteractionRequired

    argv = list(argv)
    try:
        args = build_parser().parse_args(argv)
    except KopipastaError as exc:
        return report_failure(exc, json_mode="--json" in argv)
    try:
        return _apply(args)
    except KopipastaError as exc:
        return report_failure(exc, json_mode=args.json)
    except NoHumanAttached as exc:
        return report_failure(InteractionRequired(str(exc)), json_mode=args.json)


def _apply(args: argparse.Namespace) -> int:
    root = str(find_project_root())

    if args.revert_on_fail and not args.verify:
        raise UsageError(
            "--revert-on-fail has nothing to react to without --verify.",
            hint="kopipasta apply current --verify 'pytest -q' --revert-on-fail",
        )

    text, session_id = resolve_target(root, args.target, args.session)
    patches = parse_llm_output(text)
    if not patches:
        raise NothingToApply(
            "no patches found in the response.",
            detail="Nothing in it parsed as a file block, a diff or a "
            "search/replace pair.",
            hint="Check the artifact, or re-ask with --mode patch.",
        )

    # 1. The undo, before anything is touched.
    #
    #    Scoped to the files this patch will write, because that is the whole
    #    extent of the undo: reverting is `git checkout` of those paths, and a
    #    file the patch never touches cannot be harmed by it. Refusing over
    #    unrelated dirt cost a run and protected nothing — most visibly on the
    #    documented two-step, where `ask` appends `.kopipasta/` to .gitignore
    #    on first use and `apply` then refused because the worktree was dirty.
    #    The tool dirtied the tree and blocked itself, on a clean first run.
    was_dirty = dirty_files(root)
    targets = {normalise_path(p["file_path"]) for p in patches}
    blocking = sorted(p for p in (was_dirty or ()) if normalise_path(p) in targets)
    bystanders = sorted(
        p for p in (was_dirty or ()) if normalise_path(p) not in targets
    )
    if was_dirty is None:
        narrate("kopipasta: not a git repository — there is no undo for this.")
    elif blocking and not (args.dirty_ok or args.dry_run):
        raise DirtyWorktree(blocking)
    if bystanders:
        # Not blocking is not the same as not mentioning. `revert` still
        # declines to touch any of these, so uncommitted work stays safe
        # whichever way the run ends.
        narrate(
            f"kopipasta: {len(bystanders)} uncommitted change(s) not touched by this "
            "patch, and left alone: " + ", ".join(bystanders[:8])
        )

    # 2. The editable zone, when we know it.
    zone: Optional[Set[str]] = None
    if session_id and not args.any_file:
        zone = editable_set(root, session_id)
        if zone is None:
            narrate(
                f"kopipasta: session {session_id} records no editable files; "
                "applying without a zone restriction."
            )
        else:
            # A file the model invented is a creation, not a rewrite of
            # something it was only shown for reference. Refusing those would
            # make "add a new module" impossible; the guard exists to stop an
            # -r file being edited, and that file exists by definition.
            zone = zone | {
                p["file_path"]
                for p in patches
                if not os.path.exists(os.path.join(root, p["file_path"]))
            }

    cwd = os.getcwd()
    os.chdir(root)
    try:
        result = apply_patches(
            patches,
            allow_delete=args.allow_delete,
            force=args.force,
            dry_run=args.dry_run,
            allowed_files=zone,
        )
    finally:
        os.chdir(cwd)

    payload: Dict[str, Any] = {
        "ok": result.ok,
        "dry_run": args.dry_run,
        "target": args.target if not args.session else f"session:{args.session}",
        "session": session_id,
        "patches": len(patches),
        "applied": result.applied,
        "partial": result.partial,
        "failed": result.failed,
        "skipped": result.skipped,
        "changed": result.changed,
        "hunks": {
            o.path: {"applied": o.hunks_applied, "total": o.hunks_total}
            for o in result.outcomes
            if o.hunks_total
        },
    }

    # 3. Did it land? Spec §8: 5 is "worktree untouched, retry is safe",
    #    4 is "you have a mess to clean up". Never the same number.
    if not result.ok and not result.changed:
        raise PatchFailed(
            EXIT_PATCH_FAILED,
            f"no patch applied; the worktree is untouched ({len(patches)} attempted).",
            detail=_explain(result),
            hint="The response may not match the files on disk any more.",
            **payload,
        )

    # 4. Verification, and the way back out.
    if args.verify and not args.dry_run:
        code, output = run_verify(root, args.verify, args.verify_timeout)
        payload["verify"] = {"command": args.verify, "exit": code, "output": output}
        if code != 0:
            if args.revert_on_fail:
                reverted, declined = revert(root, result, was_dirty)
                payload["reverted"] = reverted
                payload["revert_declined"] = declined
            raise VerifyFailed(
                f"--verify failed (exit {code}).",
                detail=output,
                hint="The patch is still applied; `git diff` shows it."
                if not args.revert_on_fail
                else "The files this run touched have been restored.",
                **payload,
            )

    if not result.ok:
        raise PatchFailed(
            EXIT_PATCH_PARTIAL,
            "the patch applied only in part; the worktree is dirty.",
            detail=_explain(result),
            hint="git diff        # review what landed\ngit checkout .  # put it back",
            **payload,
        )

    _emit(args, payload, result)
    return EXIT_OK


def _explain(result) -> str:
    lines = []
    for o in result.outcomes:
        if o.status == "applied":
            continue
        note = f" ({o.reason})" if o.reason else ""
        lines.append(f"  {o.status:<8} {o.path}{note}")
    return "\n".join(lines)


def _emit(args: argparse.Namespace, payload: Dict[str, Any], result) -> None:
    if args.json:
        emit_json(payload)
        return
    verb = "would apply" if args.dry_run else "applied"
    lines = [f"kopipasta: {verb} {len(payload['applied'])} file(s)"]
    for o in result.outcomes:
        mark = "+" if o.action == "created" else ("-" if o.action == "deleted" else "~")
        hunks = f"  ({o.hunks_applied}/{o.hunks_total} hunks)" if o.hunks_total else ""
        lines.append(f"  {mark} {o.path}{hunks}")
    emit("\n".join(lines))
