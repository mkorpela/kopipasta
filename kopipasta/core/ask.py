"""`kopipasta ask` — pack, ask the model, record the turn. Spec §3.

This is the whole common path, and the reason the rest of the package exists:

    kopipasta ask --all -q "Auth tokens are accepted after expiry. Which files
                            implement validation and expiry?" --json

The caller spends a couple of hundred tokens to ask a question whose answer
required half a million tokens of reading. Nothing large crosses back: stdout
carries paths, counts and a short answer, while the request, the response and
the conversation live in `.kopipasta/sessions/`.

Three rules this file exists to keep:

**Pointers, not payloads.** `--json` is a summary plus the paths to the real
artifacts. The one exception is the answer itself, which is what was asked for.

**No hidden interactivity.** Every branch has a non-TTY outcome. The only
question with no safe default is "what is the task", and it names the flag
that avoids it rather than blocking on a prompt nobody can answer.

**A failure carries what was sent.** When the oracle is wrong the caller
inherits a confident wrong answer with none of the evidence, so the request
path, the counts and every demotion are reported on the failure path too —
not just on the happy one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from kopipasta.cache import find_project_root, get_project_key
from kopipasta.config import read_env_file, read_gitignore, read_project_context
from kopipasta.core import budget as budgetmod
from kopipasta.core import modes as modesmod
from kopipasta.core.backend import GeminiBackend, build
from kopipasta.core.config import resolve_backend
from kopipasta.core.context import (
    Payload,
    Update,
    content_hash,
    render_prefix,
    render_suffix,
    role_content,
)
from kopipasta.core.errors import (
    EXIT_OK,
    BackendActedAsAgent,
    BudgetExceeded,
    DeadlineExceeded,
    InteractionRequired,
    KopipastaError,
    PatchNotParseable,
    SchemaInvalid,
    UsageError,
)
from kopipasta.core.resolver import ROLE_ORDER as ALL_ROLES
from kopipasta.core.resolver import SelectionSpec, resolve, walk_all
from kopipasta.core.session import Session
from kopipasta.interaction import NoHumanAttached, require_human
from kopipasta.output import (
    HelpToStdoutParser,
    emit,
    emit_json,
    narrate,
    stdout_reserved_for_output,
)
from kopipasta.patcher import find_paths_in_text, parse_llm_output

#: How many demotions to name in the JSON. The full list is always in the
#: session's selection.json — this is a summary, not the record.
DEMOTED_IN_JSON = 20


# --------------------------------------------------------------------------
# argument surface
# --------------------------------------------------------------------------
def add_selection_args(parser: argparse.ArgumentParser) -> None:
    """The selection grammar of spec §4. Shared with the other verbs."""
    g = parser.add_argument_group("selection (repeatable, order-independent)")
    g.add_argument("-e", "--edit", action="append", metavar="PATTERN",
                   help="full content, editable — the active workspace")
    g.add_argument("-r", "--ref", action="append", metavar="PATTERN",
                   help="full content, read-only — dependencies and call sites")
    g.add_argument("-m", "--map", action="append", metavar="PATTERN",
                   help="AST skeleton only — signatures and one docstring line")
    g.add_argument("-s", "--snippet", action="append", metavar="PATTERN",
                   help="first 50 lines only — a coarse peek")
    g.add_argument("-x", "--exclude", action="append", metavar="PATTERN",
                   help="drop these; applied last, wins over everything")
    g.add_argument("--all", action="store_true",
                   help="every non-ignored file, as a skeleton (subject to --budget)")
    g.add_argument("--changed", action="store_true",
                   help="git working-tree changes, including untracked, as editable")
    g.add_argument("--changed-since", metavar="REF",
                   help="git diff --name-only REF...HEAD, as editable")
    g.add_argument("--from-file", metavar="PATH",
                   help="newline-delimited paths, as reference — feeds a triage answer back in")
    parser.epilog = (
        "Patterns take globs (src/**/*.py), directories (recursive) and literal paths.\n"
        "@file anywhere a pattern is expected reads patterns from that file.\n"
        ".gitignore and binary filtering always apply. The most detailed role wins,\n"
        "so -m '**/*.py' -e kopipasta/patcher.py skeletons the tree but sends that\n"
        "one file in full."
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter


def build_parser() -> argparse.ArgumentParser:
    p = HelpToStdoutParser(
        prog="kopipasta ask",
        description="Ask a model about this repository with its own large context window.",
    )
    add_selection_args(p)

    q = p.add_argument_group("the question")
    q.add_argument("-q", "--question", metavar="TEXT",
                   help="the task or question. @file reads it from a file.")
    q.add_argument("--mode", default=modesmod.DEFAULT_MODE, metavar="MODE",
                   help=f"{', '.join(modesmod.MODE_NAMES)} (default: {modesmod.DEFAULT_MODE})")
    q.add_argument("--no-project-context", action="store_true",
                   help="do not prepend AI_CONTEXT.md")

    b = p.add_argument_group("budget and backend")
    b.add_argument("--budget", metavar="SIZE",
                   help="target payload size in tokens (400k), or characters with a c suffix")
    b.add_argument("--strict-budget", action="store_true",
                   help="exit 6 instead of demoting files down the ladder")
    b.add_argument("--backend", metavar="SPEC",
                   help="provider:model, overriding the config file. 'none' calls no model.")
    b.add_argument("--dry-run", action="store_true",
                   help="assemble and record everything, call no model (same as --backend none)")
    b.add_argument("--max-tokens", type=int, metavar="N",
                   help="output budget for the answer; reasoning tokens spend it too")
    b.add_argument("--timeout", type=float, metavar="SECONDS", help="cap one backend call")
    b.add_argument("--deadline", type=float, metavar="SECONDS",
                   help="cap the whole invocation, whichever stage misbehaves")
    b.add_argument("--base-url", metavar="URL", help="override the provider endpoint")

    s = p.add_argument_group("session")
    which = s.add_mutually_exclusive_group()
    which.add_argument("--session", metavar="ID",
                       help="continue (or start) a named conversation")
    which.add_argument("--continue", dest="continue_", action="store_true",
                       help="continue the session in 'current' (ask starts fresh otherwise)")
    which.add_argument("--new", action="store_true",
                       help="start a fresh session — the default, stated explicitly")
    s.add_argument("--no-cache", action="store_true",
                   help="never create a provider-side prefix cache")
    s.add_argument("--cache-ttl", type=int, metavar="SECONDS",
                   help="lifetime of that cache; it is rented until it expires")

    p.add_argument("--json", action="store_true",
                   help="stdout becomes a single JSON object (spec §8)")
    return p


def _text_arg(value: Optional[str], root: str, flag: str) -> Optional[str]:
    """`@file` reads the value from a file, for text too big to quote in a shell."""
    if not value or not value.startswith("@"):
        return value
    path = value[1:]
    target = path if os.path.isabs(path) else os.path.join(root, path)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise UsageError(
            f"could not read {flag} {path!r}.",
            detail=str(exc),
            hint=f"{flag} @file reads the text from that file.",
        ) from exc


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------
def extract_json(text: str) -> Optional[Any]:
    """The structured answer, however the provider chose to wrap it.

    Enforced schemas return bare JSON; everything else tends to fence it. The
    outermost-braces fallback is last because it is the one that can silently
    parse the wrong thing.
    """
    if not text:
        return None
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    fence = stripped.find("```")
    while fence != -1:
        end = stripped.find("```", fence + 3)
        if end == -1:
            break
        block = stripped[fence + 3 : end]
        block = block.split("\n", 1)[1] if "\n" in block else block
        try:
            return json.loads(block.strip())
        except ValueError:
            fence = stripped.find("```", end + 3)
    start, stop = stripped.find("{"), stripped.rfind("}")
    if start != -1 and stop > start:
        try:
            return json.loads(stripped[start : stop + 1])
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# the verb
# --------------------------------------------------------------------------
def run(argv: Sequence[str]) -> int:
    """Parse, run, report. Returns the process exit code; never raises.

    The output contract is established here rather than assumed, so that this
    holds however `ask` was reached. `read_gitignore` prints ".gitignore
    detected." and the patcher narrates its progress — both to stdout, neither
    aware of any contract — and one of those landing mid-object is the
    difference between a JSON reply and an unparseable one. Nesting is
    supported, so going through `main` costs nothing.
    """
    with stdout_reserved_for_output():
        return _run_parsed(argv)


def _run_parsed(argv: Sequence[str]) -> int:
    argv = list(argv)
    try:
        args = build_parser().parse_args(argv)
    except KopipastaError as exc:
        # A bad command line, raised by the parser so it can exit 1 rather than
        # argparse's 2 (spec §8 reserves 2 for "no usable backend"). There is
        # no `args` yet, so --json has to be read off argv directly. It takes
        # no value, so the only false positive is a question whose text is
        # literally "--json" — on a run that has already failed, where the cost
        # is an error object instead of prose.
        return report_failure(exc, json_mode="--json" in argv)
    try:
        return _ask(args)
    except KopipastaError as exc:
        return report_failure(exc, json_mode=args.json)
    except NoHumanAttached as exc:
        # Exit 8, and in JSON like everything else: a harness must be able to
        # tell "needs a policy or a different invocation" from "your command
        # line was wrong" without reading prose off stderr.
        return report_failure(InteractionRequired(str(exc)), json_mode=args.json)


def report_failure(exc: KopipastaError, *, json_mode: bool) -> int:
    """Spec §9: stdout stays empty on failure — unless JSON *is* the artifact.

    A partial artifact is worse than none, because the caller cannot tell it is
    partial. In `--json` mode the error object is the artifact, and it carries
    `error` (a stable slug) and `retryable` so neither has to be inferred.
    """
    if json_mode:
        emit_json(exc.to_json())
    else:
        narrate(exc.render())
    return exc.exit_code


#: Markers that mean a patch was attempted. Matched at the start of a line so
#: prose *about* patching ("wrap it in a ``` fence") does not qualify — the
#: point is to tell a formatting slip from a backend that never tried, and a
#: detector that fires on any mention would collapse them again.
_PATCH_MARKERS = re.compile(
    r"^\s*(?:#\s*FILE:|<<<<+\s*SEARCH|<<<<+\s*$|@@\s+-\d|>>>>+\s*REPLACE)",
    re.MULTILINE | re.IGNORECASE,
)


def _looks_like_a_patch(text: str) -> bool:
    return bool(_PATCH_MARKERS.search(text or ""))


def _planned_backend(args: argparse.Namespace):
    """Which backend would answer, whether or not one is going to.

    A dry run must not fail because no backend is configured — assembling a
    payload needs no key. But it should still size that payload for the
    provider that would have read it, so this returns None rather than raising
    and the estimator falls back to the pessimistic default.
    """
    try:
        return resolve_backend("ask", flag=args.backend)
    except KopipastaError:
        if not args.dry_run:
            raise
        return None


def _ask(args: argparse.Namespace) -> int:
    started = time.monotonic()
    root = str(find_project_root())

    def check_deadline(stage: str) -> float:
        """Remaining seconds, or the end of the run. Bounded runtime, spec §12."""
        if args.deadline is None:
            return float("inf")
        left = args.deadline - (time.monotonic() - started)
        if left <= 0:
            raise DeadlineExceeded(time.monotonic() - started, args.deadline, stage)
        return left

    # 1. Backend first: it is instant, and a missing key should not be
    #    discovered after a 40-second walk of a large repository.
    #
    #    Resolved twice under --dry-run, deliberately. The run calls no model,
    #    but "how big is this payload" is the whole question a dry run exists
    #    to answer, and the answer differs by ~50% between tokenizers. So the
    #    *planned* provider sets the estimator even when `none` does the work.
    planned = _planned_backend(args)
    cfg = resolve_backend("ask", flag="none") if args.dry_run else planned
    cfg.require_api_key()
    cpt = budgetmod.chars_per_token(planned.provider if planned else None)
    mode = modesmod.get(args.mode)
    question = _text_arg(args.question, root, "-q") or ""

    # 2. Session. Resolved before the selection, because a follow-up turn
    #    legitimately has no selectors at all: the context is already there.
    #    Resumption is explicit, never inferred (spec §7). It used to key off
    #    `--json`, so the same command continued a conversation for a human and
    #    started one for an agent — an output flag deciding which context a
    #    question landed in. A disposable oracle is disposable by default.
    session = Session.open(root, args.session, follow_current=args.continue_)
    prefix = session.load_prefix()
    if prefix is not None and not args.json:
        narrate(
            f"kopipasta: continuing session {session.id}, turn {session.turn}"
        )

    if not question:
        # No safe default exists for "what is the task", so this refuses
        # rather than guesses — naming the flag that avoids the question.
        require_human("A question", "Pass -q/--question, or -q @file.")
        from rich.console import Console

        from kopipasta.prompt import get_task_from_user_interactive

        question = get_task_from_user_interactive(Console(stderr=True))
        if not question.strip():
            raise UsageError(
                "no question was given.",
                hint='kopipasta ask --all -q "which files implement token expiry?"',
            )

    # 3. Selection.
    ignore = read_gitignore()
    spec = SelectionSpec(
        edit=args.edit or [],
        ref=args.ref or [],
        map=args.map or [],
        snippet=args.snippet or [],
        exclude=args.exclude or [],
        all=args.all,
        changed=args.changed,
        changed_since=args.changed_since,
        from_file=args.from_file,
    )
    if spec.is_empty() and prefix is None:
        raise UsageError(
            "nothing was selected, so there is nothing to ask about.",
            detail="A question with no files behind it is answered from the project "
            "structure alone, and that answer reads exactly like a real one.",
            hint="-e FILE      the files to work on\n"
            "-m '**/*.py' skeleton the tree\n"
            "--all        everything, subject to --budget",
        )

    budget_tokens = budgetmod.parse_budget(args.budget, cpt)
    selection = None
    demotions: List[budgetmod.Demotion] = []
    if not spec.is_empty():
        selection = resolve(spec, ignore, root)
        wanted_tokens = budgetmod.estimate_tokens(budgetmod.selection_chars(selection), cpt)
        demotions = budgetmod.demote_to_fit(selection, budget_tokens, cpt)
        if demotions and args.strict_budget:
            raise BudgetExceeded(wanted_tokens, budget_tokens or 0, [d.path for d in demotions])
        if demotions:
            narrate(budgetmod.summarise(demotions))
        for flag, pattern in selection.unmatched():
            # Not fatal — the rest of the selection stands — but never silent:
            # a typo'd selector that still leaves files selected is the exact
            # shape that produces a confident answer from the wrong context.
            narrate(f"kopipasta: {flag} {pattern} matched no files.")
    check_deadline("selection")

    # 4. The payload. The prefix is fixed for the life of the session so the
    #    provider-side cache keeps hitting; everything new rides in the suffix.
    env_vars = read_env_file()
    in_prefix = session.prefix_files()
    updates: List[Update] = []
    deduped: List[str] = []
    hashes: Dict[str, Dict[str, str]] = {}

    if prefix is None:
        # Guaranteed by the empty-selection check above, but stated rather than
        # asserted: `python -O` strips an assert, and the failure it would then
        # produce is a TypeError three frames deeper.
        if selection is None:  # pragma: no cover - unreachable
            raise UsageError("nothing was selected, so there is nothing to ask about.")
        project_context = None if args.no_project_context else read_project_context(root)

        def render() -> str:
            return render_prefix(
                selection,
                ignore=ignore,
                root=root,
                env_vars=env_vars,
                project_context=project_context,
            )

        prefix = render()
        if budget_tokens:
            # The ladder works from file sizes and cannot see the structure
            # blob, the zone headings or the project constitution. One
            # corrective pass, with that overhead now measured rather than
            # guessed, is the difference between a budget that holds and a
            # budget that is merely reported as exceeded.
            tail = len(render_suffix(question, mode))
            overhead = len(prefix) + tail - budgetmod.selection_chars(selection)
            room = budget_tokens - budgetmod.estimate_tokens(overhead, cpt)
            extra = budgetmod.demote_to_fit(selection, max(1, room), cpt)
            if extra and args.strict_budget:
                # `--strict-budget` means "exit 6 instead of demoting", and a
                # second place that demotes is a second place that has to obey
                # it. Found by dogfooding: the first pass honoured the flag and
                # this one silently did not, so a strict run under the estimate
                # but over the rendered size demoted anyway and exited 0.
                raise BudgetExceeded(
                    budgetmod.estimate_tokens(len(prefix) + tail, cpt),
                    budget_tokens,
                    [d.path for d in demotions + extra],
                )
            if extra:
                # Collapsed across both passes: a file that went full ->
                # skeleton in the first and skeleton -> path-only in the
                # second is one demoted file, not two.
                demotions = budgetmod.collapse(demotions + extra)
                narrate(budgetmod.summarise(extra, label="still over once rendered"))
                prefix = render()
        prefix_reused = False
        # Every role, not just the expensive ones: a skeleton is in the prefix
        # too, and turn 2 has to be able to tell "already there" from "never
        # sent" for those as well.
        for entry in selection.by_role(*ALL_ROLES):
            hashes[entry.rel] = {"role": entry.role, "hash": content_hash(entry.path)}
        session.save_prefix(prefix, hashes)
    else:
        prefix_reused = True
        for entry in (selection.by_role(*ALL_ROLES) if selection else []):
            digest = content_hash(entry.path)
            hashes[entry.rel] = {"role": entry.role, "hash": digest}
            previous = in_prefix.get(entry.rel)
            note = ""
            if previous is None:
                note = "new this turn"
            elif previous.hash != digest:
                note = f"changed since turn {previous.turn} — supersedes the copy above"
            elif previous.role != entry.role:
                # Same bytes, different role. Deduping on the hash alone would
                # answer "-e thing.py" with the 50-line snippet turn 1 sent and
                # report it as `edit: 1` — a file withheld from the model while
                # the record says it was sent.
                note = (
                    f"was {previous.role} in the context above, now {entry.role}"
                    " — supersedes that copy"
                )
            if note:
                updates.append(
                    Update(entry.rel, entry.role, role_content(entry.path, entry.role), note)
                )
            else:
                # Already in the prefix, byte for byte, in this same role.
                # Referencing it by path is the point of keeping the hashes.
                deduped.append(entry.rel)

    suffix = render_suffix(
        question,
        mode,
        history=session.history() if prefix_reused else (),
        updates=updates,
        env_vars=env_vars,
    )
    payload = Payload(prefix, suffix, cpt)
    request_path = session.write_request(prefix, suffix, prefix_reused=prefix_reused)
    # What this turn's context actually contains: the prefix it inherited, with
    # anything selected this turn laid over the top. `hashes` alone is only the
    # latter, so a follow-up turn with no selectors recorded `{}` — telling
    # `apply` that nothing was editable in a turn that could see the whole
    # repo. Spec §11 reads the latest turn to enforce the Active Workspace, so
    # an incomplete record there is a wrong answer, not a missing one.
    inherited = {rel: {"role": s.role, "hash": s.hash} for rel, s in in_prefix.items()}
    session.record_selection({**inherited, **hashes}, [d.as_json() for d in demotions])

    sent = _counts(selection)
    sent["deduped"] = len(deduped)
    sent["demoted"] = len(demotions)

    base: Dict[str, Any] = {
        "ok": True,
        "session": session.id,
        "turn": session.turn,
        "mode": mode.name,
        "backend": cfg.spec,
        "request": session.rel(request_path),
        "sent": sent,
        "est_input_tokens": payload.est_tokens,
        "payload_chars": payload.chars,
    }
    if prefix_reused:
        # Without this, `sent: {edit: 0, ...}` on a follow-up turn reads as
        # "no files" when it means "nothing new" — the context is in the
        # prefix, which is still sent verbatim every turn.
        base["prefix_reused"] = True
        base["in_prefix"] = len(in_prefix)
    if demotions:
        base["demoted"] = [d.as_json() for d in demotions[:DEMOTED_IN_JSON]]
    if selection is not None and selection.unmatched():
        base["unmatched"] = [{"flag": f, "pattern": p} for f, p in selection.unmatched()]

    # --strict-budget is enforced here as well as on the estimate, because the
    # ladder works from file sizes and cannot see the structure blob or the
    # mode instructions. An underestimate must not become a silent overshoot.
    if budget_tokens and payload.est_tokens > budget_tokens:
        if args.strict_budget:
            raise BudgetExceeded(
                payload.est_tokens, budget_tokens, [d.path for d in demotions]
            )
        narrate(
            f"kopipasta: assembled payload is ~{payload.est_tokens:,} tokens, "
            f"over the {budget_tokens:,} budget."
        )

    # 5. The call.
    try:
        return _call_and_report(args, cfg, mode, session, payload, base, question, ignore,
                                root, check_deadline, budget_tokens)
    except KopipastaError as exc:
        # §15: when the oracle is wrong the caller inherits the answer with
        # none of the evidence. Failures carry what was sent, so the caller can
        # go and look at exactly what the oracle did — and did not — read.
        exc.fields.setdefault("session", session.id)
        exc.fields.setdefault("turn", session.turn)
        exc.fields.setdefault("request", base["request"])
        exc.fields.setdefault("sent", sent)
        session.write_turn_meta({**base, "ok": False, "error": exc.slug, "summary": exc.summary})
        raise


def _counts(selection) -> Dict[str, int]:
    if selection is None:
        return {"edit": 0, "ref": 0, "map": 0, "snippet": 0}
    return selection.counts()


def _call_and_report(
    args: argparse.Namespace,
    cfg,
    mode: modesmod.Mode,
    session: Session,
    payload: Payload,
    base: Dict[str, Any],
    question: str,
    ignore: Sequence[str],
    root: str,
    check_deadline,
    budget_tokens: Optional[int] = None,
) -> int:
    # A rented cache is only worth creating when a turn 2 is actually intended.
    # A one-shot question would pay storage for nothing, so caching follows the
    # session: named by the caller, or already past turn 1.
    wants_cache = not args.no_cache and (bool(args.session) or session.turn > 1)
    remaining = check_deadline("assembly")
    timeout = min(
        float(args.timeout) if args.timeout else float(cfg.timeout_s),
        remaining,
    )
    backend = build(
        cfg,
        base_url=args.base_url,
        timeout=timeout,
        cache=wants_cache,
        cache_ttl_s=args.cache_ttl,
        # Which project is renting, stamped into the provider-side display
        # name. One API key serves every repo on the machine and the cache
        # list is per key, so this is what lets `session reap` sweep here
        # without deleting a lease another repo is still paying for.
        label=get_project_key(root),
    )
    # The last check before the payload is sent: `--strict-budget` promised
    # the caller it would refuse rather than overshoot, and only the
    # provider's own tokenizer can keep that promise (spec §5). Elsewhere the
    # heuristic merely decides what to demote, which its calibration covers.
    #
    # This catches overshoot only. The earlier strict check fires before the
    # payload is rendered, so there is nothing to count yet, and a run refused
    # there is refused on the estimate. That direction is the safe one — the
    # ratio is deliberately the lowest measured — and buying back the last
    # percent would mean rendering before deciding, which is a worse trade
    # than the percent is worth.
    if budget_tokens and args.strict_budget:
        counted = _count_tokens(backend, payload)
        if counted:
            base["input_tokens_counted"] = counted
            if counted > budget_tokens:
                raise BudgetExceeded(counted, budget_tokens, [])

    digest = GeminiBackend.digest(payload.prefix)
    adopted = None
    if wants_cache and isinstance(backend, GeminiBackend):
        adopted = session.load_cache_handle(cfg.provider, cfg.model, digest)
        if adopted:
            backend.adopt(
                name=adopted["name"],
                digest=digest,
                expires_in_s=float(adopted["expires_at"]) - time.time(),
                tokens=int(adopted.get("tokens") or 0),
            )

    t0 = time.monotonic()
    try:
        completion = backend.complete(
            payload.prefix,
            payload.suffix,
            schema=mode.schema,
            max_tokens=int(args.max_tokens or cfg.max_tokens),
        )
    finally:
        _release_cache(backend, session, cfg, digest, wants_cache, adopted)
    latency = round(time.monotonic() - t0, 1)

    response_path = session.write_response(completion.text)
    base["response"] = session.rel(response_path)
    base["latency_s"] = latency
    base["response_chars"] = len(completion.text)
    if getattr(backend, "dry_run", False):
        # Nothing answered. Saying so explicitly beats an `ok: true` a caller
        # could mistake for an answer.
        base["dry_run"] = True
    if any(
        (
            completion.input_tokens,
            completion.output_tokens,
            completion.cached_tokens,
            completion.cache_creation_tokens,
            completion.cost_usd,
        )
    ):
        usage = {
            "input": completion.input_tokens,
            "cached": completion.cached_tokens,
            "output": completion.output_tokens,
            "model": completion.model,
        }
        if completion.cache_creation_tokens:
            usage["cache_creation"] = completion.cache_creation_tokens
        if completion.cost_usd:
            usage["cost_usd"] = round(completion.cost_usd, 4)
        base["usage"] = usage

    reason = getattr(backend, "cache_disabled_reason", "")
    if reason:
        narrate(f"kopipasta: prefix caching unavailable, paying full price ({reason})")

    result = _interpret(completion.text, mode, base, session, ignore, root, cfg)

    session.append_transcript(
        {
            "turn": session.turn,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": mode.name,
            "backend": cfg.spec,
            "question": question,
            "answer": completion.text,
        }
    )
    session.write_turn_meta(base)
    session.update_meta(backend=cfg.spec, model=completion.model or cfg.model,
                        usage=base.get("usage"))
    # Written on every run, including --json: `apply current` (spec §1, §11) is
    # reached from an agent's `ask --json`, so withholding the pointer there
    # left the flagship workflow with no handle to the artifact it just made.
    # Writing it is not the same as following it — `Session.open` still refuses
    # to resume it under --json, because that is where the raciness bites.
    session.set_current()

    _emit(args, base, result, mode, completion.text, session)
    return EXIT_OK


def _count_tokens(backend, payload: Payload) -> Optional[int]:
    """The provider's real count, when it has one to give.

    Optional by design: a backend that cannot count is not a backend that
    cannot run, and the estimate it falls back to is pessimistic.
    """
    counter = getattr(backend, "count_tokens", None)
    return counter(payload.text) if callable(counter) else None


def _release_cache(
    backend,
    session: Session,
    cfg,
    digest: str,
    wants_cache: bool,
    adopted: Optional[Dict[str, Any]] = None,
) -> None:
    """Hand the rental to the session, or hand it back to the provider.

    Every cache created here has exactly one owner at every moment. Keeping it
    alive for a turn 2 that never comes costs rent until the TTL expires,
    which is why only a named session is allowed to hold one.
    """
    if not isinstance(backend, GeminiBackend):
        close = getattr(backend, "close", None)
        if callable(close):
            close()
        return
    handle = backend.handle() if wants_cache else None
    if handle and handle["digest"] == digest:
        backend.hand_over()
        session.save_cache_handle(
            provider=cfg.provider,
            model=cfg.model,
            digest=digest,
            name=handle["name"],
            ttl_s=int(handle["ttl_s"]),
            tokens=int(handle["tokens"]),
            # Reusing a cache does not extend its lease. Only the turn that
            # created one gets to set the expiry.
            expires_at=(adopted or {}).get("expires_at") if handle.get("adopted") else None,
        )
    else:
        session.clear_cache_handle()
        backend.close()


def _interpret(
    text: str,
    mode: modesmod.Mode,
    base: Dict[str, Any],
    session: Session,
    ignore: Sequence[str],
    root: str,
    cfg,
) -> Optional[Any]:
    """Turn the raw response into the answer this mode promised.

    A mode that promised JSON and did not produce it is a failed call, not a
    quiet null: `ok: true` beside `triage: null` sends a caller that branched
    on `ok` straight into the rest of its task with no answer.
    """
    base["files_cited"] = _files_cited(text, ignore, root)
    if base.get("dry_run"):
        return None

    if mode.expects_code:
        patches = parse_llm_output(text, console=None)
        base["patches"] = len(patches)
        if not patches:
            excerpt = " ".join(text.split())[:200] or "(nothing)"
            # "It tried and the format was wrong" and "it never tried" look
            # identical from the patch count and need opposite responses.
            if _looks_like_a_patch(text):
                raise PatchNotParseable(cfg.spec, excerpt)
            raise BackendActedAsAgent(cfg.spec, excerpt)
        return None

    if mode.structured:
        result = extract_json(text)
        if not isinstance(result, dict):
            raise SchemaInvalid(
                cfg.provider,
                f"--mode {mode.name} promises JSON and {len(text):,} characters of "
                "something else came back. Usually a truncated answer.",
                session.rel(session.turn_path("response.md")),
            )
        base[mode.name] = result
        return result

    base["answer_head"] = " ".join(text.split())[:240]
    return None


def _files_cited(text: str, ignore: Sequence[str], root: str) -> List[str]:
    """Which project files the answer actually names.

    Costs nothing — `patcher.find_paths_in_text` already extracts valid
    project paths from model prose — and for triage *which files* is the
    entire payload. Handing back an array beats making the caller parse
    English.
    """
    try:
        rels = [os.path.relpath(p, root).replace(os.sep, "/") for p in walk_all(ignore, root)]
    except OSError:  # pragma: no cover
        return []
    return find_paths_in_text(text, rels)[:20]


def _emit(
    args: argparse.Namespace,
    base: Dict[str, Any],
    result: Optional[Any],
    mode: modesmod.Mode,
    text: str,
    session: Session,
) -> None:
    """stdout is the artifact; everything else is narration — spec §8."""
    if args.json:
        emit_json(base)
        return

    if base.get("dry_run"):
        emit(text)  # the payload that would have been sent
    elif result is not None and mode.summary:
        emit(mode.summary(result))
    elif result is not None:
        emit(json.dumps(result, indent=2))
    else:
        emit(text)

    usage = base.get("usage") or {}
    bits = [f"session {base['session']} turn {base['turn']}", f"{base['request']}"]
    if base.get("response"):
        bits.append(base["response"])
    if usage:
        bits.append(
            f"in {usage.get('input', 0):,} (cached {usage.get('cached', 0):,}) "
            f"out {usage.get('output', 0):,}"
        )
    else:
        bits.append(f"~{base['est_input_tokens']:,} est. input tokens")
    if base.get("latency_s") is not None:
        bits.append(f"{base['latency_s']}s")
    narrate("kopipasta: " + " | ".join(bits))
    if session.turn == 1 and not args.session:
        narrate(f"kopipasta: follow up with  --session {base['session']}")


__all__ = ["add_selection_args", "build_parser", "extract_json", "report_failure", "run"]
