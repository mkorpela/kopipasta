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
from kopipasta.patcher import (
    apply_patches,
    normalise_path,
    parse_llm_output,
    skipped_file_paths,
)
from kopipasta.proc import TEXT


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
            timeout=timeout,
            **TEXT,
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
    with open(path, encoding="utf-8", errors="replace") as fh:
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
        with open(path, encoding="utf-8") as fh:
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
def run_command(root: str, command: str, timeout: float, label: str) -> Tuple[int, str]:
    """Run a shell command, bounded, and keep its output whatever it contains.

    The decoding is spelled out rather than inherited (see `TEXT`): this is the
    call that reads another tool's console output, so it is the one guaranteed
    to meet a box-drawing rule sooner or later.
    """
    try:
        p = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            timeout=timeout,
            **TEXT,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{label} timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    return p.returncode, "\n".join(tail[-20:])


def run_verify(root: str, command: str, timeout: float) -> Tuple[int, str]:
    """Run the verify command, bounded. Its output is narration, not artifact."""
    return run_command(root, command, timeout, "--verify")


#: Where the applied paths are spliced into `--format-cmd`.
FILES_TOKEN = "{files}"

#: Characters `cmd.exe` acts on *after* it has stripped the quotes around
#: them, or expands regardless of quoting. There is no escape that survives
#: both passes reliably, which is why these are refused rather than quoted.
CMD_METACHARACTERS = set('&|<>^"%!\r\n')


class UnsafePath(ValueError):
    """A path that cannot be handed to a shell safely on this platform."""


def _quote(paths: Sequence[str]) -> str:
    """The applied paths as one shell word list, quoted for *this* shell.

    The paths come from the model's response, not from the caller, so this is
    the one place in `apply` where untrusted text meets `shell=True`. On POSIX
    `shlex.quote` is exactly right. On Windows nothing is: `cmd.exe` parses
    the line twice, expanding `%VAR%` and honouring `&` in text it has already
    unquoted, and every published escaping recipe has a counterexample. So a
    path carrying one of those characters is refused. Refusing to format is a
    cost of nothing; getting this wrong is arbitrary command execution in the
    tool whose entire pitch is that it is the safe way to apply a patch.
    """
    if os.name != "nt":
        import shlex

        return " ".join(shlex.quote(p) for p in paths)
    bad = sorted(p for p in paths if CMD_METACHARACTERS & set(p))
    if bad:
        raise UnsafePath(", ".join(bad))
    return subprocess.list2cmdline(list(paths))


def run_format(
    root: str, command: str, files: Sequence[str], timeout: float
) -> Tuple[int, str]:
    """Normalise what was just written, before anything judges it.

    Field report 2.3: every patch turn against a prettier-gated repo failed
    `prettier --check` on whitespace the model had no way to get right, so
    turn 1 was a guaranteed red and the correction rounds were spent on
    formatting rather than on the bug. The reported workaround folded
    `prettier --write` into `--verify`, which works and is the wrong shape —
    a verifier that mutates the tree can no longer be trusted to describe it.

    `{files}` is what makes this a flag rather than a documented wrap: it
    scopes the formatter to the files this run wrote. `prettier --write .`
    after a patch reformats the caller's unrelated uncommitted work, and the
    revert path only knows about our own files, so nothing would ever put the
    rest back.
    """
    if FILES_TOKEN in command:
        try:
            command = command.replace(FILES_TOKEN, _quote(files))
        except UnsafePath as exc:
            return 126, (
                "--format-cmd refused: cmd.exe cannot be given these paths "
                f"safely ({exc}). The patch is applied and unformatted; "
                "format them by hand or rename the files."
            )
    return run_command(root, command, timeout, "--format-cmd")


#: Why a file this run wrote was left as it is. The distinction matters to
#: the caller: the first is a deliberate refusal protecting their work, the
#: other two are the undo failing to do its job.
WAS_ALREADY_DIRTY = "had uncommitted changes before this run"
GIT_REFUSED = "git could not restore it"
COULD_NOT_REMOVE = "the file it created could not be removed"


def revert(
    root: str, result, was_dirty: Optional[Set[str]]
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Undo what we just did. Returns (reverted, [(path, why_not), ...]).

    Only files this run touched, and only those that were clean beforehand.
    A file the caller had already modified is theirs, not ours: reverting it
    under --dirty-ok would destroy uncommitted work to tidy up after a failed
    test run, which is a far worse outcome than leaving the patch in place.

    Each decline carries its reason, because the caller's next move depends on
    which one it was and a bare list of paths cannot tell them apart.
    """
    reverted: List[str] = []
    declined: List[Tuple[str, str]] = []
    # Both sides normalised: git says `app.py`, the model may have written
    # `./app.py`, and a raw comparison answers "was this already dirty?" with
    # a confident no — then reverts the caller's uncommitted work.
    already_dirty = {normalise_path(p) for p in (was_dirty or set())}
    for outcome in result.outcomes:
        if not outcome.wrote:
            continue
        path = outcome.path
        if normalise_path(path) in already_dirty:
            declined.append((path, WAS_ALREADY_DIRTY))
            continue
        if outcome.action == "created":
            try:
                os.remove(os.path.join(root, path))
                reverted.append(path)
            except OSError:
                declined.append((path, COULD_NOT_REMOVE))
            continue
        code, _, _ = _git(root, "checkout", "--", path)
        if code == 0:
            reverted.append(path)
        else:
            declined.append((path, GIT_REFUSED))
    return reverted, declined


def revert_hint(
    reverted: Sequence[str], declined: Sequence[Tuple[str, str]], asked: bool
) -> str:
    """What the tree actually looks like now, in one sentence per outcome.

    This used to be a constant chosen by whether `--revert-on-fail` was
    *passed*, which is a statement about the command line and not about the
    worktree. On the one run where reverting was declined it printed "The
    files this run touched have been restored." beside `"reverted": []` —
    and anyone reading the console rather than parsing the JSON took their
    next action against a state they had mismodelled. A tool that reports a
    restoration it did not perform is the single failure mode that running it
    again cannot catch, so the hint is now derived from the outcome.
    """
    if not asked:
        return "The patch is still applied; `git diff` shows it."
    parts: List[str] = []
    if reverted:
        parts.append(
            f"Restored {len(reverted)} file(s) this run touched: "
            + ", ".join(reverted[:8])
            + "."
        )
    for reason in (WAS_ALREADY_DIRTY, GIT_REFUSED, COULD_NOT_REMOVE):
        paths = [p for p, why in declined if why == reason]
        if not paths:
            continue
        parts.append(
            f"Revert declined: {len(paths)} file(s) {reason} "
            f"({', '.join(paths[:8])}); they are untouched and the patch is "
            "still applied to them."
        )
    if not parts:
        # --revert-on-fail with nothing written: the verify failed over
        # something this run did not cause.
        return "Nothing to revert: this run wrote no files."
    return " ".join(parts)


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
        "--format-cmd",
        metavar="CMD",
        help="run this on the applied files before --verify; {files} is "
        "replaced with them (e.g. 'npx prettier --write {files}')",
    )
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

    # A response can declare two files and have one dropped by the parser,
    # most often for want of a ``` fence. `apply` is the verb that writes, so
    # reporting only what parsed is worse here than in `ask`: the caller acts
    # on a half-applied change believing it whole.
    dropped = skipped_file_paths(text, patches)
    if dropped:
        narrate(
            f"kopipasta: {len(dropped)} file(s) the response named are not in "
            f"this patch: {', '.join(dropped)}. Measured causes vary — an "
            "indented block and a header with no body both parse to nothing — "
            "so this reports the fact, not a diagnosis."
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
        # Spelled the same way `ask` spells it, and for the same reason: a
        # bare `patches` count is read as "patches applied" by anyone who did
        # not write the tool, and the two verbs' envelopes should be
        # comparable without knowing which one produced them.
        "patches_proposed": len(patches),
        "patches_applied": len(result.applied),
        **({"patches_skipped": dropped} if dropped else {}),
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

    # 4. Normalise before judging. A formatter is not a gate: its failure is
    #    reported and the run continues, because exit 7 has to keep meaning
    #    "--verify failed" or the caller cannot tell which command to go and
    #    look at.
    if args.format_cmd and not args.dry_run and result.applied:
        code, output = run_format(
            root, args.format_cmd, result.applied, args.verify_timeout
        )
        payload["format"] = {
            "command": args.format_cmd,
            "files": list(result.applied),
            "exit": code,
            "output": output,
        }
        if code != 0:
            narrate(
                f"kopipasta: --format-cmd exited {code}; the patch is applied "
                "and unformatted. " + (output.splitlines() or [""])[-1]
            )

    # 5. Verification, and the way back out.
    if args.verify and not args.dry_run:
        code, output = run_verify(root, args.verify, args.verify_timeout)
        payload["verify"] = {"command": args.verify, "exit": code, "output": output}
        if code != 0:
            declined: List[Tuple[str, str]] = []
            reverted: List[str] = []
            if args.revert_on_fail:
                reverted, declined = revert(root, result, was_dirty)
                payload["reverted"] = reverted
                payload["revert_declined"] = [p for p, _ in declined]
                # The reason, not just the path: "I refused to touch your
                # uncommitted work" and "git would not put it back" call for
                # opposite next moves.
                payload["revert_declined_why"] = {p: why for p, why in declined}
            raise VerifyFailed(
                f"--verify failed (exit {code}).",
                detail=output,
                hint=revert_hint(reverted, declined, args.revert_on_fail),
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
