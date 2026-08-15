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
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from kopipasta.core.context import Turn
from kopipasta.core.errors import UsageError
from kopipasta.git_utils import add_to_gitignore

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
        with open(path, "r", encoding="utf-8") as fh:
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


def _check_id(session_id: str) -> None:
    """A session id becomes a directory name, so it must not be a path.

    `--session ../../etc` is not a conversation, and an id that escapes the
    sessions directory would write session artifacts anywhere the process can
    reach.
    """
    if (
        not session_id
        or session_id in (".", "..")
        or "/" in session_id
        or "\\" in session_id
        or os.path.isabs(session_id)
    ):
        raise UsageError(
            f"{session_id!r} is not a usable session id.",
            detail="It becomes a directory under .kopipasta/sessions/, so it cannot be a path.",
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
            text=True,
            timeout=10,
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
        self._dir_ready = os.path.isdir(self.dir)
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

        `follow_current` is only ever true on the human path (see the module
        docstring): a racy pointer must not decide which conversation an
        agent's question lands in.

        Nothing is written here. A run that fails while resolving its
        selection should not leave an empty conversation behind, so the
        directory appears on the first actual write.
        """
        if session_id:
            _check_id(session_id)
        else:
            if follow_current:
                session_id = cls.read_current(root)
            session_id = session_id or new_session_id()
        directory = os.path.join(root, SESSIONS_DIR, session_id)
        return cls(root, session_id, created=not os.path.isdir(directory))

    def _ensure_dir(self) -> None:
        if self._dir_ready:
            return
        os.makedirs(self.dir, exist_ok=True)
        # Session artifacts are records, not source. Committing them was a real
        # bug in the spike, so the ignore entry is written before anything else
        # lands in the directory — via the existing helper, which handles the
        # missing-trailing-newline case that the spike got wrong.
        add_to_gitignore(self.root, f"{STATE_DIR}/")
        self._dir_ready = True

    @staticmethod
    def read_current(root: str) -> Optional[str]:
        try:
            with open(os.path.join(root, CURRENT), "r", encoding="utf-8") as fh:
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
            with open(self.path(PREFIX_FILE), "r", encoding="utf-8") as fh:
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
            rel: SentFile(rel=rel, role=rec.get("role", ""), hash=rec.get("hash", ""), turn=turn)
            for rel, rec in (data.get("files") or {}).items()
        }

    def record_selection(self, files: Dict[str, Dict[str, str]], demoted: List[dict]) -> None:
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
            with open(self.path(TRANSCRIPT), "r", encoding="utf-8") as fh:
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
                turns.append(Turn(n=int(rec.get("turn", 0)), question=rec["question"], answer=answer))
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
    def load_cache_handle(self, provider: str, model: str, digest: str) -> Optional[Dict[str, Any]]:
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
