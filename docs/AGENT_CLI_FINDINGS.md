# Findings: Agent-CLI Spike

Companion to `AGENT_CLI_SPEC.md`. The spec is the design; this is the **empirical log** —
what was actually run, what the numbers were, what broke, and what is still assumption.

Read this first if you are picking the work up. The spec reads as if everything is settled;
this file tells you which parts are load-bearing measurements and which are still guesses.

Most of what follows was measured in a Linux sandbox with the `claude` CLI available and **no
provider API keys** — that constraint shapes what could and could not be verified there. The
`anthropic:` results in §2.7 came from a real key on the repo owner's machine; nothing else has
been run against a real provider.

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
| `anthropic:` backend | **live-verified** | real API, twice (cold + post-fix) — §2.7 |
| Raw-API cache behaviour | **ANSWERED: no lag** | §2.7 |
| `gemini:` backend | **wire format only** | mock; never saw a real response |
| `openai:` backend | **wire format only** | mock; never saw a real response |
| Gemini 1M context for a real repo | **UNVERIFIED** | no key; §5 of the spec assumes it |

The question this document was originally built around — does the raw API share the CLI's cache
write-visibility lag? — **is answered. It does not.** See §2.7; it changes the phasing.

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

### 2.7 The raw API has no cache write-visibility lag — and this changes the phasing

Run against the real Anthropic API (`anthropic:claude-opus-5`, 56,180-char payload, per-run
nonce so turn 1 was cold):

| | raw `input_tokens` | cache_read | cache_creation | total input |
|---|---|---|---|---|
| turn 1 (cold) | 19 | 0 | **20,345** | 20,364 |
| turn 2 (~4s later) | 23 | **20,345** | 0 | 20,368 |

**Turn 2 read the entire prefix back from cache about four seconds after turn 1 wrote it** —
20,345 of 20,368 input tokens, 99.9%. Contrast §2.4, where the CLI re-wrote the whole prefix on
a back-to-back turn and saved nothing.

Both columns are directly observed. The first run of this experiment could only infer turn 1's
cache write from what turn 2 read back, because the adapter bug below discarded it; the re-run
after the fix reports it outright, and the two agree (inferred ~20,343 vs observed 20,345 — the
difference is nonce length between runs).

Consequence for the plan: the cheap-follow-up-turn economics **hold on the raw API and do not
hold through the CLI**. That promotes `anthropic:` from a Phase 2 optimisation to something
worth building early — it is the difference between multi-turn triage being nearly free and
costing full freight every turn. Spec §7 and the phasing in §12 updated accordingly.

Side-by-side on the same 56k payload, turn 2 of a back-to-back pair:

| backend | cache share of input | cost delta |
|---|---|---|
| `claude-cli:` | 50.0% (its own system prompt only) | none |
| `anthropic:` | ~99.9% (the whole prefix) | not reported by the API |

#### The bug this exposed, and the test hole that hid it

The first run reported `turn 1: in=19 cached=0 created=0` and the verdict misread it as
"ALREADY WARM". Both were wrong, for two separate reasons:

1. **`AnthropicBackend` never populated `cache_creation_tokens`.** The field existed on
   `Completion` and was set by `ClaudeCliBackend`, but the Anthropic adapter dropped it — so
   turn 1's cache write was invisible and the verdict logic had nothing to key on.
2. **Anthropic's `input_tokens` counts only tokens neither read from nor written to cache.** A
   20k-token cached prefix reports `input_tokens=19`. Reporting that field alone claims we sent
   19 tokens. The fix sums raw + read + creation; `ClaudeCliBackend` had the same flaw and got
   the same fix.

The mock in `check_backends.py` returned `cache_creation_input_tokens: 0`, so the missing field
was invisible to the test suite: **the assertions had a hole in exactly the place the adapter
had a bug.** The mock now returns the real observed numbers (19 / 0 / 20,343) and asserts both
that `cache_creation` survives and that `input_tokens` sums to 20,362 rather than 19.

Both bugs are fixed and the re-run confirms the result directly, so nothing in §2.7 rests on
inference any more. The sequence is worth remembering as a pattern: a **null-looking result was
actually two reporting bugs stacked on a positive one.** `in=19 cached=0 created=0` read as
"nothing happened"; it was really "everything happened and we logged none of it."

---

### 2.8 Shipped kopipasta spins forever when piped into

Not a spec issue — a live bug in `main` today. `kopipasta . -t task < /dev/null`:

| | before | after |
|---|---|---|
| exit | 124 (killed at timeout) | **8** |
| stdout in 10s | **5,551,947 bytes**, 4,481 tree redraws | 394 bytes |
| wall clock | unbounded | 0.33s |

`click.getchar()` raises `[Errno 6] ... '/dev/tty'`; the broad `except Exception` catches it,
prints, and calls `click.pause()` — a **no-op without a tty** — so nothing throttles the retry.
~450 redraws/sec, ~2 GB/hour into the caller's capture buffer, at 100% CPU, and
`quit_selection` is unreachable because no key can ever be read.

Fixed in `kopipasta/interaction.py` plus the selector loop; see spec §11.1b for the layered
design and why moving the TUI behind a subcommand was the *weakest* of the available fixes.
Ten regression tests in `tests/test_interaction.py`, including an end-to-end one that pipes into
the real entry point and asserts bounded output and a fast non-zero exit.

The generalisable lesson: **`except Exception: ...; continue` around a blocking read is an
infinite-loop generator.** Fixing the guard alone would have closed this instance; making
recurring failures terminate is what closes the class.

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
7. **A provider's `input_tokens` may exclude cache traffic.** Anthropic's counts only tokens
   neither read from nor written to cache, so a 20k cached prefix reports `input_tokens=19`.
   Report the sum of raw + read + creation, or a cached call looks like it sent nothing. See
   §2.7.
8. **A mock that returns zeros cannot catch a dropped field.** `check_backends.py` asserted on
   `cache_creation` never, and the mock returned `0` for it, so an adapter that silently
   discarded the field passed 21/21. Mocks should return *distinctive non-zero* values for
   every field the adapter is supposed to map — a zero is indistinguishable from "not read".
9. **`claude --bare` breaks auth** in this sandbox. Unrelated to the design, but it cost a
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

## 5. Open questions

The original open question is closed — see §2.7. What remains:

**Gemini has never been exercised against a real payload.** Its caching is implicit, so a cache
miss there is inconclusive rather than a defect, but the **1M-context claim underpinning use
case A is still an assumption**. Spec §7 recommends Gemini for triage on the strength of that
context window; nothing here has tested it.

```bash
export GEMINI_API_KEY=...           # shell env; adapters never write keys to session files
uv run python spike/livecheck.py gemini openai
```

**Whether the no-lag result survives a realistic payload.** §2.7 used 56k chars (~20k tokens).
The design targets 400k–1M. Cache behaviour, TTL and minimum-cacheable-prefix rules may differ
at that scale, and a 5-minute ephemeral TTL is short relative to how long an agent might sit
between turns. Worth one run at full size before betting the phasing on it.

**Whether `claude-cli:`'s lag is a lag at all.** §2.4 showed a stable prefix reused across runs
a minute apart but not back-to-back. That is consistent with write-visibility delay, but also
with the CLI varying something in its own prefix between rapid invocations. Not worth chasing
unless `claude-cli:` stays in the design.

---

## 6. Next actions, in order

1. Fix `estimate_tokens` (§2.5) — the budget ladder is blocked on it.
2. Phase 0 from the spec: injectable policies, `--json` plumbing and the stdout/stderr
   contract, `.kopipasta/` layout, per-project cache fix.
3. Phase 1: `pack` / `apply` / selection grammar / budget ladder / sessions — **and
   `anthropic:` alongside `exec:`**, not after it. §2.7 moved that adapter forward: it is the
   only backend measured to make follow-up turns actually cheap.
4. Run §5's Gemini check before committing to Gemini-for-triage in the docs.
5. Re-run `livecheck anthropic` at 400k+ to confirm the no-lag result holds at target scale.
