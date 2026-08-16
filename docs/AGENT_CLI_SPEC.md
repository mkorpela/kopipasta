# kopipasta as an Agent-Facing CLI

> **This document describes the destination.** It is written as though the tool is finished.
>
> For what is actually built today, see `HANDOFF.md`. For the measurements behind the claims
> here — and the list of things that were not true when assumed — see `AGENT_CLI_FINDINGS.md`.
> Where either disagrees with this document, it is right: this is intent, those are record.

---

## 1. What This Is

kopipasta was built for a world where **the human is the transport**: select files in a TUI, copy
to clipboard, paste into a browser, paste the answer back, apply patches. One context window, one
operator.

An agentic harness has a different bottleneck. Its context window is small, precious, and
degrades as it fills; it reads files one at a time and pays for each of them for the rest of the
session. Two things it therefore cannot do well:

1. **Whole-repo reasoning.** "Which of these 800 files matter?" requires seeing 800 files. An
   agent that reads them all to answer has already lost — the answer arrives in a context too
   polluted to use.
2. **Large coordinated changes.** Harnesses are tuned for many small verified edits. A 400-line
   change across seven files with a consistent design is a shape they produce badly, because each
   edit is decided with only a slice of the picture in view.

kopipasta already owns the hard parts of both: gitignore-aware walking, AST symbol extraction,
budgeted context assembly, and a patch applier tolerant of every format a model emits. What it
adds here is a **non-interactive surface** and **its own context window**.

> kopipasta is a **context oracle**: a separate process holding a large, disposable context
> window, keeping its conversation on disk, returning *pointers* to the caller — never payloads.

| | Agent context | Oracle context |
|---|---|---|
| Size | ~200k, shared with the whole task | 500k–1M+, single-purpose |
| Cost of pollution | permanent for the session | zero — thrown away after |
| Lifetime | the task | one question |
| Lives in | the harness | `.kopipasta/sessions/<id>/` |

The caller spends ~200 tokens to ask a question whose answer required 500k tokens of reading.
That is the entire product.

### The three core workflows

**1. Triage — frontload the codebase, get back a short answer and candidate files.**

```bash
kopipasta ask --all -q "Auth tokens are accepted after expiry. Which files implement
                        validation and expiry, and where is the bug likely?" --json
```

The caller receives a few hundred tokens: a hypothesis, missing context warnings, and a
ranked file list (`relevant_files` + `suggested_selection`).

**2. Distill — pivot focus to the minimal working set.**

```bash
# Headless: feed the triage selection directly back in as active focus
kopipasta ask --from-file selection.txt -m 'kopipasta/**/*.py' \
              -q "Trace the execution path between token validation and session refresh." --json
```

Pivots from whole-repo scanning (500k–1M tokens) to a tight, high-signal focus window
(20k–50k tokens) without re-reading irrelevant files. In the interactive TUI, pasting
triage output (`p`) auto-detects paths and offers to replace the active selection (`[R]eplace`).

**3. Coordinated Multi-File Patch — surgical execution across the subsystem.**

```bash
# 1. Ask the model for a coordinated patch across the zoned subsystem
kopipasta ask -e 'kopipasta/patcher.py' -e 'kopipasta/file.py' \
              -r 'tests/test_patcher*.py' -m 'kopipasta/**/*.py' \
              --mode patch -q "$(cat task.md)" --json

# 2. Apply the returned patch artifact with automated verification & rollback
kopipasta apply current --verify 'pytest -q' --revert-on-fail --json
```

One model call with the whole subsystem in view, zoned editable (`-e`) vs read-only (`-r`),
applied deterministically to disk, verified with an automated test command, and reported as a
diffstat.

---

## 2. Design Principles

1. **Pointers, not payloads.** Nothing large crosses back into the caller's context. Requests,
   responses and answers live in files; stdout carries paths, counts and short summaries.
2. **No hidden interactivity.** Every code path has a non-TTY answer. A prompt with nobody to
   answer it is an unbounded stall inside someone else's subprocess, and a harness cannot tell a
   stall from slow work.
3. **Structured for agents by default.** `--json` everywhere, differentiated exit codes, stdout
   for data and stderr for narration.
4. **The human path does not regress.** Bare `kopipasta` is the TUI. This is a published tool
   with users and muscle memory.
5. **Deterministic before clever.** Selection is explicit and reproducible from argv. No
   embedding search, no hidden RAG — that was always the point of this tool.
6. **Configuration is not conversation.** Which model answers is an operator decision made once,
   not an argument repeated at every call site.

---

## 3. Command Surface

```
kopipasta                            # interactive TUI (default, no subcommand)
kopipasta tui                        # the same, named explicitly
kopipasta ask     [selectors] -q ... # assemble context + ask model + record turn
kopipasta apply   [file|-|current]   # apply patches, check worktree, verify & diffstat
kopipasta map     [selectors]        # symbol skeleton only (cheap whole-repo map)
kopipasta session {ls|show|diff|rm|reap}  # manage on-disk conversations
kopipasta config  --show             # resolved configuration and where each value came from
```

Note what is **absent** from every line: the model and the provider. Those are configuration
(§6). `kopipasta ask -q "..."` is the whole common path. `kopipasta apply` handles patch execution.

Assembling context without calling a model is handled directly by `kopipasta ask --dry-run`
(or `--backend none`).

`ask` prepends the same three memory layers the TUI does — the global profile, `AI_CONTEXT.md`
and `AI_SESSION.md` — because the clipboard prompt is the specification (§13). `--no-memory`
drops all three; `--no-project-context` drops only the constitution.

### Dispatch

If `argv[1]` is a known subcommand, dispatch to it. Otherwise fall through to legacy behaviour
(`kopipasta src/ main.py -t "..."` opens the TUI preselected), because breaking every existing
invocation to buy syntactic purity is a bad trade.

Two rules keep that fall-through from becoming a trap:

- **Path detection is generous.** An existing `Makefile` is a path, not a typo. Breaking a real
  invocation of a published tool is worse than the failure being prevented.
- **A bare word that is neither a path nor a known verb is an error**, not a filename.
  `kopipasta pak --all` exits 1 with "unknown command" rather than opening the TUI on a
  nonexistent file. Verbs that are specced but unbuilt say so specifically — "not yet" and "no
  such thing" send the caller to different places.

---

## 4. Selection Grammar

The three-state selection model in `selection.py` maps one-to-one onto flags. This is what makes
the TUI's core concept work headlessly.

| Flag | State | Rendering | Meaning to the model |
|---|---|---|---|
| `-e, --edit` | Delta | full content | Active workspace. Editable. Attention goes here. |
| `-r, --ref` | Base | full content | Reference. Read for dependencies; do not change. |
| `-m, --map` | Map | AST skeleton | Signatures and first docstring line only. |
| `-s, --snippet` | — | first 50 lines | Coarse peek at a file. |
| `-x, --exclude` | — | dropped | Applied last; wins over everything. |
| `--url` | — | fetched text | With `--url-full` / `--url-snippet` to answer the size question. |

Patterns accept globs (`src/**/*.py`), directories (recursive) and literal paths. `.gitignore`
and binary filtering always apply. Flags are repeatable and order-independent; the last role
assigned to a path wins, so `-m '**/*.py' -e kopipasta/patcher.py` means "skeleton the whole tree,
but give me that one file in full."

**A role a file cannot be rendered in is not a role it keeps.** `-m` is the one role whose
rendering does not exist for every file: a `.md`, a `.sql`, a file that will not parse and a
module with no top-level symbols all extract to nothing, and a skeleton of nothing is invisible —
it reaches the model as `[]` in the structure tree, which the payload's own legend defines as
"not sent at all". A file *named* by `-m` therefore falls back to the cheapest rendering every
file has, its first 50 lines, and is reported under `snippet` with the fallback listed in
`unmappable`. Files dragged in by `--all` keep path-only, because that is what `--all` promises
and 4KB apiece across a repository is not.

Convenience selectors, because agents think in diffs:

```
--all                    # everything not ignored (subject to --budget)
--changed                # git working-tree changes
--changed-since REF      # git diff --name-only REF...HEAD
--from-file PATH         # newline-delimited paths
```

`--from-file` closes the loop: the file list from a triage `ask` feeds directly into the
follow-up `patch`.

---

## 5. Context Budget

Frontloading a full codebase is not literally possible for real repos, even at 1M tokens. The
tool already renders a file three ways; that becomes a budget policy.

```
--budget 400k            # target size in tokens (`c` suffix for literal chars)
--strict-budget          # exit 6 instead of demoting
```

Over budget, files **demote down a ladder** rather than disappear:

```
full content  ->  AST skeleton  ->  path-only (still present in the structure tree)
```

A file with no skeleton skips the middle rung and goes straight to path-only. For that file the
two rungs render identically — to nothing — and only one of the two names is true; reporting
`-> map` would claim a skeleton was sent for a file that left no trace in the payload.

Demotion is deterministic and explainable:

1. Files named by `-e` are **never** demoted. That is the contract of "editable".
2. Then `-r`, largest first.
3. Then everything pulled in by `--all` or directory expansion, largest first.

Every demotion is reported — on stderr, and in `--json` as `demoted`. Silent truncation is what
makes an answer confidently wrong, so the caller must always be able to see what the oracle did
*not* look at.

**The estimator must be honest.** Token counts drive this whole mechanism, and a heuristic that
under-counts silently overshoots the window the flag exists to protect. Count with the provider's
`count_tokens` or a real tokenizer; if a heuristic is used, calibrate it against measured payloads
and bias it pessimistic. (Findings §2.5: the original 3.6 chars/token assumption measured 44% low
on real input.)

**And it must be honest per provider.** One global ratio cannot serve two tokenizers that differ
by nearly 50%: Anthropic measures 2.50 chars/token on this repo's payloads and Gemini 3.42–3.87.
A single pessimistic constant is not a safe compromise — it demoted about a third of what would
have fit on Gemini, in a tool whose product is frontloading. So the ratio comes from the planned
provider (including under `--dry-run`, whose entire job is sizing), an unmeasured provider gets
the pessimistic default, and within a provider the ratio is the lowest measured rather than the
mean. Measured end to end after calibration: 21,854 estimated against 21,772 billed.

`--strict-budget` promises to refuse rather than overshoot, and no heuristic can keep that
promise, so the payload is counted with the provider's own tokenizer before it is sent.

`kopipasta ask --budget 400k --dry-run --json` shows the size and shape of a payload before any money is
spent on it.

---

## 6. Backends and Configuration

### Which model answers is an operator decision

A caller asking "where does token expiry live?" has no basis for choosing between models — that
choice is about cost, context ceiling and which keys you hold, and it does not change between
questions. So it is configuration, not an argument.

| Source | When |
|---|---|
| `--backend gemini:gemini-3.7-flash` | escape hatch — debugging, A/B, pinning a CI job |
| `KOPIPASTA_BACKEND` | per-shell or per-job override |
| `~/.config/kopipasta/config.toml` | **the normal place** — set once |
| built-in default | before anything is configured |

The file sits beside `prompt_template.j2` and `ai_profile.md` and is edited the same way, with
`--edit-config`, which inherits the interaction guard (§12): headless, it exits 8 rather than
blocking on `$EDITOR`.

```toml
[ask]
provider    = "gemini"
model       = "gemini-3.7-flash"
cache_ttl_s = 300
max_tokens  = 8192
timeout_s   = 900

[patch]
provider = "anthropic"
model    = "claude-opus-5"
```

Sections are **per verb, not per call** — triage over a 400k frontload wants a large, cheap
context window; a coordinated patch wants the strongest model available. `[ask]` is the fallback
for any verb without its own section.

**API keys live in the environment, never in this file.** `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`. Configuration holds provider, model and knobs; the secret stays out of a file
on disk, and out of session records.

`kopipasta config --show` prints the resolved values and the source of each. Precedence chains
are precisely the thing that silently does the wrong thing, and "which model actually answered?"
is the first question when an answer looks off. It never prints a key — presence and length are
enough to distinguish "unset", "empty" and "the wrong one".

### The backends

```
exec:<command>          any CLI: stdin -> stdout
claude-cli:<model|->    claude -p, with real usage accounting and enforced schema
anthropic:<model>       POST /v1/messages
gemini:<model>          POST /v1beta/models/{m}:generateContent
openai:<model>          POST /v1/chat/completions  (also OpenRouter, vLLM, Groq, LM Studio…)
```

No vendor SDKs. Every one of these is a single JSON POST in the non-streaming case, over the
`requests` dependency the project already has.

Two properties separate them, and both are load-bearing:

| | Anthropic | Gemini | OpenAI-compatible |
|---|---|---|---|
| Cache control | `cache_control` breakpoint | `cachedContents` resource | none — implicit only |
| Cache lifetime cost | free to abandon | **rented, per token-hour until TTL** | n/a |
| Enforced schema | forced tool + `input_schema` | `responseSchema` | `response_format` |
| Cached-token accounting | `cache_read_input_tokens` | `cachedContentTokenCount` | `prompt_tokens_details` |

OpenAI-compatibility is a *request-shape* convenience, not feature parity — and the two things it
drops are the two the oracle is built on. That is why native adapters exist.

### Three constraints that follow

**The payload is rendered as `(prefix, suffix)`, never one string.** The repo content is a stable
prefix reused across turns; the question is the varying tail. That split *is* the cache
breakpoint, and a renderer that interpolates the task into the middle of the payload destroys
reuse on every turn.

**A rented cache must be owned.** Gemini's `cachedContents` is billed until its TTL expires, so
any code path that creates one is responsible for reusing, superseding and deleting it — with a
sweep on exit and a way to reap orphans. A backend that creates caches and exits leaks money
silently, which is the worst kind of bug because nothing fails.

**An agent CLI must be driven as a completion.** `exec:` and `claude-cli:` borrow a harness that
would rather *do* the task than describe it — handed a file and an instruction it reaches for its
own edit tool and blocks on a permission prompt. Those backends run with file and shell tools
disabled and stdout as the only output channel. A `patch`-mode response containing no code blocks,
especially one mentioning tool permission, is a misconfigured backend (exit 3), not a bad patch
(exit 5).

---

## 7. Sessions on Disk

Conversation state lives in the repo, not the clipboard and not process memory.

```
.kopipasta/                          # auto-added to .gitignore on first write
  sessions/
    2026-08-15-a3f9/
      meta.json                      # id, model, backend, project root, git head, totals
      transcript.jsonl               # append-only index of turns
      001-request.md                 # the exact payload sent
      001-response.md                # the raw response
      001-meta.json                  # usage, latency, stop reason, applied/failed files
      selection.json                 # resolved selection + content hashes per turn
  current                            # human convenience pointer; agents pass --session
```

In-repo rather than XDG state because the caller's cwd *is* the repo, every path in an answer is
repo-relative, and the agent can grep and read these artifacts with the tools it already has.
`rm -rf .kopipasta` is a complete reset.

### Turn-level deduplication

`--session <id>` continues a conversation. kopipasta knows what it sent on earlier turns and each
file's content hash at the time:

- Sent before and unchanged → not resent; referenced by path.
- Sent before and changed → resent, noted as superseding the earlier copy.
- New → sent.

This is the Base/Delta distinction doing conversation-level dedup. Combined with prefix caching it
is what makes multi-turn triage affordable **across a session's lifetime**. Rapid bursts are a
different matter (findings §2.4), so per-turn savings are never promised in the output.

### Session Defaults & Resumption

`kopipasta ask` **always starts a fresh session by default**. A disposable context oracle
should be disposable by default; implicit resumption between separate commands risks
accidental context pollution across distinct tasks.

To continue a conversation:
- `kopipasta ask --session <id> -q "..."` (resumes a specific session by ID)
- `kopipasta ask --continue -q "..."` (resumes the session pointed to by `current`)

The `current` pointer is written on every run as a convenient handle for inspection and
tooling (e.g. `kopipasta apply current`), but `ask` will not resume it unless `--continue`
or `--session` is explicitly specified.

`kopipasta session` follows the same asymmetry. Its reporting subcommands — `ls`, `show`,
`diff` — default to `current` when given no id, because reading is cheap and a stale pointer
costs nothing. **`rm` does not**: "delete the thing I did not name" is not a default anything
should have. Ids are validated against a character whitelist as well as the usual `..` and
absolute-path checks, since `rm` is the one path in the package that removes a tree.

`session reap` hands back provider caches this project rented that no live session is holding,
and is **scoped to the current project with no flag to widen it**. A lease lives in the project
that took it, so a machine-wide sweep could read one project's leases and delete another's live
cache — the §7 money bug, one scope up. The asymmetry decides it: an abandoned cache costs
storage rent bounded by its TTL, a destroyed live one costs a full re-creation on every
following turn. Crash recovery does not need the wider scope, because the leases are still on
disk in the project that took them.

---

## 8. Output Contract

**stdout is the artifact. Everything else is narration and goes to stderr.**

This is enforced structurally rather than by auditing print statements: the whole run executes
with `sys.stdout` pointed at `sys.stderr`, and the one thing that is genuinely output is written
to the saved real handle. Third-party libraries on the path have no such contract, and one
redirect converts every call site at once, including theirs.

Consequences worth stating:

- `kopipasta > prompt.txt` yields the prompt, not a banner and a file list.
- Redirecting the artifact does not disable the TUI — the interactivity check looks at the stream
  narration goes to, so keyboard and display are still present when only stdout is redirected.
- `--help` is output, not narration.

Default stdout is the artifact or a compact summary; `--json` makes stdout a single JSON object.

```json
{
  "ok": true,
  "session": "2026-08-15-a3f9",
  "turn": 2,
  "request": ".kopipasta/sessions/2026-08-15-a3f9/002-request.md",
  "response": ".kopipasta/sessions/2026-08-15-a3f9/002-response.md",
  "usage": {"input": 412330, "cached": 389100, "output": 1840},
  "sent": {"edit": 2, "ref": 14, "map": 380, "demoted": 22},
  "answer_head": "Token expiry is validated in two places...",
  "patches": 2,
  "files_cited": ["kopipasta/patcher.py", "kopipasta/file.py"]
}
```

`files_cited` costs nothing: `patcher.find_paths_in_text` already extracts valid project paths
from model prose. For triage, *which files* is the payload — handing back an array beats making
the caller parse English.

### Exit codes

| Code | Meaning | Retry? |
|---|---|---|
| 0 | success | — |
| 1 | usage or configuration error | no |
| 2 | no usable backend — no key, no command | no |
| 3 | backend error or timeout | yes |
| 4 | patch **partially** applied — worktree dirty, inspect `failed` | no |
| 5 | patch fully failed — worktree untouched | maybe |
| 6 | budget exceeded under `--strict-budget` | no |
| 7 | `--verify` command failed | no |
| 8 | interaction required, no human attached | no |

Two carry most of the weight. **4** is the difference between "retry" and "you have a mess to
clean up". **8** is distinct from 1 because the fix differs: the caller needs a policy or a
different invocation, not a corrected command line.

---

## 9. Error Messages

Both consumers are bad at guessing. A human types the command once a week and does not remember
the flags; an agent cannot guess at all, and will either retry a permanent failure forever or
abandon a task over a blip. Every failure states **what failed, why, and the next action** — in
that order.

1. **Name the exact thing.** `GEMINI_API_KEY`, `~/.config/kopipasta/config.toml`, `--budget` —
   never "check your credentials".
2. **Quote the provider verbatim.** Paraphrasing an upstream error destroys the detail that
   identifies it. Give our diagnosis *and* their text.
3. **Separate misconfiguration from transient failure.** That is what the retry column above is
   for, and `--json` states it outright rather than making an agent infer it.
4. **Report what resolved, not only what was missing.** Half of credential bugs are the wrong
   config winning. `provider=gemini (from config.toml [ask]) needs GEMINI_API_KEY` beats
   `GEMINI_API_KEY unset`.
5. **stdout stays empty on failure.** A partial artifact is worse than none — the caller cannot
   tell it is partial.
6. **Suggest the fix, not the concept.** Show the command or the two lines of TOML.

```
kopipasta: no API key for provider 'gemini'.
  Resolved from ~/.config/kopipasta/config.toml [ask]; GEMINI_API_KEY is unset.

  export GEMINI_API_KEY=...
  Or switch provider:  kopipasta --edit-config
  See what resolved:   kopipasta config --show
```

```json
{"ok": false, "error": "no_api_key", "exit": 2, "provider": "gemini",
 "resolved_from": "~/.config/kopipasta/config.toml [ask]",
 "missing_env": "GEMINI_API_KEY", "retryable": false,
 "hint": "export GEMINI_API_KEY=..."}
```

`error` is a stable slug and `retryable` is explicit, so neither has to be inferred from prose.

### Two failures that need their own treatment

**An empty selection is an error, not a warning.** A typo'd glob selects nothing, the model
answers from the project structure alone, and the answer reads fine. No error, no signal, a
plausible answer produced from nothing — the worst failure shape in the tool. Report per pattern,
because with several selectors "0 files" does not say which one was wrong:

```
kopipasta: no files matched.
  -e kopipasta/pacher.py   0 files   (did you mean kopipasta/patcher.py?)
  -m kopipasta/*.py       16 files
Nothing was selected, so there is nothing to ask about.
```

**Truncation is never success.** A response that stopped at `max_tokens` is a failure with its own
message, however much text came back. Under an enforced schema a truncated response is JSON
ending mid-string; reported as success it becomes a null field and a caller with no idea why.
Reasoning tokens also spend the output budget, so a generous limit is not a guarantee.

---

## 10. Modes

`--mode` swaps the prompt template and the expected response shape. Templates live alongside
`prompt_template.j2` and stay user-editable.

**`--mode triage`** — the default for `ask`, and the reason the whole thing is worth building.
Prose is the wrong interface when the answer is "which files"; the schema is enforced by the
provider, not requested politely.

```json
{
  "relevant_files": [
    {"path": "kopipasta/patcher.py", "why": "owns hunk matching", "confidence": 0.9}
  ],
  "hypothesis": "Fuzzy matching re-indents...",
  "missing_context": ["tests/fixtures/"],
  "suggested_selection": ["kopipasta/patcher.py", "kopipasta/file.py"]
}
```

Three things the field set encodes, each from observed failure (findings §2.6, HANDOFF §4):

- **`missing_context` is surfaced first, not buried.** Every wrong answer named the relevant file
  as absent. It is the built-in confidence check — a confident claim about a file the model never
  read is a guess wearing a score.
- **File-level attribution is reliable; line numbers are not.** Line numbers are excluded or
  marked unverified. A wrong `file.py:412` at 0.95 confidence is worse than `file.py`, because it
  reads as a citation.
- **Permission to answer "none"** belongs in the template, not in every question typed by hand.

`suggested_selection` feeds straight into `--from-file` for the follow-up call.

Other modes — `review`, `explain`, `plan` — are the same machinery with a different template and
schema.

---

## 11. Patch Safety (`kopipasta apply`)

Patch application is a separate, dedicated command (`kopipasta apply [TARGET]`) that accepts
input from a file path, `-` (stdin), or `current` (the latest session's `response.md`).

A large patch landing unattended needs the guarantees a human review would have given:

- **Clean worktree by default**; refuse otherwise (`--dirty-ok` to override). This is the cheapest
  possible undo — `git diff` to review, `git checkout .` to revert — and it is what makes a
  400-line one-shot patch safe to try.
- `--dry-run` renders the diff that would be applied and touches nothing.
- Deletes require `--allow-delete`. Never delete on a model's say-so alone.
 The shrink/hallucination guard declines suspicious overwrites by default; `--force` overrides.
- The shrink/hallucination guard declines the file by default; `--force` overrides.
- `--verify 'pytest -q'` runs after applying, with `--revert-on-fail` to restore via git.
- ~~`--commit [msg]` returns a revertable SHA in the JSON.~~ **Cancelled.** A tool that assembles
  context does not need to write git history, and `apply && git commit` is one line the caller
  already knows how to write. Everything it would need is in place — `PatchResult` names exactly
  the files this run touched, so it could stage those and never `git add -A` — so this is a
  scope decision, not a difficulty one.

Two constraints any implementation must honour:

- **A policy refusal must not raise inside the applier.** Its per-file body catches broad
  exceptions, so a refusal thrown from within would be swallowed and reported as a corrupt patch,
  sending the caller to debug something that was fine. Decisions are injected, not raised.
- **An opt-in flag does not suppress the prompt for a human who is present.** `--allow-delete`
  means "this run may delete", not "delete without telling me". The flag answers the question only
  when there is nobody to ask.

---

## 12. Interaction Policy

The guard lives in one module (`interaction.py`) rather than at each call site, so a prompt added
later inherits it by calling one function. It is consulted **before** anything is rendered.

`human_attached()` is false unless both stdin and the narration stream are ttys, and is forced
false by `KOPIPASTA_NONINTERACTIVE=1` or `CI`. When in doubt it assumes nobody is watching:
falsely refusing to prompt produces a clear error, falsely prompting produces a hang.

**The policy is deliberately not uniform.** Two kinds of question:

| | Rule | Examples |
|---|---|---|
| No safe default | **Refuse** (exit 8), naming the flag that avoids the question | which files, what task, full page or snippet, open `$EDITOR` |
| Safe default exists | **Apply it**, narrate on stderr, keep running | mask the secret, decline the delete, skip the suspicious patch |

A guard that refused uniformly would be simpler and would make the tool useless in CI the moment a
`.env` existed — for no safety gain, since masking leaks nothing. Guessing is worse than refusing
only when there is nothing to guess from.

Two structural rules keep this from decaying:

- **A recurring failure must terminate.** `except Exception: …; continue` around a blocking read is
  an infinite-loop generator. Unrecoverable errors abort immediately; anything else gets a small
  number of attempts and then a hard stop.
- **Naming the TUI explicitly is not consent to hang.** `kopipasta tui` still goes through the
  guard. This is the same reason moving the TUI behind a subcommand was rejected as the primary
  defence against accidental launches: prevention has to live where the blocking happens, not in
  the argument grammar.

Bounded runtime is a separate axis: `--timeout` caps a single backend call, and a global
`--deadline` caps the whole invocation regardless of which stage misbehaves.

---

## 13. Architecture

The logic the CLI needs must not live in the TUI. Both the TUI and the CLI are thin views over a
core that never prompts:

```
kopipasta/
  interaction.py   # is a human attached, and what to do when not
  output.py        # stdout is the artifact; everything else is stderr
  core/
    resolver.py    # patterns -> resolved selection (role + render mode)
    budget.py      # the demotion ladder
    context.py     # resolved selection -> rendered (prefix, suffix)
    session.py     # on-disk conversation: turns, dedup, hashes, meta
    backend.py     # exec / claude-cli / anthropic / gemini / openai
    patchflow.py   # parse -> validate -> apply -> report
```

**The clipboard prompt is the specification.** It is the one a human has read, tuned and come to
trust; `ask` sends the same thing to the same models with nobody there to notice a difference. So
the TUI's shape is canonical and `ask` conforms to it — never the reverse. Stated as an assertion,
which is how it is tested:

```
ask payload == clipboard prompt, with the instruction tail swapped for the --mode tail
```

Everything above the tail is the same bytes: the three memory layers, the structure tree, the
legend, the zones. Only the tail may differ, and only because the clipboard has a human who can
answer a question and `ask` does not. `context.py` renders all of it — `render_memory` for the
prologue, `render_context` for the body — and the TUI's template composes those two rather than
rebuilding them.

The TUI's three-state engine and the selection grammar are one model in two vocabularies, and
they resolve to the same roles:

```
Delta (green)  ->  edit      active workspace, editable
Base  (cyan)   ->  ref       synced context, read-only
Map   (yellow) ->  map       skeleton only
snippet        ->  snippet   first 50 lines, whichever state selected it
```

That correspondence was already load-bearing — the Ralph loop hands an agent Delta as editable
and Base as read-only — but the pasted prompt flattened it into one undifferentiated
`## File Contents` list, so the model was never shown the boundary the tool enforces everywhere
else. Two renderers for one prompt is the drift this file exists to prevent; a test asserts the
shared body is byte-identical from both entry points.

Two rules for anything added here:

- **Route through the existing helpers.** The walker, the symbol extractor, the structure renderer
  and the patcher are the assets; a new surface that reimplements one of them will reintroduce a
  bug that was already fixed there.
- **State is per project.** Caches are keyed by project root — the nearest ancestor containing
  `.git`, else cwd — with stored paths relative to that root, and case folding applied only where
  the filesystem is genuinely case-insensitive. A single global cache silently cross-contaminates
  repos, because relative paths from one project frequently exist in another.

---

## 14. What Does Not Change

- Bare `kopipasta` is the TUI. The human loop keeps working.
- `.gitignore` respect, binary filtering, secret masking, hallucination guards.
- The patcher's format tolerance — unified diff, search/replace, full file, delete, `<<<RESET>>>`,
  and a block the model never fenced. This is the asset that makes one-shot large patches land at
  all, and every new surface routes through it rather than around it. Tightening a prompt to
  avoid a parse failure is the opposite move: it asks the model to be careful instead of making
  the tool tolerant, and it only holds until the next model.

  The unfenced case marks the edge of that tolerance, though. With no closing fence there is no
  end marker, so it accepts only text carrying an unmistakable patch marker and never a
  whole-file replacement — `# FILE: x` followed by a model explaining itself must not overwrite
  `x` with the explanation.
- Explicit context control. The selection is still yours; it just comes from argv instead of arrow
  keys.

---

## 15. Open Questions

- **Cost is easy to hide.** A 500k-token frontload is real money and agents call things in loops.
  `ask --dry-run --json` shows the size before spending and `--max-cost` refuses above a threshold, but an
  accidental `while true; do kopipasta ask --all; done` must not be cheap to write by mistake.
- **Cache economics at target scale.** Reuse is proven at ~20k tokens against a design targeting
  400k–1M. Larger caches cost more to hold, and the TTL is an uncosted guess: nobody has priced
  frontload-and-idle against simply re-sending.
- **What makes a cache figure quotable.** The explicit-caching number is stable across runs and
  machines; the implicit one moved between two machines with nothing changed on our side. Decide
  the required *n* before quoting either.
- **Two-model failure modes.** When the oracle is wrong the caller inherits a confident wrong
  answer with none of the evidence. `--json` must always carry `demoted` and the request path so
  the caller can see what the oracle actually looked at.
- **Does `AI_SESSION.md` survive?** `AI_CONTEXT.md` clearly stays and gets *more* valuable — it is
  the cheapest way to give a disposable oracle the project's non-obvious rules. `AI_SESSION.md`
  overlaps with `.kopipasta/sessions/`; keep it on the human path for now and revisit with real
  usage.
- **Should `ask` select its own context?** An `--auto-select` doing a cheap map-only pass before a
  full one would be useful, and is also exactly the hidden magic this tool exists to avoid.
  Deferred until the explicit path is proven.
