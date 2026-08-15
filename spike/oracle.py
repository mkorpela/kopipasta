#!/usr/bin/env python3
"""Throwaway spike proving the `exec:` backend end-to-end.

NOT the Phase 1 implementation — no refactor, no core package, no TUI changes.
This exists to answer one question: does the context-oracle pipeline actually
work when the backend is just another CLI on the PATH?

    python spike/oracle.py pack  --all --budget 40k --json
    python spike/oracle.py ask   -m 'kopipasta/**/*.py' -q "..." --json
    python spike/oracle.py patch -e kopipasta/prompt.py -q "..." --verify 'pytest -q'

Reuses the real kopipasta modules for everything that matters: gitignore
walking, AST symbol extraction, project structure, and the patcher.
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import click  # noqa: E402

from kopipasta.file import (  # noqa: E402
    extract_symbols,
    is_binary,
    is_ignored,
    read_file_contents,
)
from kopipasta.config import read_gitignore  # noqa: E402
from kopipasta.git_utils import add_to_gitignore  # noqa: E402
from kopipasta.ops import estimate_tokens  # noqa: E402
from kopipasta.prompt import get_language_for_file, get_project_structure  # noqa: E402
from kopipasta.patcher import apply_patches, find_paths_in_text, parse_llm_output  # noqa: E402

import backends  # noqa: E402

ROOT = os.path.abspath(os.getcwd())
SESSIONS = os.path.join(ROOT, ".kopipasta", "sessions")

# Exit codes from AGENT_CLI_SPEC.md §8
EX_OK, EX_USAGE, EX_NOBACKEND, EX_BACKEND, EX_PARTIAL, EX_FAILED, EX_BUDGET, EX_VERIFY = range(8)

EDIT, REF, MAP = "edit", "ref", "map"

_REAL_STDOUT = sys.stdout


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------
def expand(pattern: str, ignore: List[str]) -> List[str]:
    """Glob / dir / literal path -> list of abs paths, gitignore + binary filtered."""
    if os.path.isdir(pattern):
        pattern = os.path.join(pattern, "**", "*")
    hits = globlib.glob(pattern, recursive=True)
    out = []
    for h in hits:
        a = os.path.abspath(h)
        if not os.path.isfile(a):
            continue
        if is_ignored(a, ignore, ROOT) or is_binary(a):
            continue
        out.append(a)
    return sorted(set(out))


def walk_all(ignore: List[str]) -> List[str]:
    out = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore, ROOT)]
        for f in files:
            p = os.path.join(root, f)
            if not is_ignored(p, ignore, ROOT) and not is_binary(p):
                out.append(p)
    return sorted(out)


def resolve(args, ignore: List[str]) -> Dict[str, str]:
    """Build {abs_path: role}. Later flags win; --exclude is applied last."""
    sel: Dict[str, str] = {}
    if args.all:
        for p in walk_all(ignore):
            sel[p] = MAP
    for pat in args.map or []:
        for p in expand(pat, ignore):
            sel[p] = MAP
    for pat in args.ref or []:
        for p in expand(pat, ignore):
            sel[p] = REF
    for pat in args.edit or []:
        for p in expand(pat, ignore):
            sel[p] = EDIT
    for pat in args.exclude or []:
        for p in expand(pat, ignore):
            sel.pop(p, None)
    return sel


def render_size(path: str, role: str) -> int:
    if role == MAP:
        return sum(len(s) + 1 for s in extract_symbols(path))
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def apply_budget(sel: Dict[str, str], budget_chars: Optional[int]) -> Tuple[Dict[str, str], List[str]]:
    """Demote full -> skeleton -> path-only until under budget. -e is never demoted."""
    if not budget_chars:
        return sel, []
    demoted: List[str] = []
    total = sum(render_size(p, r) for p, r in sel.items())
    if total <= budget_chars:
        return sel, []
    # ladder order: REF largest-first, then MAP largest-first. EDIT is untouchable.
    for stage in (REF, MAP):
        victims = sorted(
            [p for p, r in sel.items() if r == stage],
            key=lambda p: render_size(p, sel[p]),
            reverse=True,
        )
        for p in victims:
            if total <= budget_chars:
                break
            before = render_size(p, sel[p])
            if stage == REF:
                sel[p] = MAP
                total -= before - render_size(p, MAP)
            else:
                del sel[p]
                total -= before
            demoted.append(os.path.relpath(p, ROOT))
    return sel, demoted


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------
def render(sel: Dict[str, str], ignore: List[str], question: str, mode: str) -> Tuple[str, str]:
    """Returns (prefix, suffix).

    prefix = the stable repo payload, reused verbatim across turns of a session.
    suffix = the varying task. The split IS the cache breakpoint — providers that
    support explicit caching key on the prefix, so it must not contain the task.
    """
    edits = sorted(p for p, r in sel.items() if r == EDIT)
    refs = sorted(p for p, r in sel.items() if r == REF)
    maps = sorted(p for p, r in sel.items() if r == MAP)

    out: List[str] = ["# Project Overview", "", "## Project Structure", "", "```json"]
    out += [get_project_structure(ignore, ["."], maps), "```", ""]

    if edits:
        out += ["## Active Workspace (Editable)", ""]
        for p in edits:
            rel = os.path.relpath(p, ROOT)
            out += [f"# FILE: {rel}", f"```{get_language_for_file(p)}", read_file_contents(p), "```", ""]
    if refs:
        out += ["## Reference Context (Read-Only)", ""]
        for p in refs:
            rel = os.path.relpath(p, ROOT)
            out += [f"# FILE: {rel}", f"```{get_language_for_file(p)}", read_file_contents(p), "```", ""]
    if maps:
        out += ["## Symbol Map (skeletons only)", "", "```"]
        for p in maps:
            syms = extract_symbols(p)
            if syms:
                out.append(os.path.relpath(p, ROOT))
                out += [f"    {s}" for s in syms]
        out += ["```", ""]

    prefix = "\n".join(out)
    out = ["## Task Instructions", "", question, ""]

    if mode == "triage":
        out += [
            "## Required Output Format",
            "",
            "Return ONLY a single JSON object in a ```json code block. No prose before or after:",
            "",
            "```json",
            '{"relevant_files": [{"path": "...", "why": "...", "confidence": 0.0}],',
            ' "hypothesis": "...", "missing_context": ["..."], "suggested_selection": ["..."]}',
            "```",
        ]
    elif mode == "patch":
        out += [
            "## Code Output Rules (CRITICAL)",
            "",
            "A local tool auto-applies your code blocks.",
            "- Every code block starts with a path comment: `# FILE: path/to/file.py`",
            "- To EDIT an existing file, use a Search/Replace block:",
            "  `<<<<<<< SEARCH` / exact existing lines / `=======` / new lines / `>>>>>>> REPLACE`",
            "- To CREATE a file, give the FULL content.",
            "- Only files under '## Active Workspace (Editable)' may be modified.",
            "- Output the code blocks and nothing else. Do not ask questions.",
        ]
    return prefix, "\n".join(out)


# --------------------------------------------------------------------------
# session + backend
# --------------------------------------------------------------------------
def open_session(sid: Optional[str]) -> Tuple[str, str, int]:
    sid = sid or f"{time.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:4]}"
    d = os.path.join(SESSIONS, sid)
    os.makedirs(d, exist_ok=True)
    turn = len([f for f in os.listdir(d) if f.endswith("-request.md")]) + 1
    add_to_gitignore(ROOT, ".kopipasta/")  # handles the missing-trailing-newline case
    return sid, d, turn


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        # Must mirror the prompt template's shape exactly. Where the provider
        # enforces the schema, the schema wins — a flatter one here would
        # silently drop the `why`/`confidence` that makes triage worth reading.
        "relevant_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "why": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["path", "why", "confidence"],
            },
        },
        "hypothesis": {"type": "string"},
        "missing_context": {"type": "array", "items": {"type": "string"}},
        "suggested_selection": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant_files", "hypothesis", "missing_context", "suggested_selection"],
}


def run_backend(spec, prefix, suffix, timeout, base_url, schema,
                cache=True, cache_ttl_s=None):
    b = None
    try:
        kw = {} if cache_ttl_s is None else {"cache_ttl_s": cache_ttl_s}
        b = backends.build(spec, base_url=base_url, timeout=timeout, cache=cache, **kw)
        c = b.complete(prefix, suffix, schema=schema)
    except backends.BackendError as e:
        kind = EX_NOBACKEND if "unknown backend" in str(e) else EX_BACKEND
        return kind, "", str(e), None
    finally:
        # A backend may hold a cache that bills per token-hour until its TTL.
        # This is a single-shot CLI: nothing here will reuse it, so holding it
        # open would be pure rent. A real session (spec §6) is where keeping it
        # alive starts to pay, and that is also where an owner has to exist.
        if b is not None:
            close = getattr(b, "close", None)
            if callable(close):
                close()
    return EX_OK, c.text, "", c


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def parse_budget(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s = s.strip().lower()
    as_chars = s.endswith("c")  # bare number = tokens; 'c' suffix = literal chars
    if as_chars:
        s = s[:-1]
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    return int(float(s) * mult * (1 if as_chars else 4))  # tokens -> chars, ~4:1


def cmd_pack(args) -> int:
    ignore = read_gitignore()
    sel = resolve(args, ignore)
    if not sel:
        emit(args, {"ok": False, "error": "empty selection"})
        return EX_USAGE
    sel, demoted = apply_budget(sel, parse_budget(args.budget))
    if demoted and args.strict_budget:
        emit(args, {"ok": False, "error": "budget exceeded", "demoted": demoted})
        return EX_BUDGET
    prefix, suffix = render(sel, ignore, args.question or "(no task)", args.mode)
    payload = prefix + "\n" + suffix
    out = args.out or os.path.join(SESSIONS, "pack.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(payload)
    emit(args, {
        "ok": True, "payload": os.path.relpath(out, ROOT),
        "chars": len(payload), "est_tokens": estimate_tokens(len(payload)),
        "sent": counts(sel), "demoted": demoted,
    })
    return EX_OK


def counts(sel: Dict[str, str]) -> Dict[str, int]:
    return {r: sum(1 for v in sel.values() if v == r) for r in (EDIT, REF, MAP)}


def cmd_ask(args) -> int:
    ignore = read_gitignore()
    sel = resolve(args, ignore)
    if not sel:
        emit(args, {"ok": False, "error": "empty selection"})
        return EX_USAGE
    sel, demoted = apply_budget(sel, parse_budget(args.budget))
    if demoted and args.strict_budget:
        emit(args, {"ok": False, "error": "budget exceeded", "demoted": demoted})
        return EX_BUDGET

    mode = args.mode or ("patch" if args.apply else "default")
    prefix, suffix = render(sel, ignore, args.question, mode)
    payload = prefix + "\n" + suffix
    sid, sdir, turn = open_session(args.session)
    req = os.path.join(sdir, f"{turn:03d}-request.md")
    with open(req, "w", encoding="utf-8") as f:
        f.write(payload)

    t0 = time.time()
    schema = TRIAGE_SCHEMA if mode == "triage" else None
    code, stdout, err, comp = run_backend(
        args.backend, prefix, suffix, args.timeout, args.base_url, schema,
        cache=not getattr(args, "no_cache", False),
        cache_ttl_s=getattr(args, "cache_ttl", None))
    dt = round(time.time() - t0, 1)

    resp = os.path.join(sdir, f"{turn:03d}-response.md")
    with open(resp, "w", encoding="utf-8") as f:
        f.write(stdout)

    base = {
        "ok": code == EX_OK, "session": sid, "turn": turn,
        "request": os.path.relpath(req, ROOT), "response": os.path.relpath(resp, ROOT),
        "sent": counts(sel), "demoted": len(demoted),
        "payload_chars": len(payload), "est_input_tokens": estimate_tokens(len(payload)),
        "response_chars": len(stdout), "latency_s": dt,
    }
    if comp is not None and (comp.input_tokens or comp.cached_tokens or comp.cost_usd or comp.cache_creation_tokens):
        base["usage"] = {"input": comp.input_tokens, "cached": comp.cached_tokens,
                         "output": comp.output_tokens, "model": comp.model}
        if comp.cache_creation_tokens:
            # NB: includes the harness system prompt, not just our payload.
            base["usage"]["cache_creation"] = comp.cache_creation_tokens
        if comp.cost_usd:
            base["usage"]["cost_usd"] = round(comp.cost_usd, 4)
    if code != EX_OK:
        base["error"] = err
        emit(args, base)
        return code

    all_rel = [os.path.relpath(p, ROOT) for p in walk_all(ignore)]
    base["files_cited"] = find_paths_in_text(stdout, all_rel)[:20]

    if mode == "triage":
        base["triage"] = extract_json(stdout)
        if base["triage"] is None:
            # `ok: true` beside `triage: null` is the worst possible report: a
            # caller branching on `ok` proceeds with no answer. Triage mode
            # promises a machine-readable result, so failing to produce one is
            # a failure of the call, not a quiet null.
            base["ok"] = False
            base["error"] = (
                "triage mode returned no parseable JSON "
                f"({len(stdout)} chars written to {base['response']}). "
                "Usually a truncated answer — raise --timeout or the model's "
                "output budget."
            )
            code = EX_BACKEND
    else:
        base["answer_head"] = " ".join(stdout.split())[:240]

    if not args.apply:
        with open(os.path.join(sdir, f"{turn:03d}-meta.json"), "w", encoding="utf-8") as f:
            json.dump(base, f, indent=2)
        emit(args, base)
        return EX_OK

    # ---- apply path ----
    editable = {os.path.relpath(p, ROOT) for p, r in sel.items() if r == EDIT}
    patches = parse_llm_output(stdout, console=None)
    illegal = [p["file_path"] for p in patches if os.path.normpath(p["file_path"]) not in editable]
    patches = [p for p in patches if os.path.normpath(p["file_path"]) in editable]
    base["rejected_not_editable"] = illegal

    # §11.2: policy replaces the human. Deletes denied, shrink guard hard-fails.
    click.confirm = lambda *a, **k: bool(args.allow_delete)  # type: ignore[assignment]

    modified = apply_patches(patches, logger=None) if patches else []
    base["applied"] = sorted(set(modified))  # one patch per hunk -> dedupe by file
    base["failed"] = sorted({p["file_path"] for p in patches if p["file_path"] not in modified})
    base["diffstat"] = diffstat()

    if not modified:
        base["ok"] = False
        emit(args, base)
        return EX_FAILED

    rc = EX_PARTIAL if base["failed"] else EX_OK
    if args.verify:
        v = subprocess.run(args.verify, shell=True, capture_output=True, text=True, cwd=ROOT)
        log = os.path.join(sdir, f"{turn:03d}-verify.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write(v.stdout + v.stderr)
        base["verify"] = {
            "command": args.verify, "exit": v.returncode,
            "log": os.path.relpath(log, ROOT),
            "tail": " ".join((v.stdout + v.stderr).split())[-300:],
        }
        if v.returncode != 0:
            base["ok"] = False
            if args.revert_on_fail:
                subprocess.run(["git", "checkout", "--", "."], cwd=ROOT)
                base["reverted"] = True
            rc = EX_VERIFY
    base["ok"] = rc == EX_OK
    with open(os.path.join(sdir, f"{turn:03d}-meta.json"), "w", encoding="utf-8") as f:
        json.dump(base, f, indent=2)
    emit(args, base)
    return rc


def diffstat() -> Dict[str, int]:
    r = subprocess.run(["git", "diff", "--numstat"], cwd=ROOT, capture_output=True, text=True)
    files = ins = dele = 0
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            files += 1
            ins += int(parts[0])
            dele += int(parts[1])
    return {"files": files, "insertions": ins, "deletions": dele}


def extract_json(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def emit(args, obj) -> None:
    if args.json:
        # _REAL_STDOUT, not print(): stdout is redirected to stderr for the
        # whole run so library narration (read_gitignore's ".gitignore
        # detected.", patcher progress) cannot corrupt the JSON contract.
        print(json.dumps(obj, indent=2), file=_REAL_STDOUT)
    else:
        for k, v in obj.items():
            print(f"{k:>22}: {v}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="oracle-spike")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pack", "ask", "patch"):
        p = sub.add_parser(name)
        p.add_argument("-e", "--edit", action="append")
        p.add_argument("-r", "--ref", action="append")
        p.add_argument("-m", "--map", action="append")
        p.add_argument("-x", "--exclude", action="append")
        p.add_argument("--all", action="store_true")
        p.add_argument("--budget")
        p.add_argument("--strict-budget", action="store_true")
        p.add_argument("--mode", default="default", choices=["default", "triage", "patch"])
        p.add_argument("--json", action="store_true")
        p.add_argument("-q", "--question", default="")
        if name == "pack":
            p.add_argument("-o", "--out")
        else:
            p.add_argument("--backend", default=os.environ.get("KOPIPASTA_BACKEND", "exec:claude -p"))
            p.add_argument("--session")
            p.add_argument("--timeout", type=int, default=900)
            p.add_argument("--base-url")
            p.add_argument("--verify")
            p.add_argument("--revert-on-fail", action="store_true")
            p.add_argument("--allow-delete", action="store_true")
            # Gemini's prefix cache is a rented resource, not a request flag.
            # --no-cache exists because that rent is real: a one-shot question
            # that will never have a turn 2 pays storage for nothing.
            p.add_argument("--no-cache", action="store_true",
                           help="don't create a provider-side prefix cache (gemini:)")
            p.add_argument("--cache-ttl", type=int, default=None, metavar="SECONDS",
                           help="lifetime of the provider-side prefix cache "
                                "(gemini:; default 300, max 3600)")
    args = ap.parse_args()
    args.apply = args.cmd == "patch"
    if args.json:
        # Spec §8: stdout is data, stderr is narration. Enforce it rather than
        # trusting every library on the path to have got the memo.
        sys.stdout = sys.stderr
    if args.cmd == "pack":
        return cmd_pack(args)
    if args.apply and not args.edit:
        emit(args, {"ok": False, "error": "patch requires at least one -e/--edit"})
        return EX_USAGE
    return cmd_ask(args)


if __name__ == "__main__":
    sys.exit(main())
