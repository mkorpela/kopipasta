"""The conversation, on disk, in the repo — spec §7.

State lives in `.kopipasta/sessions/<id>/`, not in the clipboard and not in
process memory, because the caller's cwd *is* the repo: every path in an
answer is repo-relative, and an agent can read and grep these artifacts with
the tools it already has. `rm -rf .kopipasta` is a complete reset.

Two decisions worth stating, because both are load-bearing:

**The prefix is fixed for the life of a session.** Turn 1 renders the repo
payload and it is stored verbatim; every later turn sends those exact bytes
again. That is what a provider-side prefix cache keys on — re-rendering
"the same" selection would produce a different string the moment a file
changed on disk, and the cache would miss on every turn. Files that are new or
changed since turn 1 therefore ride in the *suffix*, marked as superseding the
copy in the prefix. Turn-level dedup is the same idea from the other side:
unchanged files are not resent, because they are still sitting in the prefix.

**The `current` pointer is a human convenience and nothing else.** It is racy
by construction, so `--json` never follows it: an agent that omits `--session`
gets a fresh session rather than someone else's.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from kopipasta.core.context import Turn
from kopipasta.core.errors import UsageError
from kopipasta.git_utils import add_to_gitignore
from kopipasta.output import narrate
from kopipasta.proc import TEXT

STATE_DIR = ".kopipasta"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
CURRENT = os.path.join(STATE_DIR, "current")
PREFIX_FILE = "prefix.md"
PREFIX_FILES = "prefix-files.json"
TRANSCRIPT = "transcript.jsonl"
SELECTION = "selection.json"
CACHE_FILE = "cache.json"
META = "meta.json"


def new_session_id() -> str:
    """Date first so `ls` sorts usefully; four hex chars so it stays typeable."""
    return f"{time.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:4]}"


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _write_text(path: str, text: str) -> None:
    # utf-8 pinned: these payloads are full of em dashes and box characters,
    # and the Windows default (cp1252) cannot encode either (findings trap 18).
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


#: What a session id may contain. Deliberately a whitelist: the id becomes a
#: directory name, and `session rm` deletes that directory recursively, so the
#: cost of being wrong here is asymmetric. `C:sessions` is not absolute on
#: POSIX and contains no slash, but on Windows it is drive-relative and lands
#: outside the tree entirely — the separator checks below cannot see that, and
#: this does.
_ID_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")


def _check_id(session_id: str) -> None:
    """A session id becomes a directory name, so it must not be a path.

    `--session ../../etc` is not a conversation, and an id that escapes the
    sessions directory would write session artifacts anywhere the process can
    reach — or, once `rm` exists, delete anything the process can reach.
    """
    if (
        not session_id
        or session_id in (".", "..")
        or "/" in session_id
        or "\\" in session_id
        or os.path.isabs(session_id)
        or not _ID_CHARS.match(session_id)
    ):
        raise UsageError(
            f"{session_id!r} is not a usable session id.",
            detail="It becomes a directory under .kopipasta/sessions/, so it cannot be a "
            "path. Letters, digits, dot, dash and underscore only.",
            hint="Use a plain name: --session refactor-auth",
        )


def _git_head(root: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        r = subprocess.run(
            [git, "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            timeout=10,
            **TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


@dataclass
class SentFile:
    rel: str
    role: str
    hash: str
    turn: int


class Session:
    """One conversation. Created on `open`, appended to on every turn."""

    def __init__(self, root: str, session_id: str, *, created: bool) -> None:
        self.root = root
        self.id = session_id
        self.dir = os.path.join(root, SESSIONS_DIR, session_id)
        self.created = created
        self._dir_ready = False
        self._gitignore_checked = False
        self.turn = self._next_turn()

    # -- lifecycle -----------------------------------------------------------
    @classmethod
    def open(
        cls,
        root: str,
        session_id: Optional[str] = None,
        *,
        follow_current: bool = False,
    ) -> "Session":
        """Resume `session_id`, follow the `current` pointer, or start fresh.

        `follow_current` means `--continue` was passed. It is never inferred:
        a racy pointer must not decide which conversation a question lands in,
        and two unrelated questions typed a minute apart used to share one
        context because resumption was implicit whenever --json was off.

        With nothing to continue, this raises rather than quietly starting a
        fresh session — a follow-up question answered with no context reads
        exactly like a successful answer.

        Nothing is written here. A run that fails while resolving its
        selection should not leave an empty conversation behind, so the
        directory appears on the first actual write.
        """
        if session_id:
            _check_id(session_id)
        else:
            if follow_current:
                session_id = cls.read_current(root)
                if not session_id:
                    raise UsageError(
                        "--continue: there is no session to continue.",
                        detail=f"No conversation is recorded in {STATE_DIR}/.",
                        hint="Drop --continue to start one, or name it with --session <id>.",
                    )
            session_id = session_id or new_session_id()
        directory = os.path.join(root, SESSIONS_DIR, session_id)
        return cls(root, session_id, created=not os.path.isdir(directory))

    def _in_worktree(self) -> bool:
        """True when the state directory lives inside a git worktree.

        A git repo has a .git directory or a .git file (worktrees and
        submodules). Writing a .gitignore into a directory with no git would
        litter a non-repo checkout with an artifact nothing will ever read.
        """
        return os.path.exists(os.path.join(self.root, ".git"))

    def _ensure_gitignore(self) -> None:
        """Keep .kopipasta/ out of version control, once per session instance.

        Announced when it happens, because on a first run in a fresh repo it
        is the *only* change the run makes to the tree, and a tracked file
        edited in silence is one `git diff` away from looking like a bug in
        something else.

        Checked independently of directory creation: resuming a session must
        still repair the rule if it was lost, but non-git checkouts must never
        gain a stray .gitignore.
        """
        if self._gitignore_checked:
            return
        self._gitignore_checked = True
        if not self._in_worktree():
            return
        if add_to_gitignore(self.root, f"{STATE_DIR}/"):
            narrate(
                f"kopipasta: added '{STATE_DIR}/' to .gitignore — session "
                "records are bookkeeping, not source."
            )

    def _ensure_dir(self) -> None:
        if not self._dir_ready:
            os.makedirs(self.dir, exist_ok=True)
            self._dir_ready = True
        self._ensure_gitignore()

    @staticmethod
    def read_current(root: str) -> Optional[str]:
        try:
            with open(os.path.join(root, CURRENT), encoding="utf-8") as fh:
                name = fh.read().strip()
        except OSError:
            return None
        if name and os.path.isdir(os.path.join(root, SESSIONS_DIR, name)):
            return name
        return None

    def set_current(self) -> None:
        """Point `current` here. Human convenience; never read under --json."""
        try:
            self._ensure_dir()
            _write_text(os.path.join(self.root, CURRENT), self.id + "\n")
        except OSError:
            pass  # A convenience must never be able to fail a run (trap 23).

    def _next_turn(self) -> int:
        try:
            existing = [f for f in os.listdir(self.dir) if f.endswith("-request.md")]
        except OSError:
            existing = []
        return len(existing) + 1

    # -- paths ---------------------------------------------------------------
    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def turn_path(self, suffix: str, turn: Optional[int] = None) -> str:
        return self.path(f"{turn or self.turn:03d}-{suffix}")

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    # -- the fixed prefix ----------------------------------------------------
    def load_prefix(self) -> Optional[str]:
        try:
            with open(self.path(PREFIX_FILE), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def save_prefix(self, text: str, files: Dict[str, Dict[str, str]]) -> None:
        """Store the prefix and exactly what is inside it.

        The manifest is written with the prefix, not derived from the turn
        records, because those are two different questions. "What did turn 2
        send?" includes its suffix updates; "what will turn 3 still be able to
        see?" is only ever the prefix.
        """
        self._ensure_dir()
        _write_text(self.path(PREFIX_FILE), text)
        _write_json(
            self.path(PREFIX_FILES),
            {"turn": self.turn, "files": files},
        )

    # -- what the model can still see ---------------------------------------
    def prefix_files(self) -> Dict[str, SentFile]:
        """rel -> the copy of that file the reused prefix contains.

        Deliberately NOT "everything ever sent". A file that rode in an
        earlier turn's *suffix* is gone: the suffix is not replayed, so
        treating it as still-present would withhold the file from the model
        while the record claimed it was sent. Anything outside the prefix must
        be resent on every turn that needs it.
        """
        data = _read_json(self.path(PREFIX_FILES), {})
        turn = int(data.get("turn") or 1)
        return {
            rel: SentFile(
                rel=rel, role=rec.get("role", ""), hash=rec.get("hash", ""), turn=turn
            )
            for rel, rec in (data.get("files") or {}).items()
        }

    def record_selection(
        self, files: Dict[str, Dict[str, str]], demoted: List[dict]
    ) -> None:
        self._ensure_dir()
        data = _read_json(self.path(SELECTION), {})
        data[str(self.turn)] = {"files": files, "demoted": demoted}
        _write_json(self.path(SELECTION), data)

    # -- transcript ----------------------------------------------------------
    def history(self, max_turns: int = 6, max_answer_chars: int = 4000) -> List[Turn]:
        """Earlier exchanges, newest last, replayed into the suffix.

        Bounded on both axes: an unbounded replay would grow the varying half
        of the payload without limit, which is the part that is never cached.
        """
        turns: List[Turn] = []
        try:
            with open(self.path(TRANSCRIPT), encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return turns
        for line in lines[-max_turns:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            answer = rec.get("answer") or ""
            if len(answer) > max_answer_chars:
                answer = answer[:max_answer_chars] + "\n... (truncated in replay)"
            if rec.get("question"):
                turns.append(
                    Turn(
                        n=int(rec.get("turn", 0)),
                        question=rec["question"],
                        answer=answer,
                    )
                )
        return turns

    def append_transcript(self, record: Dict[str, Any]) -> None:
        self._ensure_dir()
        with open(self.path(TRANSCRIPT), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    # -- writing a turn ------------------------------------------------------
    def write_request(self, prefix: str, suffix: str, *, prefix_reused: bool) -> str:
        """The exact payload sent, or a pointer to it plus what varied.

        Repeating a 400k-token prefix into every turn's record would be an
        honest but expensive way to say "unchanged", so a reused prefix is
        recorded as a reference to the file that holds it, byte count and all.
        """
        self._ensure_dir()
        path = self.turn_path("request.md")
        if prefix_reused:
            header = (
                f"<!-- prefix: {PREFIX_FILE} ({len(prefix):,} chars), reused verbatim "
                f"from turn 1 and not repeated here -->\n\n"
            )
            _write_text(path, header + suffix)
        else:
            _write_text(path, f"{prefix}\n{suffix}")
        return path

    def write_response(self, text: str) -> str:
        self._ensure_dir()
        path = self.turn_path("response.md")
        _write_text(path, text)
        return path

    def write_turn_meta(self, meta: Dict[str, Any]) -> str:
        self._ensure_dir()
        path = self.turn_path("meta.json")
        _write_json(path, meta)
        return path

    def update_meta(self, **fields: Any) -> None:
        """Session-level totals. Read-modify-write; single writer per session."""
        self._ensure_dir()
        meta = _read_json(self.path(META), {})
        if not meta:
            meta = {
                "id": self.id,
                "project_root": self.root,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "git_head": _git_head(self.root),
                "turns": 0,
                "totals": {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0},
            }
        usage = fields.pop("usage", None)
        if usage:
            totals = meta.setdefault(
                "totals", {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0}
            )
            for key in ("input", "cached", "output"):
                totals[key] = totals.get(key, 0) + int(usage.get(key) or 0)
            totals["cost_usd"] = round(
                totals.get("cost_usd", 0.0) + float(usage.get("cost_usd") or 0.0), 6
            )
        meta["turns"] = self.turn
        meta["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta.update(fields)
        _write_json(self.path(META), meta)

    # -- the provider-side cache handle -------------------------------------
    #
    # Gemini's cachedContents is rented, not free: billed per token-hour until
    # its TTL expires. Persisting the handle here is what lets turn 2 of a
    # session — a *different process* — read a cache turn 1 paid to create.
    # The rent is bounded by the TTL, which is why the handle carries its own
    # expiry and why nothing here is trusted without the provider agreeing.
    def load_cache_handle(
        self, provider: str, model: str, digest: str
    ) -> Optional[Dict[str, Any]]:
        rec = _read_json(self.path(CACHE_FILE), None)
        if not isinstance(rec, dict):
            return None
        if rec.get("provider") != provider or rec.get("model") != model:
            return None
        if rec.get("digest") != digest:
            return None
        if float(rec.get("expires_at") or 0) <= time.time():
            # Expired on the wall clock. The provider is still the authority —
            # a suspended process makes any local deadline a lie — so this is
            # an optimisation, and the 403 retry in the backend is the guard.
            return None
        return rec

    def save_cache_handle(
        self,
        *,
        provider: str,
        model: str,
        digest: str,
        name: str,
        ttl_s: int,
        tokens: int,
        expires_at: Optional[float] = None,
    ) -> None:
        """Record the handle. `expires_at` is the provider's lease, not ours.

        A turn that *reused* an existing cache must pass the original expiry
        through. Recomputing `now + ttl` on every save renews a lease only the
        provider can renew: the record would claim the cache is good long
        after the server evicted it, and each later turn would spend a round
        trip discovering the 403 before rebuilding.
        """
        self._ensure_dir()
        _write_json(
            self.path(CACHE_FILE),
            {
                "provider": provider,
                "model": model,
                "digest": digest,
                "name": name,
                "tokens": tokens,
                "ttl_s": ttl_s,
                "expires_at": time.time() + ttl_s if expires_at is None else expires_at,
            },
        )

    def clear_cache_handle(self) -> None:
        try:
            os.remove(self.path(CACHE_FILE))
        except OSError:
            pass


# --------------------------------------------------------------------------
# reading the record from outside a run — what `kopipasta session` reports on
#
# These are here rather than in the verb because they are knowledge of the
# on-disk layout, and a second module that knows where `meta.json` lives is a
# second module to fix when it moves.
# --------------------------------------------------------------------------
def session_dir(root: str, session_id: str) -> str:
    _check_id(session_id)
    return os.path.join(root, SESSIONS_DIR, session_id)


def list_sessions(root: str) -> List[str]:
    """Every session id on disk, oldest first — ids start with the date."""
    try:
        names = os.listdir(os.path.join(root, SESSIONS_DIR))
    except OSError:
        return []
    return sorted(
        n for n in names if os.path.isdir(os.path.join(root, SESSIONS_DIR, n))
    )


def read_meta(root: str, session_id: str) -> Dict[str, Any]:
    meta = _read_json(os.path.join(session_dir(root, session_id), META), {})
    # The file is whatever is on disk. A hand-edited `meta.json` holding a
    # list would otherwise be handed back under a Dict annotation and fail
    # somewhere far from here.
    return meta if isinstance(meta, dict) else {}


def read_turns(root: str, session_id: str) -> List[Dict[str, Any]]:
    """The transcript, oldest first. Malformed lines are skipped, not fatal."""
    out: List[Dict[str, Any]] = []
    try:
        with open(
            os.path.join(session_dir(root, session_id), TRANSCRIPT),
            encoding="utf-8",
        ) as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_selection(root: str, session_id: str) -> Tuple[int, Dict[str, Dict[str, str]]]:
    """(turn, files) for the latest recorded turn. `(0, {})` when there is none.

    The *latest* turn, because that is the context the next thing to touch this
    session inherits — the same record `apply` reads to enforce the editable
    zone.
    """
    data = _read_json(os.path.join(session_dir(root, session_id), SELECTION), {})
    if not isinstance(data, dict) or not data:
        return 0, {}
    latest = max(data, key=lambda k: int(k) if str(k).isdigit() else -1)
    files = (data.get(latest) or {}).get("files") or {}
    turn = int(latest) if str(latest).isdigit() else 0
    return turn, {k: v for k, v in files.items() if isinstance(v, dict)}


def read_lease(root: str, session_id: str) -> Optional[Dict[str, Any]]:
    """The provider-side cache this session is renting, if any.

    Returned whether or not it has expired, with `expired` and `expires_in_s`
    filled in — because the two callers want opposite things. A sweep must not
    delete a live one; a report must not claim an expired one is still costing
    money.
    """
    rec = _read_json(os.path.join(session_dir(root, session_id), CACHE_FILE), None)
    if not isinstance(rec, dict) or not rec.get("name"):
        return None
    left = float(rec.get("expires_at") or 0) - time.time()
    return {
        **rec,
        "session": session_id,
        "expired": left <= 0,
        "expires_in_s": round(left, 1),
    }


def live_leases(root: str) -> Dict[str, Dict[str, Any]]:
    """Provider cache name -> the session record renting it, unexpired only.

    This is what stops a sweep from deleting a cache a session is still paying
    to hold. A named session deliberately leaves its cache rented so turn 2 can
    reuse it, so "a cache exists that no process is using" is the *expected*
    state, not evidence of a leak.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for session_id in list_sessions(root):
        lease = read_lease(root, session_id)
        if lease and not lease["expired"]:
            out[str(lease["name"])] = lease
    return out


def clear_current(root: str) -> None:
    try:
        os.remove(os.path.join(root, CURRENT))
    except OSError:
        pass


def remove_session(root: str, session_id: str) -> bool:
    """Delete one session directory. Returns False when there was none.

    The id is validated first: this is the one operation in the package that
    deletes a tree, and `rm ../..` must not be a thing anyone can type.
    """
    directory = session_dir(root, session_id)
    if not os.path.isdir(directory):
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not os.path.isdir(directory)
