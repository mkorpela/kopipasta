# Specification: kopipasta as an Agent-Facing CLI ("Context Oracle")

> **Companion document:** `AGENT_CLI_FINDINGS.md` is the empirical log — what was actually run,
> the numbers, what broke, and which parts of this spec are measured versus still assumed.
> This document reads as if everything is settled; that one tells you what is not. Read it
> before implementing.

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

Note what is **absent** from every line above: the model and the provider. Those are operator
configuration, not per-call arguments — see §7a. `kopipasta ask -q "..."` is the whole common
path, and §7b defines what every failure of it has to tell you.

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
| Explicit cache breakpoint | `cache_control: ephemeral` on a content block | `cachedContents` resource — **required; implicit caching measured 0%** | **none** — implicit, no control |
| Cache lifetime cost | free to abandon | **rented: per token-hour until TTL** | n/a |
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

#### Measured: cache reuse is real, but not back-to-back

`spike/livecheck.py` sends the same prefix twice with different suffixes and checks whether turn
2 had to *write* the prefix into cache again. The naive check — "is `cached_tokens > 0`?" — is
worthless, because a harness backend caches its own system prompt and reports a nonzero number
on turn 1 too. The signal is `cache_creation` on turn 2.

Two runs of the same 56k-char payload through `claude-cli:`, the second about a minute after the
first (prefix pinned via `LIVECHECK_NONCE`):

| | turn 1 created | turn 2 created | cost/turn |
|---|---|---|---|
| cold | 24,432 | 24,436 | **$0.176** |
| ~1 min later | 0 | 0 | **$0.035** |

Two conclusions, and they point in opposite directions:

1. **Prefix caching genuinely works across separate CLI invocations — 5× cheaper.** A stable
   repo payload is worth real money, and §6's dedup design is sound.
2. **Back-to-back turns get nothing.** Turn 2, issued seconds after turn 1, re-wrote the entire
   prefix and cost the same. A cache entry is not readable the moment it is written.

So "turn 1 pays for the repo, turns 2..n pay for a question" is **true across a session's
lifetime and false within a rapid burst.** An agent firing three follow-ups in ten seconds pays
full freight three times. Design accordingly: do not promise per-turn savings in the `--json`
output, and treat rapid multi-turn as a cost the caller should know about.

#### Measured: Gemini's implicit caching does not serve this pattern

The same experiment against Gemini, and the result is a warning about assuming caching happens
by itself. `gemini-3.7-flash`, 58k chars (~16.3k tokens, four times the documented 4,096-token
minimum), two arms seconds apart on the same model:

| arm | turn 2 cache share |
|---|---|
| explicit `cachedContents` | **99.9%** (16,277 / 16,293) |
| implicit only (control) | **0.0%** (0 / 16,290) |

Implicit caching missed on **7/7** requests that shared a byte-identical prefix but asked a
different question, and hit on **5/5** exact repeats of a whole earlier request. That is the
inverse of what an oracle needs: "same repo, different question" is the entire access pattern,
and an exact-repeat hit hands back an answer you already had. It is not a lag — a new suffix
missed 98 seconds in, while a repeat hit 2 seconds after its own miss.

**Consequence for §11.2 and §6:** Gemini's cache is a *resource*, not a request flag, and it is
billed per token-hour for the whole TTL whether or not turn 2 arrives. A session that opens one
must own its lifetime: explicit clamped TTL, delete on session end, and a sweep for orphans. A
crashed run that leaks one produces a bill, not an error — the failure mode nobody notices.
See `AGENT_CLI_FINDINGS.md` §2.9.

#### Measured: the raw API has no such lag

The same experiment against the real Anthropic API, 56k-char payload, cold turn 1:

| | raw `input_tokens` | cache_read | cache_creation | total input |
|---|---|---|---|---|
| turn 1 (cold) | 19 | 0 | **20,345** | 20,364 |
| turn 2 (~4s later) | 23 | **20,345** | 0 | 20,368 |

Turn 2 read the whole prefix back **four seconds** after turn 1 wrote it — 99.9% of its input.
Side by side on the same payload, turn 2 of a back-to-back pair:

| backend | cache share of input | cost delta |
|---|---|---|
| `claude-cli:` | 50.0% — its own system prompt only | none |
| `anthropic:` | ~99.9% — the whole prefix | (API reports tokens, not cost) |

**This changes the phasing.** Cheap-follow-up-turn economics hold on the raw API and do not hold
through the CLI, so `anthropic:` moves out of "Phase 2 optimisation" and into Phase 1 alongside
`exec:`. It is the only backend measured to make multi-turn actually cheap, and multi-turn
triage is half the point of the design.

Two cautions. Anthropic's `input_tokens` counts only tokens neither read from nor written to
cache — a 20k cached prefix reports `input_tokens=19`, so report the sum of raw + read +
creation or a cached call looks like it sent nothing. And this was measured at ~20k tokens; the
design targets 400k–1M, where TTL and minimum-prefix rules may differ. See
`AGENT_CLI_FINDINGS.md` §2.7 and §5.

#### Choosing

- **Gemini** for use case A. 1M+ context is the only honest way to frontload a large repo
  (`inputTokenLimit: 1048576`, confirmed), and `responseSchema` makes the triage answer
  machine-consumable without parsing prose. **Always create an explicit `cachedContents` entry**
  — measured, the implicit path gives 0% on multi-turn — and always own its TTL.
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

---

## 7a. Backend Selection Is Configuration

**Which model answers is an operator decision, not a question decision.** A caller asking
"where does token expiry live?" has no basis for choosing between Gemini and Opus — that choice
is about cost, context ceiling and which keys you hold, and it does not change between
questions. So it does not belong in the call.

The whole point is that the verb collapses to this:

```
kopipasta ask -q "question" [-e/-r/-m/-x selectors]
```

One required argument and the selectors. No `--backend` in the common path at all.

### Precedence

| Source | When you use it |
|---|---|
| `--backend gemini:gemini-3.7-flash` | escape hatch — debugging, A/B, pinning a CI job |
| `KOPIPASTA_BACKEND` | per-shell or per-job override |
| `~/.config/kopipasta/config.toml` | **the normal place** — set once |
| built-in default | first run, before anything is configured |

The flag survives, but as an override you rarely type — not a thing every call site repeats.

### The file

`~/.config/kopipasta/` already holds `prompt_template.j2` and `ai_profile.md`, both hand-edited
via an `--edit-X` flag. `config.toml` and `--edit-config` follow that precedent, and inherit the
editor guard from §11.1b: headless `--edit-config` exits 8 rather than blocking on `$EDITOR`.

```toml
[ask]
provider    = "gemini"
model       = "gemini-3.7-flash"
cache_ttl_s = 300
max_tokens  = 8192
timeout_s   = 900
```

`requires-python` is `>=3.10` and `tomllib` is 3.11+, so this needs
`tomli; python_version < "3.11"`. Worth the dependency: a file people hand-edit wants comments,
and JSON does not have them.

**API keys stay in the environment, never in this file.** `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`. Config holds provider, model and knobs; the secret stays out of a file on
disk, which is the rule the adapters already follow.

### The one thing that genuinely varies — per verb, not per call

Triage over a 400k frontload wants Gemini's context ceiling and price. A one-shot coordinated
patch wants the strongest model available. Same operator, same day, different answer:

```toml
[ask]
provider = "gemini"
model    = "gemini-3.7-flash"

[patch]
provider = "anthropic"
model    = "claude-opus-5"
```

Still configuration, still zero per-call flags — sectioned by verb, with `[ask]` as the fallback
for any verb lacking its own section. This handles the real case without reintroducing a
per-call decision.

### `kopipasta config --show`

Prints the **resolved** backend and where each value came from:

```
provider     gemini              ~/.config/kopipasta/config.toml [ask]
model        gemini-3.7-flash    KOPIPASTA_BACKEND
api key      GEMINI_API_KEY      set (39 chars)
cache_ttl_s  300                 built-in default
```

Precedence chains are exactly the thing that silently does the wrong thing, and "which model
actually answered?" is the first question when an answer looks off. Never print the key itself —
presence and length are enough to diagnose "set but empty" and "set to the wrong one".

Internally `backends.build("gemini:gemini-3.7-flash")` is unchanged; the `kind:model` spec string
becomes plumbing that config resolves into, rather than user surface.

---

## 7b. Error Messages Are Part of the Interface

Both consumers are bad at guessing. A human types the command once a week and does not remember
the flags; an agent cannot guess at all and will either retry a permanent failure forever or
give up on a transient one. Every failure therefore states **what failed, why, and the next
action** — in that order.

### Rules

1. **Name the exact thing.** `GEMINI_API_KEY`, `~/.config/kopipasta/config.toml`, `--budget` —
   never "configure your credentials" or "check your settings".
2. **Quote the provider verbatim.** Paraphrasing an upstream error destroys the detail that
   identifies it. Include our diagnosis *and* their text.
3. **Separate "you configured it wrong" from "the world is temporarily broken."** This is what
   the exit codes are for (§8): 1 and 2 mean *do not retry*, 3 means *retrying may work*. An
   agent branches on this, and getting it wrong produces either an infinite retry loop or a task
   abandoned over a blip.
4. **Report what was resolved, not just what was missing.** Half of all credential bugs are "the
   wrong config won". `no GEMINI_API_KEY` is much less useful than `provider=gemini (from
   config.toml [ask]) needs GEMINI_API_KEY`.
5. **stdout stays empty on failure.** Errors and narration go to stderr (§11.2b). A partial
   artifact on stdout is worse than none — the caller cannot tell it is partial.
6. **Suggest the fix, not the concept.** Show the command or the two lines of TOML.

### The failure table

| Condition | Exit | The message must carry |
|---|---|---|
| No backend configured anywhere | 2 | The three ways to set one, and the config path — this is a first-run experience, so it doubles as onboarding |
| Key missing for the resolved provider | 2 | The provider, **where it was resolved from**, and the exact env var |
| Unknown provider in config | 1 | The invalid value and the valid set |
| Model rejected by the provider | 3 | That the *model name* was rejected, not the credentials — with the provider's text |
| Auth rejected (401/403) | 2 | The provider's text; do not retry |
| Rate limited (429) | 3 | `Retry-After` when present; retrying is expected to work |
| Timeout | 3 | The timeout that was hit and the config key that raises it |
| **No files matched the selectors** | 1 | **Which patterns matched nothing.** See below |
| Response truncated (`MAX_TOKENS`) | 3 | That it was truncated and `max_tokens`; never report a partial answer as success |
| Schema validation failed | 3 | What was expected vs what arrived, and the response path |
| Backend behaved as an agent | 3 | The tool-permission diagnosis and the flags that disable tools (§7) |

### Two that deserve their own treatment

**An empty selection is an error, not a warning.** A typo'd glob silently selects nothing, the
model answers from the project structure alone, and the answer looks plausible. This is the
worst failure shape in the whole tool: no error, an answer that reads fine, and no signal that
it was produced from nothing. Report *per pattern*, because with several selectors "0 files" does
not say which one was wrong:

```
kopipasta: no files matched.
  -e kopipasta/pacher.py   0 files   (did you mean kopipasta/patcher.py?)
  -m kopipasta/*.py       16 files
Nothing was selected, so there is nothing to ask about.
```

The "did you mean" is worth the few lines — a single-character typo in a path is the common case,
and the fix is a nearby-filename search we already have the file list for.

**Truncation must never be success.** Measured (findings §2.9): the finish-reason guard only
fired when text was *empty*, so a `MAX_TOKENS` stop with partial text passed as `"ok": true`
with `"triage": null` — under `responseSchema` that is JSON ending mid-string. Reasoning tokens
also spend `maxOutputTokens`, which is how an 8192 budget produced a 318-token answer. Any
finish reason other than a normal stop is a failure with its own message.

### Shape

```
kopipasta: <what failed>
  <why, including anything the provider said>

  <next action — a command or the config lines>
```

Example:

```
kopipasta: no API key for provider 'gemini'.
  Resolved from ~/.config/kopipasta/config.toml [ask]; GEMINI_API_KEY is unset.

  export GEMINI_API_KEY=...
  Or switch provider:  kopipasta --edit-config
  See what resolved:   kopipasta config --show
```

Under `--json` the same content is structured, so an agent gets the fields rather than the prose:

```json
{"ok": false, "error": "no_api_key", "exit": 2, "provider": "gemini",
 "resolved_from": "~/.config/kopipasta/config.toml [ask]",
 "missing_env": "GEMINI_API_KEY", "retryable": false,
 "hint": "export GEMINI_API_KEY=..."}
```

`error` is a stable machine-readable slug and `retryable` is explicit, so an agent never has to
infer either from prose.

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

The policy is **not** uniform, and that is the point. Two kinds of question:

- **No safe default → refuse** (exit 8) and name the flag that avoids the question. "Which
  files?", "What is the task?", "Full page or snippet?" cannot be guessed; a wrong guess yields
  a plausible-looking wrong answer, which is worse than an obvious failure.
- **Safe default exists → apply it**, narrate on stderr, keep running. Failing fast on
  everything would make kopipasta unusable in CI the moment a `.env` existed, and buy no safety
  at all — masking already leaks nothing.

`interaction.require_human` is the first; `interaction.use_default_without_human` is the second.

| Location | Current | Fix | Status |
|---|---|---|---|
| `prompt.py:383` | `input()` per env var during render | non-interactive default **mask** (leaking a secret to an API is worse than a broken value); announce on stderr, and report what was masked in `--json` | **done** |
| `prompt.py:553` | `prompt_toolkit` task editor | `require_human`, hint names `-t/--task` | **done** |
| `main.py:267` | `input()` for web full/snippet | `--url-full` / `--url-snippet` flags; `require_human` when neither is given | **done** |
| `patcher.py:914` | `click.confirm` on delete | policy `allow_delete`; declines by default | **done** |
| `patcher.py:1035` | `click.confirm` on shrink guard | policy `force`; skips the file by default | **done** |

Two constraints the patcher work exposed, both worth carrying into the headless `apply` verb:

- **The refusal must not raise.** The per-file body of `apply_patches` is wrapped in a broad
  `except Exception`, so a `NoHumanAttached` would be swallowed and resurface as
  "Error processing …" — sending the caller to debug a patch that was fine. Injecting the
  decision is the only shape that survives an exception handler you do not control.
- **An opt-in flag must not suppress the prompt for a human who IS present.** `--allow-delete`
  means "this run may delete", not "delete without telling me". The flag answers the question
  only when there is nobody to ask.

### 11.1b Never stall a caller who cannot answer

The worst failure mode for a tool an agent shells out to is a prompt with nobody there. The
agent cannot distinguish a stall from slow work, so it waits forever.

**This is not hypothetical, and it is not caused by anything in this spec.** Shipped kopipasta
today, invoked as `kopipasta . -t task < /dev/null`:

| | before | after |
|---|---|---|
| exit | 124 (killed at timeout) | **8** |
| stdout in 10s | **5,551,947 bytes**, 4,481 tree redraws | 394 bytes |
| wall clock | unbounded | 0.33s |

`click.getchar()` raised `[Errno 6] ... '/dev/tty'`, the broad `except Exception` caught it,
printed the error and called `click.pause()` — which is a **documented no-op without a tty**, so
nothing throttled the retry. The loop redrew the full tree at ~450/sec forever: ~2 GB/hour into
whatever the caller was capturing, at 100% CPU, with `quit_selection` unreachable because no key
could ever be read.

#### The fix is layered, and moving the TUI is the weakest layer

One obvious option is to put the TUI behind `kopipasta tui` and make bare `kopipasta` do
something safe. **Rejected as the primary mechanism**: it breaks every existing invocation, every
README, and every user's muscle memory of a published tool — and it would not have prevented this
bug, because a caller that types `kopipasta tui` in a script still hangs. Prevention has to live
where the blocking happens, not in the argument grammar.

1. **A single guard at the point of interaction** — `kopipasta/interaction.py`. `human_attached()`
   is false unless *both* stdin and stdout are ttys, and is forced false by
   `KOPIPASTA_NONINTERACTIVE=1` or `CI`. `require_human(what, hint)` raises `NoHumanAttached`.
   It lives in one module rather than at each call site so a prompt added later inherits the
   protection by calling one function. Checked *before* the first render, so nothing is drawn
   into a pipe.
2. **Recurring failures must terminate.** `except Exception: …; continue` around a blocking read
   is an infinite-loop generator. `OSError` (terminal lost) now aborts immediately — retrying
   cannot recover it — and anything else gets three attempts before a hard stop. This is the
   layer that makes the *class* of bug non-recurring, not just this instance.
3. **Subcommands must never fall through to the TUI.** The dispatch rule in §3 is itself an
   accidental-TUI generator: a typo like `kopipasta pak --all` treats `pak` as a path and opens
   the selector. If `argv[1]` is a bare word — no path separator, no extension, not present on
   disk — and is not a known subcommand, exit 1 with "unknown command". Only fall through for
   things that actually look like paths.

   Implemented in `KopipastaApp._resolve_subcommand`. Two details worth keeping: the
   path-detection is deliberately **generous** (an existing `Makefile` is a path, not a typo)
   because breaking a real invocation of a published tool is worse than the bug being fixed; and
   verbs that are *specced but unbuilt* (`pack`, `ask`, …) get their own message rather than
   "unknown command", because "not yet" and "no such thing" send the caller to different places.
4. **`kopipasta tui` as an explicit alias.** Additive, breaks nothing, gives scripts an
   unambiguous way to say "I really do want the UI" — and leaves the door open to flipping the
   default later without inventing new syntax then. Note that it does **not** bypass layer 1:
   naming the TUI explicitly is not consent to hang, which is the same reason moving the TUI
   behind a subcommand was rejected as the primary fix.

Exit code **8 = interaction required, no human attached**, distinct from 1 (usage) because the
fix differs: the caller needs a policy or a different invocation, not a corrected command line.
The message names the env var that makes the refusal explicit.

Every other prompt in §11.2 now routes through the same module, so the fix generalises — but
see that section for why "fail fast" is the right answer for only *some* of them. The task
prompt and the web snippet choice refuse (exit 8); env masking applies the safe default and
carries on. A guard that refuses uniformly would be simpler and would make the tool useless in
CI.

Bounded runtime is a separate axis and still worth having: `--timeout` already caps a single
backend call, and a global `--deadline` capping the whole invocation would let a caller
guarantee termination regardless of which stage misbehaves.

### 11.2b Narration currently goes to stdout, which breaks `--json` — **done**

Found by the spike the moment it tried to parse its own output: `config.read_gitignore`
(`config.py:68`) prints `".gitignore detected."` to **stdout**, and `apply_patches` prints its
progress there too. Both land in the middle of the JSON object and make it unparseable —
exactly the §8 contract they violate.

Auditing every `print()` in the codebase is necessary but not sufficient, because third-party
libraries on the path have no such contract. Belt and braces: **redirect `sys.stdout` to
`sys.stderr` for the whole run in `--json` mode** and write the result object to the saved real
stdout handle. Narration then cannot corrupt the contract no matter who emits it.

Implemented in `output.py` and applied **unconditionally**, not only in `--json` mode. The
audit would have been large (127 `print()` and 92 `console.print()` calls) and would not have
held: `rich`, `click`, and anything else on the path write to stdout too. One redirect converts
every call site at once, including third-party ones, and `rich` resolves `Console.file` at
write time so consoles built before the swap follow it.

Three consequences worth stating, all of them tested:

1. **`kopipasta > prompt.txt` now yields the prompt.** Previously the file began
   `Generated prompt:`, contained 80 dashes, the included-file list and a `☕🍝` banner.
2. **Redirecting the artifact no longer disables the TUI.** `human_attached()` checks the
   stream it narrates to, which is now stderr; the keyboard and display are still present when
   only stdout is redirected. This is a behaviour change and a deliberate one.
3. **`--help` is output, not narration.** It is written to the artifact stream by a parser
   subclass. Pre-scanning `argv` for `-h` was the first attempt and is wrong: `kopipasta -- -h`
   names a *file*, and the scan would silently disable the redirect for the whole run.

The one thing the redirect cannot fix is itself: `main._configure_platform` calls
`sys.stdout.reconfigure(encoding="utf-8")`, which by then is stderr, leaving the real artifact
handle on cp1252 — the stream most likely to carry non-ASCII, since it carries the user's file
contents. `output.py` reconfigures the saved handle where it still has a reference to it.

### 11.3 Fix the global cache (pre-existing bug) — **done**

`cache.py:12` stored selection, map, and task in a **single global** `~/.cache/kopipasta/`
directory. Two kopipasta processes in two repos already clobber each other's state today; with
agents running things in parallel this goes from latent to routine. Key the cache by project
root hash, and treat per-session state as belonging to `.kopipasta/sessions/<id>/` instead.

**It was worse than "clobber", because the files hold *relative* paths.** Opening repo B did
not fail to load repo A's selection — it loaded it and the `os.path.exists()` filter kept every
path that happened to exist in B too, so `src/main.py` and `README.md` were silently
pre-selected from another project's session. The same applied to the task description, which is
prose and frequently confidential. `clear_cache()` wiped the shared files, so ending a session
in one repo destroyed every other repo's saved state. None of it warned.

Implemented as:

| Concern | Decision |
| --- | --- |
| Location | `~/.cache/kopipasta/projects/<slug>-<sha256[:12]>/`. **Not** in the project: that would need a `.gitignore` entry in every repo and breaks on read-only checkouts. |
| Project identity | Nearest ancestor containing `.git` (a *file* in worktrees/submodules), else cwd — so `repo/` and `repo/src/` share one cache instead of looking like two projects. |
| Key normalisation | `os.path.normcase`, plus `.lower()` on darwin only. macOS is case-insensitive but `normcase` is a POSIX no-op; Linux is genuinely case-sensitive, so folding there would merge two real projects. |
| Stored paths | Relative to the **project root**, converted back to cwd-relative on load, since callers `os.path.abspath()` the result. |
| Writes | Temp file + `os.replace`, retried on `PermissionError` (Windows fails the rename while another process holds the destination). Torn reads were reproducible in 0.5s before this. |
| Legacy global files | Never read — attributing them to whichever repo runs first would recreate the bug. Swept only on an explicit `clear_cache()`. |
| Failure | A cache must never be why the tool won't start: unset `HOME` falls back to the temp dir, and an unwritable root degrades to a stderr warning. |

Cache warnings moved to stderr as part of this (§11.2b): they are narration, and on stdout they
corrupt `--json`.

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

**Phase 1 — The loop.** `pack`, `apply`, the selection grammar (§4), the budget ladder (§5),
sessions on disk (§6), `ask --backend exec:...` — **and `anthropic:`**, which is ~40 lines over
the existing `requests` dependency and is what makes multi-turn affordable. **This phase alone
delivers both target use cases.** Ship it, use it for a week, then decide the rest.

**Phase 2 — Remaining backends and safety rails.** `gemini:` and `openai:`, streaming,
retry/backoff. `patch` = `ask --apply` with the safety rails of §9 and `--verify`. (`anthropic:`
moved up to Phase 1 — see §7: it is the only backend measured to make follow-up turns cheap.)

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
