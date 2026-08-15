# Findings: Agent-CLI Spike

Companion to `AGENT_CLI_SPEC.md`. The spec is the design; this is the **empirical log** —
what was actually run, what the numbers were, what broke, and what is still assumption.

Read this first if you are picking the work up. The spec reads as if everything is settled;
this file tells you which parts are load-bearing measurements and which are still guesses.

Everything below was measured in a Linux sandbox with the `claude` CLI available and **no
provider API keys**. That constraint shapes what could and could not be verified.

---

## 1. State of verification

| Component | Status | How |
|---|---|---|
| Selection grammar (`-e/-r/-m/-x`, globs) | **working** | run against this repo |
| Budget demotion ladder | **working** | 15 refs → 9 demoted largest-first; `-e` held |
| Session dirs + JSON envelope | **working** | `.kopipasta/sessions/<id>/NNN-*.md` |
| `exec:` backend | **live-verified** | real `claude -p` |
| `claude-cli:` backend | **live-verified** | real `claude -p --output-format json` |
| Triage mode (use case A) | **live-verified** | found the right call sites, plus one we missed |
| One-shot patch (use case B) | **live-verified** | 4 hunks, +32/−17, 119 tests green |
| `anthropic:` backend | **wire format only** | mock; never saw a real response |
| `gemini:` backend | **wire format only** | mock; never saw a real response |
| `openai:` backend | **wire format only** | mock; never saw a real response |
| Raw-API cache behaviour | **UNVERIFIED** | no key — see §5 |
| Gemini 1M context for a real repo | **UNVERIFIED** | no key; §5 of the spec assumes it |

**The single most valuable next action** is `uv run python spike/livecheck.py anthropic` with a
real key. It takes ten seconds and settles the one question the whole multi-turn design rests
on. See §5.

---

## 2. Measurements

### 2.1 The oracle must be a completion, not an agent

The first `patch` run returned no code at all:

> *"The edit tool needs permission approval from you to modify `kopipasta/prompt.py`."*

`claude -p` is an agent. Handed a task and a file, it reaches for its own Edit tool rather than
emitting a patch, then blocks on a permission prompt nobody will answer. The identical payload
with `--disallowedTools` produced a clean four-hunk patch first try.

Consequence for the design: a `patch`-mode response with no code blocks — especially one
mentioning permission or tool approval — is a **backend misconfiguration (exit 3)**, not a bad
patch (exit 5). Different diagnosis, different fix.

### 2.2 CLI backends are not limited to blind text

`claude -p` supports `--output-format json` (real `usage` **and `total_cost_usd`**) and
`--json-schema` (server-enforced structured output, returned parsed under `structured_output`).
The spec originally claimed `exec:` structurally gives both up. It does not — those were
unread flags. Hence the `claude-cli:` rung.

### 2.3 The harness tax

Differencing a tiny prompt against a 46k-char payload:

| | input | cache_creation | cache_read | cost |
|---|---|---|---|---|
| `"hi"` | 2 | 5,099 | 29,280 | **$0.0396** |
| 46k-char payload | 2 | 23,573 | 29,280 | $0.1648 |

- **~29.3k tokens of harness system prompt are read on every call**; ~34k floor before your
  payload exists. Saying "hi" costs four cents.
- `input_tokens` reads **2 in both rows**. The CLI routes everything through cache blocks, so
  it is useless as a measure of payload size and must never be reported as "our input." The
  payload only appears in the `cache_creation` delta.

For a 400k-token frontload the floor is ~8% overhead. For the cheap follow-up turns that make
multi-turn attractive, it is most of the cost.

### 2.4 Cache reuse works across invocations, but not back-to-back

`spike/livecheck.py`, 56k-char payload, prefix pinned across runs via `LIVECHECK_NONCE`:

| | turn 1 created | turn 2 created | cost/turn |
|---|---|---|---|
| cold | 24,432 | 24,436 | **$0.176** |
| ~1 min later | 0 | 0 | **$0.035** |

1. **Prefix caching genuinely works across separate CLI invocations — 5× cheaper.** The
   stable-prefix design is sound.
2. **Back-to-back turns get nothing.** Turn 2 seconds after turn 1 re-wrote the entire prefix
   at full price. A cache entry is not readable the moment it is written.

So *"turn 1 pays for the repo, turns 2..n pay for a question"* is **true across a session's
lifetime and false within a rapid burst** — which is exactly the pattern an agentic harness
naturally produces. Do not promise per-turn savings in `--json` output.

### 2.5 `estimate_tokens` is 44% low

`ops.estimate_tokens` assumes **3.6 chars/token**. A real 46,102-char payload from this repo
measured **~18,474 tokens — 2.50 chars/token**. The estimator said 12,806.

This is not cosmetic: `--budget 400k` would ship ~576k tokens and blow the window the flag
exists to protect. Dense code plus the minified JSON structure blob tokenise far worse than
prose, and the payload is mostly both. **The budget ladder cannot ship on this estimator.**
Recalibrate against measured payloads, or count properly via provider `count_tokens` /
`tiktoken`. If it stays heuristic, bias it pessimistic — under-counting is the dangerous
direction.

### 2.6 Triage quality (n=1, but encouraging)

Asked which call sites block non-interactive operation, with the repo mapped (12.8k est input
tokens, 72s). It returned `main.py:292`, `main.py:267`, `patcher.py:914`, `patcher.py:1035` —
matching a manual read — and found one that manual reading missed: **`.ralph.json` can only be
produced by the interactive `_action_ralph`, so the already-headless MCP server cannot be
bootstrapped headlessly.**

One slip worth knowing: it placed `get_task_from_user_interactive` "around line 383". That is
the `input()` inside `handle_env_variables`; the real location is line 535. Line numbers from
an oracle are hints, not citations.

---

## 3. Traps

Ordered by how much time each cost.

1. **`cached_tokens > 0` is not evidence of cache reuse.** A harness caches its own system
   prompt, so turn 1 reports a nonzero number too. The first version of `livecheck` printed
   "cache HIT" over numbers showing zero cost saving. The signal is `cache_creation` on turn 2.
2. **A caching experiment needs a per-run nonce.** Without one, the second run of the day reads
   a warm cache on turn 1 and silently measures nothing. One run reported "PREFIX REUSED" while
   testing nothing at all.
3. **The response schema must mirror the prompt template exactly.** Where the provider enforces
   the schema, *the schema wins*. A flatter schema than the template silently drops fields — we
   lost `why` and `confidence` off every cited file that way. Same input, quietly worse output,
   no error. This only shows up when you switch backends.
4. **Library narration on stdout breaks `--json`.** `config.read_gitignore` (`config.py:68`)
   prints `".gitignore detected."` to stdout; `apply_patches` prints progress there too. Both
   land mid-object and make it unparseable. Auditing every `print()` is necessary but not
   sufficient — third-party libraries have no such contract. Redirect `sys.stdout` to
   `sys.stderr` for the whole run in `--json` mode and write the result to the saved handle.
5. **Reimplementing helpers that already exist.** The spike hand-rolled a `.gitignore` append
   without checking for a trailing newline, produced `.ralph.json.kopipasta/`, and committed
   session artifacts into git as a result. `git_utils.add_to_gitignore` (`git_utils.py:16-17`)
   already handles that case. Treat "new surfaces route through existing helpers" as a hard
   constraint on Phase 1, not a preference.
6. **OpenAI-compatibility is a request-shape convenience, not parity.** The two features it
   drops — explicit cache control and cached-token accounting — are precisely the two the
   oracle is built on. Gemini's compat endpoint is
   `https://generativelanguage.googleapis.com/v1beta/openai/`; the trailing slash matters.
7. **`claude --bare` breaks auth** in this sandbox. Unrelated to the design, but it cost a
   debugging cycle.

---

## 4. What is in `spike/`

Throwaway. **Not** the Phase 1 implementation — no core package, no refactor, no TUI changes.
It exists to prove the pipeline and to make the measurements above reproducible. Delete it once
Phase 1 lands, or keep `livecheck.py`, which stays useful.

| File | What |
|---|---|
| `oracle.py` | `pack` / `ask` / `patch`, selection, budget ladder, sessions, JSON envelope |
| `backends.py` | `exec:`, `claude-cli:`, `anthropic:`, `gemini:`, `openai:` |
| `check_backends.py` | 21 assertions against a mock speaking all three wire shapes; no keys needed |
| `livecheck.py` | Real-provider cache measurement; skips providers without credentials |

```bash
uv run python spike/check_backends.py                     # offline, always runnable
uv run python spike/oracle.py pack --all --budget 40k --json
uv run python spike/oracle.py ask --all --mode triage -q "..." --json \
    --backend 'claude-cli:-'
uv run python spike/livecheck.py                          # needs a key for raw providers
```

The spike reuses the real modules for everything that matters — `file.is_ignored`,
`file.extract_symbols`, `prompt.get_project_structure`, `patcher.parse_llm_output` /
`apply_patches`. That is deliberate: it tests the actual components, not stand-ins.

---

## 5. The open question

**Does the raw Anthropic API share the CLI's cache write-visibility lag?**

It matters more than it sounds. If raw does *not* have the lag, then rapid multi-turn is cheap
on `anthropic:` and expensive through `claude-cli:` — which promotes the native adapter from a
Phase 2 optimisation to something worth building early. If it does have the lag, then §6's
multi-turn dedup needs rethinking rather than tuning, because bursts cost full freight
everywhere.

```bash
export ANTHROPIC_API_KEY=...        # shell env; the adapters never write keys to session files
uv run python spike/livecheck.py anthropic
```

Three outcomes:

- **PREFIX REUSED on turn 2** — no lag on raw. Build `anthropic:` early.
- **FAIL** — the adapter is wrong. We place the breakpoint ourselves, so a miss is our code,
  not the provider's.
- **Same lag as the CLI** — the lag is a property of the cache itself. Revisit §6.

Worth running `gemini` in the same pass: its caching is implicit, so a miss there is
inconclusive rather than a defect, but the **1M-context claim underpinning use case A has never
been exercised against a real payload**.

---

## 6. Next actions, in order

1. Run §5. Ten seconds, settles the multi-turn design.
2. Fix `estimate_tokens` (§2.5) — the budget ladder is blocked on it.
3. Phase 0 from the spec: injectable policies, `--json` plumbing and the stdout/stderr
   contract, `.kopipasta/` layout, per-project cache fix.
4. Phase 1: `pack` / `apply` / selection grammar / budget ladder / sessions / `exec:` and
   `claude-cli:` backends. This alone delivers both use cases.
5. Only then decide whether the raw adapters graduate from the spike, using the §5 answer.
