# Specification: kopipasta as an Agent-Facing CLI ("Context Oracle")

## 1. The Repositioning

`kopipasta` was built for a world where **the human is the transport**: select files in a TUI,
copy to clipboard, paste into a browser, paste the answer back, apply patches. That world had
one context window (the human's chat) and one operator (the human).

The agentic harness world has a different bottleneck. The agent (Claude Code, Codex, Cursor,
an SDK loop) has a *small, precious* context window that degrades as it fills. It reads files
one at a time and pays for every one of them, permanently, for the rest of the session. Two
things it structurally cannot do well:

1. **Whole-repo reasoning.** "Which of these 800 files matter for this task?" requires seeing
   800 files. An agent that reads 800 files to answer that has already lost — the answer
   arrives in a context too polluted to use it.
2. **Large coordinated changes.** Harnesses are optimized for many small, verified edits. A
   400-line change spanning 7 files with a consistent design is a shape they produce badly,
   because each edit is decided with only a slice of the picture in view.

`kopipasta` already owns the hard parts of both: gitignore-aware repo walking, AST symbol
extraction, budgeted context assembly, and a battle-tested multi-format patch applier. What it
lacks is a **non-interactive surface** and its **own context window**.

### The new mental model

> `kopipasta` is a **context oracle**: a separate process that holds a large, disposable
> context window, keeps its conversation on disk, and returns *pointers* to the calling agent —
> never payloads.

| | Agent context | kopipasta context |
|---|---|---|
| Size | ~200k, shared with the whole task | 500k–1M+, single-purpose |
| Cost of pollution | permanent for the session | zero, thrown away after |
| Lifetime | the task | one question |
| Lives in | the harness | `.kopipasta/sessions/<id>/*.md` |

The calling agent spends ~200 tokens to ask a question whose answer required 500k tokens of
reading. That is the entire product.

### The two target invocations

**A. Triage / hypothesis (frontload the codebase, get back a short answer)**

```bash
kopipasta ask --all --map '**/*.py' \
  -q "Auth tokens are occasionally accepted after expiry. Which files implement token
      validation and expiry, and what is the most likely location of the bug?" \
  --json
```
→ agent receives ~300 tokens: a ranked file list, a hypothesis, and a path to the full answer.

**B. One-shot large patch**

```bash
kopipasta patch -e 'kopipasta/patcher.py' -e 'kopipasta/file.py' \
  -r 'tests/test_patcher*.py' -m 'kopipasta/**/*.py' \
  -q "$(cat task.md)" --verify 'pytest -q' --json
```
→ one model call with the whole relevant subsystem in view, one coherent multi-file patch,
applied deterministically, verified, reported as a diffstat.

---

## 2. Design Principles

1. **Pointers, not payloads.** Nothing large ever crosses the process boundary into the
   agent's context. Answers, requests, and responses live in files; stdout carries paths,
   counts, and short summaries.
2. **No hidden interactivity.** Every code path must have a non-TTY answer. A prompt with no
   human attached is a hang, and a hang in a subprocess is the worst failure mode there is.
3. **Structured by default for agents.** `--json` on every command; differentiated exit codes;
   stderr for narration, stdout for data.
4. **The human path does not regress.** Bare `kopipasta` stays the TUI. This is a published
   tool with users and muscle memory.
5. **Deterministic before clever.** Selection is explicit and reproducible from argv. No
   embedding search, no hidden RAG — that was always the point of this tool.

---

## 3. Command Surface

```
kopipasta                            # unchanged: interactive TUI (default, no subcommand)
kopipasta pack    [selectors]        # assemble context payload -> file. No model call.
kopipasta ask     [selectors] -q ... # pack + call model + record turn -> answer on disk
kopipasta patch   [selectors] -q ... # ask --apply: apply the returned patch to the worktree
kopipasta apply   <file|->           # apply an existing LLM response. No model call.
kopipasta map     [selectors]        # symbol skeleton only (cheap whole-repo map)
kopipasta session {new|ls|show|end}  # manage on-disk conversations
```

`patch` is exactly `ask --apply`; it exists as its own verb because agents read `--help` and
verbs are cheaper to discover than flags.

### Backwards compatibility

Dispatch rule: if `argv[1]` is a known subcommand, dispatch to it; otherwise fall through to
the current legacy behaviour (`kopipasta src/ main.py -t "..."` opens the TUI preselected).
Ugly but correct — breaking every existing invocation to buy syntactic purity is a bad trade.

`pack` and `apply` matter independently of any model integration: together they are a two-command
version of the whole loop that works with *any* model access, including none at all. They are
also the honest primitives that keep the "no black boxes" promise intact.

---

## 4. Selection Grammar

The existing three-state selection model (`kopipasta/selection.py`) maps one-to-one onto flags.
This is the piece that makes the TUI's core concept work headlessly.

| Flag | State | Rendering | Meaning to the model |
|---|---|---|---|
| `-e, --edit PATTERN` | Delta | full content | Active workspace. Editable. Attention goes here. |
| `-r, --ref PATTERN` | Base | full content | Reference. Read for dependencies, do not change. |
| `-m, --map PATTERN` | Map | AST skeleton | Signatures + first docstring line only. |
| `-s, --snippet PATTERN` | — | first 50 lines | Existing snippet mode. |
| `-x, --exclude PATTERN` | — | dropped | Applied last; wins over everything. |
| `--url URL` | — | fetched text | Existing web fetch, `--url-full` / `--url-snippet` to skip the prompt. |

Patterns accept globs (`src/**/*.py`), directories (recursive), and literal paths. `.gitignore`
and binary filtering always apply. Flags are repeatable and order-independent; the last
role assigned to a path wins, so `-m '**/*.py' -e 'kopipasta/patcher.py'` means "skeleton the
whole tree, but give me one file in full."

Convenience selectors, because agents already think in diffs:

```
--all                    # everything not ignored (subject to --budget)
--changed                # git working-tree changes
--changed-since REF      # git diff --name-only REF...HEAD
--from-file PATH         # newline-delimited paths (feed it a previous triage answer)
```

`--from-file` closes the loop: the file list produced by a triage `ask` is directly consumable
as the selection for the follow-up `patch`.

---

## 5. The Budget Ladder

"Frontload the full codebase" is not literally possible for real repos, even at 1M tokens. The
tool already knows how to render a file three ways; make that a budget policy.

```
--budget 400k            # target size in tokens (or chars with a `c` suffix)
--strict-budget          # exit 6 instead of demoting
```

When full rendering exceeds the budget, files **demote down a ladder** rather than disappear:

```
full content  ->  AST skeleton  ->  path-only (present in the structure tree)
```

Demotion order is deterministic and explainable:

1. Files named by `-e` are **never** demoted (that is the contract of "editable").
2. Then `-r`, largest-first.
3. Then everything pulled in by `--all` / directory expansion, largest-first.

Every demotion is reported (stderr, and in `--json` as a `demoted` array) so the agent knows
what it did *not* see. Silent truncation is the failure mode that makes a triage answer
confidently wrong.

`kopipasta pack --budget 400k --json` with no model call is the cheap way to see the shape and
cost of a payload before paying for it.

### The estimator must be recalibrated first

`ops.estimate_tokens` assumes **3.6 chars/token**. Measured against a real payload from this
repo (46,102 chars, differenced across two `claude -p --output-format json` runs to isolate the
payload from the harness prefix): **~18,474 actual tokens, i.e. 2.50 chars/token.** The
estimator reported 12,806 — **44% low**.

That is not a rounding error, it is the budget feature silently failing: `--budget 400k` would
ship roughly 576k tokens and blow the window it exists to protect. Dense code and the minified
JSON structure blob tokenize far worse than prose, and this payload is mostly both.

Before shipping the ladder, either recalibrate the constant against measured payloads (a repo
of Python plus a JSON tree lands near 2.5), or — better — count properly: the provider
`count_tokens` endpoints are cheap and exact, and a local `tiktoken`/`tokenizers` pass is exact
for the OpenAI-family case. Under-counting is the dangerous direction; if the estimate stays
heuristic, bias it pessimistic.

---

## 6. Sessions on Disk

Conversation state moves out of the clipboard and out of process memory into the repo.

```
.kopipasta/                          # auto-added to .gitignore on first write
  sessions/
    2026-08-15-a3f9/
      meta.json                      # id, model, backend, project root, git head, totals
      transcript.jsonl               # append-only index of turns
      001-request.md                 # the exact rendered payload that was sent
      001-response.md                # raw model response
      001-meta.json                  # usage, latency, stop reason, applied/failed files
      002-request.md
      ...
      selection.json                 # resolved selection + content hashes per turn
  current                            # human convenience pointer; agents pass --session
```

Rationale for in-repo rather than XDG state: the agent's cwd is the repo, every path in an
answer is repo-relative, and the agent can `grep`/`read` these artifacts with the tools it
already has. `rm -rf .kopipasta` is a complete reset.

### Turn-level context deduplication (Base/Delta, reused)

`--session <id>` continues an existing conversation. On turn N, kopipasta already knows what it
sent on turns 1..N-1 and the content hash of each file at that time. So:

- A file already sent and **unchanged** → not resent; referenced by path only.
- A file already sent and **changed** (e.g. we just patched it) → resent as Delta, with a note
  that it supersedes the earlier copy.
- A new file → sent as Delta.

This is exactly the Base/Delta distinction that existed for clipboard economy, now doing
conversation-level dedup. Combined with prompt caching (§7) it makes multi-turn triage cheap:
turn 1 pays for the repo, turns 2..N pay for a question.

### Concurrency

Agents run things in parallel. Session directories are unique by construction; the `.kopipasta/current`
pointer is racy and is therefore **human-only**. In `--json` mode, omitting `--session` always
creates a fresh session rather than resuming `current`.

---

## 7. Backends

### Phase 1: `exec:` — no new dependencies, no API keys

Every harness the user cares about already has a CLI entry point that accepts a prompt on stdin
and writes an answer to stdout.

```bash
kopipasta ask --backend 'exec:claude -p --model claude-opus-5' ...
kopipasta ask --backend 'exec:codex exec' ...
kopipasta ask --backend 'exec:llm -m gemini-2.5-pro' ...
```

Contract: the rendered request is written to `NNN-request.md` and piped to the command's stdin;
stdout is captured verbatim to `NNN-response.md`; a non-zero exit is a backend error (exit 3).
Timeout via `--timeout`, default 900s.

This is the single highest-leverage decision in this plan. It delivers **both** target use cases
with zero new dependencies, zero API-key custody, and zero provider abstraction to maintain —
and it lets the calling agent choose the oracle's model per invocation. Ship this first.

#### The oracle must be a completion, not an agent

Verified the hard way in the spike (`spike/oracle.py`). The first `patch` run returned no code
at all:

> *"The edit tool needs permission approval from you to modify `kopipasta/prompt.py`."*

`claude -p` is an **agent**. Handed a task and a file, it reaches for its own Edit tool instead
of emitting a patch — then blocks on a permission prompt that no one will ever answer. The
harness we are borrowing wants to do the work, not describe it. Same payload with tools
disabled produced a clean four-hunk patch on the first try.

So the `exec:` contract has a second clause: **the command must be invoked in completion mode,
with file and shell tools off.** The oracle's only output channel is stdout.

```
claude    exec:claude -p --disallowedTools "Edit,Write,Read,Bash,Glob,Grep,Task,..."
codex     exec:codex exec --sandbox read-only
llm       exec:llm -m <model>            # no tools by default
```

kopipasta should ship these as named presets (`--backend claude-cli`) rather than making every
caller remember the flag list, and should **detect the failure** rather than returning an empty
patch: a `patch`-mode response containing no code blocks, especially one mentioning permission
or tool approval, is a backend misconfiguration — exit `3` with that diagnosis, not exit `5`
("model produced a bad patch"). The two failures have completely different fixes.

### Phase 2: raw model APIs — the layer below agent CLIs

`exec:` borrows an *agent*. For the oracle role we actually want the layer beneath: a single
completion, no tool loop, no permission negotiation, real token accounting, and direct control
over the two features that make this whole design economical.

**Zero new dependencies.** `requests` is already a kopipasta dependency, and every one of these
APIs is a single JSON POST in the non-streaming case. Each adapter is ~40 lines. Do not pull in
three vendor SDKs to send three JSON bodies.

```
--backend anthropic:claude-opus-5       ANTHROPIC_API_KEY   POST /v1/messages
--backend gemini:gemini-3-pro           GEMINI_API_KEY      POST /v1beta/models/{m}:generateContent
--backend openai:gpt-5                  OPENAI_API_KEY      POST /v1/chat/completions
--backend openai:<model> --base-url ... any OpenAI-compatible server
```

`openai:` is the wide net — OpenRouter, Groq, Together, Fireworks, vLLM, llama.cpp, LM Studio,
and Gemini itself, which exposes an OpenAI-compatible endpoint at
`https://generativelanguage.googleapis.com/v1beta/openai/` (the trailing slash matters; without
it you get a 404).

#### Why native adapters exist at all, given the compat layer

Because OpenAI-compatibility is a *request-shape* convenience, not feature parity — and the two
features it drops are precisely the two the oracle is built on.

| | Anthropic native | Gemini native | OpenAI-compatible |
|---|---|---|---|
| Explicit cache breakpoint | `cache_control: ephemeral` on a content block | `cachedContents` resource | **none** — implicit, no control |
| Server-enforced schema | forced single tool + `input_schema` | `responseSchema` + `responseMimeType` | `response_format: json_schema` |
| Cached-token accounting | `usage.cache_read_input_tokens` | `usageMetadata.cachedContentTokenCount` | `usage.prompt_tokens_details.cached_tokens` |
| Context ceiling | large | **1M+ — the reason this adapter exists** | varies |

**Caching is the whole economics of multi-turn.** The repo payload is a stable prefix reused
verbatim across turns; the question is the only thing that varies. So the payload must be
rendered as **`(prefix, suffix)`, not one string** — the split *is* the cache breakpoint, and a
renderer that interpolates the task into the middle of the payload destroys cache reuse on every
turn. This is a constraint on §6's dedup design, not an afterthought.

**Server-enforced schema is what makes triage mode real.** With `exec:`, §10's schema is a
polite request in a prompt. With Gemini `responseSchema` or OpenAI `json_schema`, it is a
guarantee. Anthropic has no `response_format`, but a single forced tool with an `input_schema`
is exactly equivalent and enforced the same way.

One trap, found in the spike: **the schema and the prompt template must describe the same
shape.** Where the provider enforces the schema, the schema wins — a flatter schema than the
template silently drops fields (we lost `why` and `confidence` off every cited file that way).

#### `claude-cli:` — the middle rung, and what a CLI oracle really costs

Two of the things §7 gives up under `exec:` are not inherent to CLI-backed oracles, they were
just flags we had not read. `claude -p` supports:

- `--output-format json` → real `usage` (input / cache_creation / cache_read / output) **and
  `total_cost_usd`**.
- `--json-schema '<schema>'` → server-enforced structured output, returned parsed under
  `structured_output`. Triage mode works properly here, no prompt-begging.

So `claude-cli:<model>` is a legitimate third rung: zero key custody, real accounting, enforced
schema. What it still cannot do is place the cache breakpoint — and it carries a fixed tax.

**Measured in this sandbox**, differencing a tiny prompt against a 46k-char payload:

| | input | cache_creation | cache_read | cost |
|---|---|---|---|---|
| `"hi"` | 2 | 5,099 | 29,280 | **$0.0396** |
| 46k-char repo payload | 2 | 23,573 | 29,280 | $0.1648 |

Read it carefully:

- **~29.3k tokens of harness system prompt on every single call**, plus ~5.1k created — a
  ~34k-token floor before your payload exists. "hi" costs four cents.
- `input_tokens` is **2 in both runs** and is useless as a measure of payload size — the CLI
  routes everything through cache blocks. Do not report it as "our input"; the payload only
  shows up in the `cache_creation` delta.
- For a 400k-token frontload the ~34k floor is ~8% overhead. For the cheap follow-up turns that
  make multi-turn sessions attractive, it is *most of the cost*.

That tax is the real argument for the raw `anthropic:` adapter, more than any feature gap: same
model, none of the harness prefix, and the cache breakpoint lands where we put it.

#### Choosing

- **Gemini** for use case A. 1M+ context is the only honest way to frontload a large repo, and
  `responseSchema` makes the triage answer machine-consumable without parsing prose.
- **Anthropic** for use case B and any multi-turn session. Explicit cache breakpoints are the
  best available control for the stable-prefix pattern, which is what a session *is*.
- **`openai:` + `--base-url`** for everything else, including local models — accepting that you
  lose cache control and get whatever implicit caching the server does.
- **`exec:`** when you want zero key custody, or specifically want the model your harness is
  already authenticated against.

Also needed regardless of provider: streaming to the response file (a long generation stays
tailable, and a crash leaves a partial artifact instead of nothing), retry with backoff on
429/5xx, and a hard `--timeout`. Keys come from the environment only — never read from, and
never written to, session files.

---

## 8. Output Contract

Rules: **stdout is data, stderr is narration.** Default stdout is a compact summary; `--json`
makes stdout a single JSON object and pushes everything else to stderr.

`ask --json`:

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
  "files_cited": ["kopipasta/patcher.py", "kopipasta/file.py"]
}
```

`files_cited` is free: `find_paths_in_text` already exists (`kopipasta/patcher.py:344`) and was
built for exactly this — extracting valid project paths from LLM prose. For triage, *which
files* is the payload; handing back a clean array beats making the caller parse prose.

`patch --json`:

```json
{
  "ok": false,
  "applied": ["kopipasta/patcher.py", "kopipasta/file.py"],
  "created": ["kopipasta/core/budget.py"],
  "deleted": [],
  "failed": [{"file": "kopipasta/prompt.py", "hunk": 2, "reason": "context not found"}],
  "diffstat": {"files": 3, "insertions": 412, "deletions": 98},
  "verify": {"command": "pytest -q", "exit": 1, "log": ".kopipasta/sessions/.../003-verify.log"},
  "response": ".kopipasta/sessions/.../003-response.md"
}
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage / configuration error |
| 2 | no usable backend (no key, no command) |
| 3 | backend error or timeout |
| 4 | patch **partially** applied (worktree is dirty — inspect `failed`) |
| 5 | patch fully failed (worktree untouched) |
| 6 | budget exceeded under `--strict-budget` |
| 7 | `--verify` command failed |

Exit code 4 is the one that matters: it is the difference between "retry" and "you have a mess
to clean up," and an agent must be able to branch on it without parsing prose.

---

## 9. Patch Safety Without a Human

`apply_patches` currently asks a human before deleting (`kopipasta/patcher.py:914`) and before
suspicious full-file overwrites (`kopipasta/patcher.py:1035`). With no human attached, those
prompts must become policy:

- **Clean worktree required** by default; refuse with exit 1 otherwise (`--dirty-ok` to override).
  This is the cheapest possible undo: `git diff` to review, `git checkout .` to revert. It costs
  nothing to implement and it makes a 400-line one-shot patch safe to try.
- `--dry-run` renders the diff that *would* be applied and touches nothing. Essential for letting
  the calling agent review a large patch before it lands.
- Deletes require `--allow-delete`. Never silently delete a file on a model's say-so.
- The shrink/hallucination guard becomes a **hard failure** in non-interactive mode, not a prompt.
  `--force` to override.
- `--verify 'pytest -q'` runs after apply; `--revert-on-fail` restores via git if it fails. This
  is the good idea inside the Ralph loop, extracted from the MCP/Claude-Desktop machinery into a
  plain flag.
- `--commit [msg]` commits the applied patch, so the agent gets a revertable SHA back in `--json`.

---

## 10. Triage Mode (structured answers)

For use case A, prose is the wrong interface. `--mode triage` swaps the prompt template for one
that demands a schema, validates the result, and returns it inline:

```bash
kopipasta ask --all --budget 500k --mode triage -q "..." --json
```

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

`suggested_selection` feeds straight into `--from-file` for the follow-up call. Once the pipeline
of §3–§8 exists, this is a template plus a schema validator — the highest value-per-line feature
in the whole plan.

Other useful modes: `--mode review`, `--mode explain`, `--mode plan`. Same machinery, different
template. Templates live alongside the existing `prompt_template.j2` and stay user-editable.

---

## 11. Required Refactoring

The blocker is not missing features, it is **where the logic lives**. The workflows the CLI needs
are welded into the TUI: `tree_selector.py` is 1465 lines and owns patch intake, extend, the
session gardener, and Ralph setup. `main.py:292` always enters the interactive selector — there is
no headless path today, even with `-t` supplied.

### 11.1 Extract a core with no I/O prompts

```
kopipasta/core/
  resolver.py    # patterns -> resolved selection (role + render mode)
  budget.py      # the demotion ladder
  context.py     # resolved selection -> rendered payload (wraps prompt.py)
  session.py     # on-disk conversation: turns, dedup, hashes, meta
  backend.py     # exec / anthropic / openai-compat
  patchflow.py   # parse -> validate -> apply -> report
```

Both the TUI and the CLI become thin views over this. The TUI gets simpler as a side effect.

### 11.2 Make interactivity injectable

Three call sites block on a human inside otherwise pure logic:

| Location | Current | Fix |
|---|---|---|
| `prompt.py:383` | `input()` per env var during render | `policy` param; non-interactive default **mask** (leaking a secret to an API is worse than a broken value) and report what was masked in `--json` |
| `patcher.py:914` | `click.confirm` on delete | injected `confirm` callable; agent policy = `--allow-delete` |
| `patcher.py:1035` | `click.confirm` on shrink guard | injected `confirm`; agent policy = hard fail unless `--force` |
| `main.py:267` | `input()` for web full/snippet | `--url-full` / `--url-snippet` flags |

### 11.2b Narration currently goes to stdout, which breaks `--json`

Found by the spike the moment it tried to parse its own output: `config.read_gitignore`
(`config.py:68`) prints `".gitignore detected."` to **stdout**, and `apply_patches` prints its
progress there too. Both land in the middle of the JSON object and make it unparseable —
exactly the §8 contract they violate.

Auditing every `print()` in the codebase is necessary but not sufficient, because third-party
libraries on the path have no such contract. Belt and braces: **redirect `sys.stdout` to
`sys.stderr` for the whole run in `--json` mode** and write the result object to the saved real
stdout handle. Narration then cannot corrupt the contract no matter who emits it.

### 11.3 Fix the global cache (pre-existing bug)

`cache.py:12` stores selection, map, and task in a **single global** `~/.cache/kopipasta/`
directory. Two kopipasta processes in two repos already clobber each other's state today; with
agents running things in parallel this goes from latent to routine. Key the cache by project
root hash, and treat per-session state as belonging to `.kopipasta/sessions/<id>/` instead.

### 11.4 Zone the prompt template

`SEMANTIC_ARCHITECTURE.md §3.1` already calls for splitting `## File Contents` into
`## Active Workspace (Editable)` and `## Reference Context (Read-Only)`. That doc was written
for the human flow; it matters more here, because `-e` vs `-r` is now a machine-checkable
contract the patcher can enforce (reject patches to files that were only `-r`).

---

## 12. Phasing

**Phase 0 — Headless foundation.** Injectable policies (§11.2), `--json` plumbing, exit codes,
`.kopipasta/` layout + auto-gitignore, per-project cache fix. No user-visible features; every
later phase depends on it.

**Phase 1 — The loop, no new dependencies.** `pack`, `apply`, the selection grammar (§4), the
budget ladder (§5), sessions on disk (§6), and `ask --backend exec:...`. **This phase alone
delivers both target use cases.** Ship it, use it for a week, then decide the rest.

**Phase 2 — Native backend.** Anthropic SDK, prompt caching, streaming, usage accounting.
`patch` = `ask --apply` with the safety rails of §9 and `--verify`.

**Phase 3 — Agent ergonomics.** `--mode triage` and the schema (§10), `files_cited`,
`--dry-run` diffs, multi-turn dedup via content hashes, `map` as a standalone verb.

**Phase 4 — Distribution.** Re-point `mcp_server.py` at the new core (`kopipasta_ask`,
`kopipasta_pack`, `kopipasta_apply`) so MCP harnesses skip the subprocess; ship a Claude Code
skill wrapper; rewrite the README around the oracle framing with the TUI as the human path.

---

## 13. Risks and Open Questions

- **Cost is real and easy to hide.** A 500k-token frontload is meaningful money per call, and
  agents call things in loops. Mitigations: `pack --json` shows the size before spending;
  `--max-cost` refuses above a threshold; prompt caching for multi-turn. An accidental
  `while true; do kopipasta ask --all; done` must not be cheap to write by mistake.
- **API-key custody.** Sidestepped entirely if `exec:` stays the default backend. Prefer that.
- **Two-model failure modes.** When the oracle is wrong, the agent inherits a confident wrong
  answer with none of the evidence. `--json` must always carry `demoted` and the request path so
  the agent can check what the oracle actually saw.
- **Verbatim capture.** `exec:` backends may emit ANSI, spinners, or wrapper chrome into stdout.
  Response capture needs sanitising (`ops.sanitize_string` is a start) and the raw bytes kept
  alongside.
- **Does `AI_SESSION.md` survive?** The quad-memory model was designed for a human driving a
  chat. `AI_CONTEXT.md` (project constitution) clearly stays and gets *more* valuable — it is the
  cheapest way to give a disposable oracle the project's non-obvious rules. `AI_SESSION.md`
  overlaps with `.kopipasta/sessions/`; proposal is to keep it as the human/TUI path and let the
  agent path use session dirs, then revisit once Phase 1 has real usage.
- **Open: should `ask` be able to select its own context?** A `--auto-select` that runs a cheap
  map-only triage pass and then a full pass would be genuinely useful, and is also exactly the
  "hidden magic" this tool was built to avoid. Deferred until the explicit path is proven.

---

## 14. What Does Not Change

- Bare `kopipasta` is the TUI, unchanged. The human loop keeps working.
- `.gitignore` respect, binary filtering, secret masking, hallucination guards.
- The patcher's format tolerance (unified diff, search/replace, full file, delete, `<<<RESET>>>`)
  — this is the asset that makes one-shot large patches land at all, and every new surface
  should route through it rather than around it.
- Explicit context control. The selection is still yours; it just comes from argv instead of
  arrow keys.
