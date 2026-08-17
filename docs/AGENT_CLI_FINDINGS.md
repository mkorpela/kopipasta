# Findings: Agent-CLI Spike

Companion to `AGENT_CLI_SPEC.md`. The spec is the design; this is the **empirical log** —
what was actually run, what the numbers were, what broke, and what is still assumption.

Read this first if you are picking the work up. The spec reads as if everything is settled;
this file tells you which parts are load-bearing measurements and which are still guesses.

Most of what follows was measured in a Linux sandbox with the `claude` CLI available and **no
provider API keys** — that constraint shapes what could and could not be verified there. The
`anthropic:` results in §2.7 came from a real key on the repo owner's machine; the `gemini:`
results in §2.9 from a real key on Windows and again on macOS. `openai:` has still never been
run against a real provider (§5).

Three numbers in here have now been taken on more than one machine, and **one of them moved**
(§2.9). Where a figure has an `n` next to it, that is why. Treat anything without one as a
single sample.

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
| `gemini:` backend | **live-verified** | real API; explicit caching works, implicit is a coin flip — §2.9, §2.15 |
| Gemini cache lifecycle (TTL/close/reap) | **live-verified** | 99.0% reuse at 70k tokens on Windows, 0 orphan resources after — §2.15 |
| End-to-end Patch + Apply loop | **live-verified** | `ask --mode patch` -> `apply current --dry-run` -> `apply current --verify` (32 tests green) — §2.15 |
| Two-pass budget demotion | **live-verified** | 401k unbudgeted -> 83 demoted (pass 1) + 6 demoted (pass 2) -> 29,539 under 30k budget — §2.15 |
| `openai:` backend | **wire format only** | mock; never saw a real response |
| Gemini 1M context for a real repo | **CONFIRMED** | `inputTokenLimit: 1048576` on `gemini-3.7-flash` |
| Hosted sandbox (`claude-cli:` only backend) | **live-verified** | floor 34,382 → **7,070** denying all tools; `--json-schema` 2×; sonnet 1M ctx — §2.14 |

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

> **Superseded.** The floor measured ~29.3k here and **~34k** when re-measured days later
> (§2.14). It is set by a system prompt we do not own and it moves; treat the shape below as
> the finding and §2.14 as the current number.

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

### 2.5 `estimate_tokens` is 44% low — and the ratio is per provider

> **Resolved.** See §2.12 for the Gemini measurements and the per-provider fix that landed.


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
costing full freight every turn. Spec §6 updated accordingly; the phase ordering lives in `HANDOFF.md`.

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

Fixed in `kopipasta/interaction.py` plus the selector loop; see spec §12 for the layered
design and why moving the TUI behind a subcommand was the *weakest* of the available fixes.
Ten regression tests in `tests/test_interaction.py`, including an end-to-end one that pipes into
the real entry point and asserts bounded output and a fast non-zero exit.

The generalisable lesson: **`except Exception: ...; continue` around a blocking read is an
infinite-loop generator.** Fixing the guard alone would have closed this instance; making
recurring failures terminate is what closes the class.

### 2.9 Gemini implicit caching cannot be budgeted on; explicit caching can

The `gemini:` adapter shipped with no cache control at all — it posted a bare `generateContent`
and relied on implicit caching. Measured against the real API (`gemini-3.7-flash`, 58,232-char
payload, ~16.3k tokens, well over the documented 4,096-token minimum):

| suffix | result | n |
|---|---|---|
| **new** question, byte-identical prefix | miss | 0/7 |
| **exact repeat** of a whole earlier request | HIT 12,263 | 5/5 |

Perfect separation, and always the same block-aligned 12,263 tokens. **Timing is not the
variable** — a new suffix missed at t+45s, t+51s and t+98s, while a repeat hit 2s after its own
miss. So this is *not* the `claude-cli:` write-visibility lag of §2.4. Mechanically it is a
prefix cache (12,263 < 16,288, and the model re-generated: output and thought counts differed
between two hits), but the lookup only succeeded on whole-request identity.

The consequence is the only part that matters: **"same repo, different question" is exactly the
pattern that gets nothing**, and an exact-repeat hit is worthless to an oracle — you already
have that answer.

Explicit `cachedContents`, same payload, three different suffixes, first try, no warm-up turn:

```
created cachedContents/...   totalTokenCount 16,277
explicit suffix A   prompt=16,292  cached=16,277   99.9%
explicit suffix B   prompt=16,293  cached=16,277   99.9%
explicit suffix C   prompt=16,291  cached=16,277   99.9%
```

Both arms now run in `livecheck` back to back, on the same model, seconds apart:

| arm | turn 2 cache share |
|---|---|
| `gemini` (explicit `cachedContents`) | **99.9%** (16,277 / 16,293) |
| `gemini-implicit` (control, no cache resource) | **0.0%** (0 / 16,290) |

So the §2.7 lesson generalises: **on both providers the reuse has to be asked for.** Neither
gives you prefix economics for free. We knew that for Anthropic and wrote the adapter
accordingly; for Gemini we wrote it in the module docstring and then did not implement it.

#### Re-measured on macOS: the control arm is not 0%, it is a coin flip

The `0.0%` above was a real reading, but it does not reproduce. Re-run on the macstudio
(same model, same payload, 8 consecutive runs of the control arm):

| arm | turn 2 cache share | n |
|---|---|---|
| `gemini` (explicit `cachedContents`) | **99.9%** every time | 4/4 |
| `gemini-implicit` (control) | **74.3%** (12,264 / ~16,507) | 6/8 |
| `gemini-implicit` (control) | **0.0%** | 2/8 |

The hit is always *exactly* 12,264 tokens — the same block-aligned figure as the exact-repeat
hit in the table above, so it is the same prefix-cache mechanism, now reaching a case it did not
reach before. Whether Google changed the lookup or the earlier 0/7 was an unlucky window cannot
be settled from here; either way **the "implicit gives you nothing" claim is retired.**

The conclusion it was supporting survives, and is in fact better supported by the new numbers
than by the old ones. The argument was never "implicit returns zero" — it is **"implicit is not
a promise."** 99.9% on every single run is something you can put in a budget; 74.3% on six runs
out of eight, with no way to tell in advance which run you are on, is not. An intermittent
optimisation is worse than an absent one for planning purposes, because it sets an expectation
it does not keep.

Two process notes, both of which cost a re-run to learn:

- **The number was quoted from one measurement.** A single reading of an *opportunistic*
  provider behaviour is a sample, not a constant, and it went into a table that reads like a
  constant. Anything a provider does "on a best-effort basis" needs an `n` next to it.
- **The instrument disagreed with itself and the wrong half was believed.** The same run
  printed `VERDICT: PREFIX NOT REUSED` and `CACHE: 74.3%`. See the verdict bug below.

#### The cache is rented, and that changes the design

Anthropic's ephemeral breakpoint is a flag on a request. Gemini's `cachedContents` is a
**resource billed per token-hour for the whole TTL, whether or not turn 2 ever arrives.** A
leaked cache is a meter running with nobody watching — the failure mode is a slow bill, which is
exactly the kind nobody notices. `GeminiBackend` therefore owns a lifecycle, defence in depth:

1. **TTL is always explicit and clamped** (default 300s, max 3600s). The server default is never
   used, and `cache_ttl_s=0` cannot become "unbounded".
2. **`close()`** deletes it; changing the prefix deletes the superseded one immediately rather
   than letting it run out its TTL unused.
3. **`atexit` sweep** for anything still live if we die before `close()`.
4. **`displayName="kopipasta-<hash>"`** so an orphan is identifiable, plus `reap_orphans()` to
   delete them without waiting for expiry.
5. **Cache-create failure degrades to inline** rather than aborting — a payload under the
   provider's minimum should cost money, not fail the run. The reason is recorded in
   `cache_disabled_reason` instead of vanishing.

Verified after a live run: `cachedContents.list` returned 0 resources.

#### The short TTL bought a correctness cliff, and the first version fell off it

Bounding the rent creates a failure the unbounded version did not have: **the session can
outlive its own cache.** The first implementation kept sending the expired name, and the live
API answers

```
HTTP 403: {"error":{"code":403,"message":"CachedContent not found (or permission denied)"}}
```

Measured with `cache_ttl_s=15` and a 25s gap. Worth stating plainly because the rationale
comment above the constant *claimed* "if a session outlives the TTL the next turn simply
re-creates it" — the reasoning was written and never implemented, and nothing tested it. A short
TTL is not free; it converts a cost risk into an availability risk, and both need code.

Two layers, because one is not enough:

- A **local monotonic deadline** with a proportional margin, so the common case avoids a wasted
  round trip. This is an optimisation and is *not* trusted for correctness — it depends on a
  local clock, and a suspended process invalidates it.
- A **403 retry**, exactly once, keyed on `"CachedContent not found"` specifically rather than on
  the 403 status, since Gemini shares that code with real permission errors. This is what
  actually guarantees the session survives; it holds under clock skew, process suspension and
  server-side eviction alike.

Fixing it exposed a third bug of the §2.7 family: the re-created cache was reported as
`cache_creation_tokens=0`, i.e. **a turn that paid for a second full cache write looked free.**
The cause was inferring "did we create?" from whether a handle existed before and after the
call, which cannot see a replace. The adapter now asks the cache layer what it actually did.
That is the same mistake as §2.7 one layer down: *a real cost hidden by a reporting bug*, and it
would have quietly understated the cost of exactly the long sessions the cache exists to serve.

#### Is the TTL explicit? Two senses, two answers

**On the wire, yes** — `ttl` is set on every create, the server default is never used, and the
value is clamped to `[1s, 3600s]` so neither `0` nor a typo'd `10**9` can become open-ended
rent. That is the sense that closes the leak.

**In the design, no** — `DEFAULT_TTL_S = 300` applies silently and nothing in this repo ever
overrides it. 300s is a *bounded* guess rather than a leak, so it is safe, but it is still a
guess: it has never been costed against how long a real agent sits between turns, and at the
400k–1M payloads the design targets the rent per hour is 25–60× larger. See §5.

#### Three faults stacked, and only one was the bug

1. **The adapter never asked for caching.** The real defect.
2. **`livecheck`'s verdict was Anthropic-shaped and could not express a Gemini pass.** It keyed
   everything on `cache_creation_tokens`, which the Gemini completion API has no field for, so
   `wrote_on_1` was structurally always `False`. Trace a *perfect* explicit cache through the old
   logic and it prints "ALREADY WARM — Inconclusive". 99.9% reuse reported as a non-result.
3. **§5 of this document pre-excused the failure**: *"its caching is implicit, so a cache miss
   there is inconclusive rather than a defect."* That sentence is why nobody looked. Wrong on
   both halves — the caching did not have to be implicit, and the miss was a defect.

The adapter now carries the creation cost across from the `cachedContents` resource into
`Completion.cache_creation_tokens`, so the same verdict logic works for both providers; and the
verdict gained an explicit branch for "we asked for caching and turn 2 read nothing back", which
is a failure *we* own rather than a fact about the provider.

One methodology bug found by fixing this: the two arms initially shared one prefix, so the
explicit arm warmed the implicit cache and the control arm read 12,263 tokens on a supposedly
cold turn 1. The nonce is now rebuilt **per arm**, not per run. The old verdict flagged it
("unexpected — the per-run nonce should have made turn 1 cold"), which is the one thing that
went right.

#### Fault 2 was fixed only where it had been noticed

Re-running on macOS turned up two more defects in the measuring instrument. Neither is in
shipped code, which is the point: **`livecheck` is the thing that decides whether the central
economic claim is true, and it was wrong twice about that claim in the same run.**

1. **The verdict's structural blind spot was patched for `explicit` arms only.** The code
   comment describes the trap exactly — a provider with no cache-creation counter makes
   `wrote_on_1` permanently `False`, so every branch falls through — and then the guard was
   wired into the `explicit` branches and nowhere else. The control arm has the identical
   blind spot, so a run that read 12,264 tokens back printed `PREFIX NOT REUSED — turn 2
   re-wrote 0 tokens`, with `CACHE: 74.3%` on the very next line. The `else` was also citing
   `cache_creation_tokens` as its evidence, a field that is structurally 0 on this provider
   whatever happens; it now cites `cached_tokens`, which is the number actually being claimed.
   *A fix aimed at a specific symptom does not close the class it came from.*

2. **`max_tokens=256` made the harness unable to complete a turn.** Reasoning tokens are billed
   against the output budget, so `gemini-3.7-flash` spent 241 thinking and 11 answering, and the
   §2.8 truncation guard correctly aborted turn 2 — leaving the explicit arm with **no cache
   measurement at all** and a non-zero exit. The guard did its job; the harness was calibrated
   against a non-reasoning model and nobody re-checked the budget when the model changed in
   `5aa8df8`. Now `LIVECHECK_MAX_TOKENS`, default 2048. Note the shape of this one: a correct
   new guard converted a silently-wrong number into a loud failure, which is the trade the guard
   was built for.

#### And fixing *that* left the class open a third time — found by dogfooding

The fix for (1) was reviewed by pointing `spike/oracle.py` at the file it had just changed and
asking it to attack the change. It found the branch **above** the one that had just been fixed:

**`ALREADY WARM` sat above both `explicit` FAIL branches, so a broken explicit cache could exit
0.** That branch fires on `not wrote_on_1 and c1.cached_tokens > 0` — conditions a *broken*
adapter satisfies whenever turn 1 happens to catch an implicit hit. The run then printed
"Inconclusive", never incremented `failed`, and exited 0. Enumerated over the plausible token
counts:

| scenario | old verdict | exit | new verdict |
|---|---|---|---|
| explicit healthy | `REUSED, NO LAG` | 0 | unchanged |
| **explicit broken, turn 1 implicitly warm** | **`ALREADY WARM`** | **0** | **`FAIL`** |
| explicit broken, turn 1 cold | `FAIL` | 1 | unchanged |
| explicit, `LIVECHECK_NONCE` pinned (legitimately warm) | `ALREADY WARM` | 0 | unchanged |

So whether the harness noticed a broken cache depended on whether the provider's opportunistic
cache warmed turn 1 — the same coin flip measured at 74.3% just above. **The two defects
compose:** the implicit caching that turned out to be more active than documented is exactly
what would have hidden a broken explicit adapter. Neither finding is dangerous alone.

Two things worth stating plainly:

- **This is trap #27, committed in the act of writing trap #27.** The blind spot was fixed in
  the branch where the symptom had appeared, and the identical structure one branch up was not
  even looked at — while writing the lesson that says to look. Knowing the rule is not applying
  it; only re-reading the neighbours is.
- **`claude-cli:` has never been able to produce a result from this harness.** It reads ~50% of
  input from its own system-prompt cache on every call, so `c1.cached_tokens > 0` is *always*
  true and that arm lands in `ALREADY WARM` every run, permanently "Inconclusive". §2.7 records
  the verdict misreading a `claude-cli` run this way once and treats it as a one-off; it is
  structural. `cached_tokens > 0` is a weak proxy for "our prefix was warm" — those tokens may
  belong to somebody else's prompt. Not fixed here: telling the two apart needs a live `claude`
  run to calibrate against, and there is no measurement to write that code from.

The third finding was cosmetic but the same species: the new implicit-reuse branch asserted "it
is not all of the prefix", which is the Gemini 74.3% hardcoded into a message every non-explicit
provider reaches, and would be simply false on a provider that serves 100%. It reports the share
now and characterises nothing.

### 2.10 Dogfooding: what the oracle found in its own repo

The §11.2 audit was run through `spike/oracle.py ask --mode triage --backend gemini:` against
this repo, rather than by reading. Worth logging because the hit rate was uneven in a specific,
predictable way.

**It found things greps could not.** The editor launch (`prompt.py:141`, `config.py:133`) is a
blocking-forever path — `$EDITOR` defaults to `vim`, and `--edit-template` on a pipe waits for a
`:q` nobody can type — reachable from `_handle_utility_commands` *before* any guard. It appears
in no search for `input(`/`click.` because it is `subprocess.call`. Semantic search found it;
five different lexical greps did not. I discounted it on the first pass and it was right.

**Its line numbers were wrong, again.** It cited `patcher.py:587/670` for confirmations that
live at 914/1035, and claimed `main.py:269` reached `TreeSelector` unguarded when `run()` guards
internally. Both are §2.6 repeating: **file-level attribution was reliable (0.95 on patcher.py,
the correct answer), line-level was not.** Treat the oracle as a search engine over meaning,
never as a citation.

**Missing context is the tell.** Every wrong answer came with the relevant file listed in
`missing_context` — it flagged `tree_selector.py` as absent in exactly the run where it wrongly
accused `main.py` of not guarding the selector. The field is worth branching on: a confident
claim about a file the model never saw is a guess wearing a confidence score.

#### Three defects the dogfooding run exposed in the spike itself

1. **`oracle.py` wrote session artifacts with the platform default encoding.** `open(path, "w")`
   is cp1252 on Windows, so the first payload containing a `—` crashed the run mid-write. Every
   artifact write now pins `encoding="utf-8"`.
2. **`GeminiBackend` accepted truncated answers as successes.** The finish-reason check only
   fired when the text was *empty*: `if not text and finishReason not in (None, "STOP")`. A
   `MAX_TOKENS` stop with partial text sailed through, and under `responseSchema` that means
   JSON ending mid-string. The observable result was a triage run reporting `"ok": true` beside
   `"triage": null` — the worst possible pair, because a caller branching on `ok` proceeds with
   no answer. The check now runs regardless of whether text came back.
3. **Reasoning tokens are billed against `maxOutputTokens`.** This is why (2) triggered at all
   with a budget of 8192: thinking consumed nearly all of it and the answer got the remainder.
   `Completion.output_tokens` now sums `candidatesTokenCount + thoughtsTokenCount` — reporting
   only the former understates the turn, which is the §2.7 mistake a third time — and the
   `MAX_TOKENS` error names both numbers, turning "I asked for 8192 and got 318" into something
   actionable.

`oracle.py` now also treats an unparseable triage result as a failed call (`ok: false`,
exit 3) rather than a silent null.

### 2.11 Dogfooding round two: the oracle attacking a fix, and its limits

The per-project cache fix (§11.3) was written from an oracle triage of `cache.py`, then the
*fix* was fed back with "attack this". Both directions produced real defects, and the second
direction produced one clean falsehood — worth recording because the falsehood is the
characteristic failure mode.

Real, and fixed as a result of the attack:

- **Subdirectory fragmentation.** Keying on cwd gave `repo/` and `repo/src/` separate caches;
  paths stored cwd-relative would not resolve from the other. Now keyed on the nearest `.git`
  ancestor with paths stored root-relative.
- **macOS case-folding.** `os.path.normcase` is a no-op on POSIX, but the default macOS
  filesystem is case-insensitive, so `/Users/me/Repo` and `/Users/me/repo` — one directory —
  produced two caches. Folding is now darwin-only; doing it on Linux would merge two *real*
  projects, which is the opposite bug.
- **`Path.home()` raises `RuntimeError`** when no home can be determined, crashing the CLI over
  a cache. Now falls back to the temp dir.
- **`os.replace` on Windows** fails with `PermissionError` while another process holds the
  destination open — a lost write, silently. Now retried.

Rejected: "a moved or renamed repo loses its cache" is not a defect, it is a cache.

**The falsehood is instructive.** Asked which of the new tests would still pass if the fix were
reverted, the oracle named six, including the key tests and the concurrency test. Measuring it
with `git stash` gave a different answer: **13 of 16 failed**, and the three that passed were
exactly the "don't break the feature" tests. The model was reasoning about tests it had read
but never run. It is worth doing this stash-and-rerun on any test written alongside its fix —
a test that passes against the broken code tests nothing, and this is cheap to check and
impossible to guess.

#### The machine found what neither the tests nor the oracle did

Listing the *real* `~/.cache/kopipasta` after a green suite showed a dozen stray project
directories and a `last_task.txt` containing `"Refactor logic"` — a fixture string from
`test_main.py`. **The test suite had been writing into the developer's real home**, and before
the cache was keyed per project that meant overwriting the single global file the developer's
own sessions depended on. `test_main.py` isolates `XDG_CONFIG_HOME` and stops there, which
looks careful and is not.

No unit test catches this, because the pollution *is* the test run. An autouse fixture in
`tests/conftest.py` now points `HOME`/`USERPROFILE`/`XDG_*` at a temp directory — which also
covers the subprocess tests, since `Path.home()` re-reads the environment on every call.
Generalised: **after a suite that touches user state passes, go and look at the user state.**

---

### 2.12 The estimator, measured per provider and then verified against a bill

§2.5 measured 2.50 chars/token and the constant was set there. That measurement came from
`cache_creation` deltas through `claude-cli` — **Claude's tokenizer** — while the configured
provider in this repo is Gemini. Four fresh payloads through Gemini's free `countTokens`:

| payload | chars | real tokens | chars/token |
|---|---|---|---|
| `--all`, skeletons + structure blob | 73,035 | 21,384 | 3.42 |
| dense code (`core/ask.py`, `patcher.py`) | 99,608 | 25,728 | 3.87 |
| mixed (code + spec + skeletons) | 93,839 | 24,919 | 3.77 |
| prose (three design docs) | 112,426 | 30,053 | 3.74 |
| **total** | **378,908** | **102,084** | **3.71** |

The constant was therefore ~48% pessimistic on Gemini: `--budget 400k` shipped ~273k real
tokens, and the ladder demoted about a third of what would have fit.

Two things worth keeping from this:

- **The spread within a provider is small; the spread between providers is not.** 3.42 to 3.87
  across every content mix, against 2.50 versus 3.71 between tokenizers. §2.5's "dense code
  tokenises far worse than prose" is directionally right and nearly worthless in magnitude —
  and it is *backwards* here, since the worst-tokenising payload is the one full of skeletons
  and minified JSON, not the one full of code.
- **Pick the lowest measured ratio, not the mean.** Within a provider the two directions are not
  symmetric: over-counting wastes budget, under-counting overshoots the window the flag exists
  to protect. Gemini is set to 3.4, below all four measurements.

**Verified against a real bill, which is the only check that counts.** After calibration, a live
`--all` run estimated **21,854** input tokens and was billed **21,772** — 0.4% high, still on the
safe side. The old constant would have said 29,721.

The general lesson is not about tokenizers. A measurement is scoped to the thing it was measured
on, and §2.5 recorded the number without recording that scope, so a correct measurement became a
wrong constant the moment the default provider changed. Cite the tokenizer, not just the ratio.

### 2.13 Dogfooding round four: the oracle beat 20 passing tests, then said nothing

`session_cmd.py` was written with 20 tests, all green, and reviewed by hand. Pointing
`ask --mode review` at it an hour later returned one finding, `confidence: 0.95`, and it was
right: `session reap --all-projects` passed *this* project's live leases as the keep-list for a
**machine-wide** delete, destroying caches other repos were holding mid-conversation. That is the
same money bug the keep-list was written to prevent, one scope up — and no test could have caught
it, because every test runs in one project.

The second pass is the part worth recording. The fixed code, plus the estimator and patcher
changes, went back with the same instructions and came back with `findings: []` and "safe to
ship". That is **not** evidence of correctness. Reviewing my own wiring immediately afterwards
found a real asymmetry the model had missed: the `countTokens` call can only catch overshoot,
never a false refusal, because the earlier `--strict-budget` check fires before the payload is
rendered and therefore has nothing to count. Defensible, but not what the comment claimed.

So the loop's value is asymmetric, and worth stating plainly: **a finding from the oracle is
cheap and often excellent; an empty finding list means nothing at all.** Round two (§2.11)
produced a confident falsehood, round four produced a confident silence. Both cost the same to
check and only one looks like a problem.

### 2.14 The hosted sandbox: `claude-cli:` is the only backend, and what it costs

Measured inside Claude Code on the web (no provider key of any kind, `claude` on PATH and
authenticated). Full report and proposal: **`docs/SANDBOX_BACKEND.md`**. Headlines:

Capturing the request (point `ANTHROPIC_BASE_URL` at a local server) shows a
`POST /v1/messages?beta=true` of **153,164 bytes**, of which **129,804 — 85% — is JSON schemas
for 38 tools**. The system prompt is only ~15 KB. `Authorization: Bearer` is attached **by the
CLI** from its own OAuth token; the sandbox border injects nothing, and `*.anthropic.com` is in
the proxy's `noProxy` list, so those calls never traverse the egress proxy.

| | tools | measured input | note |
|---|---|---|---|
| deny 4 names | 34 | **34,382** | ~$0.011 warm |
| deny 11 (kopipasta today) | 30 | ~34k | body 118,268 B |
| **deny all 38** | **2** | **7,070** | **4.9× cheaper** |
| `--allowedTools ""` | 38 | — | **no-op**, all tools still sent |
| `--json-schema` | — | **69,064** | **exactly 2×** — a second pass re-sends the prefix |
| cwd repo root vs empty | — | 34,382 / 34,376 | no difference |

**The floor is reducible after all.** Denying every tool takes it from ~34k to ~7k. An earlier
version of this section said `--disallowedTools` did not shrink the request; that compared the
deny-list against the allow-list rather than against a no-flag baseline, and was wrong.
`--allowedTools ""` really is a no-op. Dodging `CLAUDE.md`/skills/MCP via cwd really does save
nothing.

The tool list is environment-specific — `Workflow`, `Artifact`, `DesignSync` are hosted-surface
tools a laptop CLI never ships — so ~34k is a `remote_mobile` number, not a universal one.

The capture that produced these numbers also surfaced the live OAuth Bearer token. Replaying it to skip the floor was investigated and rejected.

`triage` is the default mode and the one that wants the schema, so **the default path is the
expensive one** and nothing currently says so. Against that, `sonnet` and `opus` both report a
**1,000,000-token context window**, so the frontload case works with no API key at all.

Latency is 22–26s per `ask` against ~4s for a raw API call carrying the same payload — harness
startup, not generation.

Do not read a model price ranking out of single samples here: cache state dominates. A warm
sonnet call measured $0.0104 while a cold haiku call measured $0.0508.

---

### 2.15 Live Gemini 3.7 Flash end-to-end verification on Windows (70k tokens, 99.0% cache hit, full lifecycle)

Live run on Windows 11 PowerShell against `gemini-3.7-flash` (August 16, 2026):

#### 1. Single-Shot Live Triage
- Question: *"Which exit codes are reserved for budget violations vs backend errors, and where are they enforced?"*
- Estimated input: **26,356 tokens** (from 89,611 chars @ 3.4 chars/token).
- Actual billed input: **23,885 tokens** (estimate was ~10.3% pessimistic — safe headroom).
- Latency: **4.4s** (1,281 output tokens including reasoning).
- Outcome: Zero-rent one-shot path held (`cached: 0`, no `cachedContents` created for unnamed one-shot turns). Returned 100% schema-compliant JSON correctly mapping `EXIT_BUDGET=6`, `EXIT_BACKEND=3`, and `EXIT_NO_BACKEND=2`.

#### 2. Multi-Turn 70k-Token Explicit Prefix Caching
- **Turn 1 (Cold start with `--session live-cache-test`):**
  - Payload: 260,019 characters (1 edit, 13 ref).
  - Estimated input: 76,476 tokens.
  - Actual input billed: **70,270 tokens**.
  - Cache created: `cachedContents` created with **69,975 tokens** (`cache_creation: 69975`).
  - Latency: 5.5s.
- **Turn 2 (Follow-up question with `--session live-cache-test`):**
  - Payload: 261,557 characters.
  - Actual input billed: **70,701 tokens**.
  - Cache read: **69,975 tokens** (**99.0% cache hit** on the 70k prefix).
  - Only the 726-token question/history suffix was billed as fresh input.
  - Latency: dropped to **3.6s**.

#### 3. Budget Ladder Two-Pass Convergence
- Unbudgeted `--all`: 1,003,663 chars -> **401,465 est. tokens** (warned on stderr).
- Capped `--all --budget 30k`:
  - **Pass 1 (File size estimate):** Demoted 83 files (saving ~376,000 tokens).
  - **Pass 2 (Rendered overhead correction):** Detected 6,718 tokens of remaining overhead and demoted 6 map files to `path-only`.
  - **Final payload:** **29,539 tokens** (cleanly under the 30,000 token limit).
- Strict budget `--all --budget 30k --strict-budget`:
  - Exited **6** (`EXIT_BUDGET`) with `"error": "budget_exceeded"`, `"retryable": false`, and listed all 83 files that would have been demoted.

#### 4. Patch Generation, Dry-Run, Apply & Verification Loop
- **`ask --mode patch`:** Generated a Search/Replace block for `kopipasta/ops.py` (reported `patches: 1`).
- **`apply current --dry-run --json`:** Resolved pointer, checked editable zone against session record, and matched 1/1 hunks without touching disk.
- **`apply current --verify "uv run pytest tests/test_apply.py" --revert-on-fail --json`:** Applied patch, ran verification command (32 tests passed in 15.4s), and returned exit 0 with diffstat metadata.
- **`session diff`:** Successfully compared sha256 content hashes of recorded session files against disk, reporting `changed kopipasta/ops.py (edit)`.

#### 5. Cache Lease Protection & Cleanup
- **`session reap`:** Inspected live Google `cachedContents`, found `live-cache-test` holding an active lease (267s remaining), and correctly protected it (`handed back 0 cache(s); 1 still leased`).
- **`session rm live-cache-test --json`:** Successfully called Google API `DELETE` to release the 69,975-token cache resource, purged the session directory, and reported `"released": [{"tokens": 69975}]`.
- **`session rm live-patch-test --json`:** Released the 10,133-token cache resource and safely cleared the `.kopipasta/current` pointer (`"current_cleared": true`).

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
10. **"Implicit caching" is not caching you can budget against.** Measured on Gemini it went
     from 0% to 74.3%-on-6-runs-of-8 for "same repo, different question", with nothing changed
     on our side — while the explicit cache sat at 99.9% on every run (§2.9). An optimisation
     that arrives on most calls is *harder* to plan around than one that never arrives. If a
     provider offers an explicit cache, the implicit one is a bonus, never the plan.
11. **A hedge written before the measurement will be used to ignore the measurement.** §5 of
    this file said a Gemini cache miss would be "inconclusive rather than a defect". It was a
    defect, and that sentence is why it sat unexamined. Write down what would falsify a claim,
    not what would excuse it.
12. **A verdict function shaped around one provider silently cannot fail for the others.**
    `livecheck` keyed on a field Gemini does not report, so every possible Gemini outcome —
    including a flawless one — landed in "inconclusive". A check that cannot print PASS is not a
    check. Assert that your assertion can fail.
13. **Some caches are rented, not flagged.** Anthropic's breakpoint costs nothing to leave
    behind; Gemini's `cachedContents` bills per token-hour until its TTL. Any cache with a
    lifetime needs an owner, an explicit TTL and a delete path, or the failure mode is a bill
    rather than an error.
14. **Capping a resource's lifetime creates an expiry path you now have to handle.** A short TTL
    trades a cost risk for an availability risk — it does not remove risk. The session outliving
    its own cache produced a hard `403`, and the mitigation has to be a retry on the specific
    error, not a local clock, because a suspended process makes any local deadline a lie.
15. **A rationale comment is not an implementation.** The constant above the TTL said expired
    caches "simply re-create"; nothing did, and no test asked. Comments that describe behaviour
    should be read as claims to be tested, not as descriptions of what the code does.
16. **A partial answer is not a successful one.** Guarding a provider's finish reason only when
    the text is empty lets truncation through as success. Under an enforced schema that yields
    JSON stopping mid-string, which downstream reads as "no result" rather than "cut off" —
    `ok: true` next to `triage: null`. Check the finish reason unconditionally.
17. **Reasoning tokens spend the output budget.** On a thinking model, `maxOutputTokens` covers
    thoughts *and* answer, so a generous-looking budget can leave almost nothing for the result,
    and reporting only `candidatesTokenCount` understates what the turn cost.
18. **`open(path, "w")` without an encoding is a latent Windows crash.** cp1252 cannot encode
    the em dashes and box characters this project's own payloads are full of. Pin `utf-8` on
    every text write.
19. **A blocking call need not look like a prompt.** `subprocess.call(("vim", path))` stalls
    exactly like `input()` does, and matches no grep for `input(`/`click.`. When auditing for
    "what can block", enumerate by *effect*, not by the names of the usual suspects.
20. **A cache of relative paths is a cache of *other projects'* files.** Global state keyed by
    nothing, holding paths valid in more than one repo, does not fail loudly when reused in the
    wrong repo — it succeeds, on the wrong files, because the existence check passes.
21. **Verify a new test against the old code.** `git stash` the fix, run the test, confirm it
    fails. Asking a model which tests are load-bearing got 6 of 16 wrong; the measurement took
    one command.
22. **`os.path.normcase` is a no-op on POSIX**, so it does not save you on case-insensitive
    macOS — and applying `.lower()` unconditionally would collide genuinely distinct paths on
    Linux. Case folding is a per-platform decision, not a portability helper. And it has to
    reach *every* component derived from the path: the cache key folded its hash but not its
    human-readable slug, so one directory could still produce two names, `Repo-<h>` and
    `REPO-<h>`, that agreed on the hash and disagreed on everything else. `Path.resolve()` is
    no help — it expands symlinks but preserves the caller's casing; only `os.getcwd()`
    canonicalises, which is exactly why this stayed invisible until a path was passed in.
23. **A convenience must not be able to prevent startup.** `Path.home()` raises when there is
    no home; an unwritable cache directory raises on `mkdir`. Anything on the "nice to have"
    side of the design should degrade to a warning, never an exception.
24. **Redirect the stream, don't audit the call sites.** 127 `print()` plus 92 `console.print()`
    calls, and an audit still would not cover `rich` or `click`. Pointing `sys.stdout` at
    stderr for the run converts all of them, including third-party ones.
25. **Scanning argv for flags is guessing at a job argparse already does.** A scan for `-h`
    misfires on `kopipasta -- -h`, which names a file — and the failure is not a wrong help
    message but narration silently corrupting the artifact for the entire run.
26. **A stream swap invalidates code that reconfigures "stdout".** `sys.stdout.reconfigure(...)`
    after the swap fixes the wrong handle and leaves the real one on the platform default. Any
    setup that touches streams by name has to run before the swap, or where the handle is known.
27. **Fixing the symptom's location is not fixing the class.** `livecheck`'s verdict had a known
    structural blind spot — described accurately in its own comment — and the guard was added to
    the two branches where it had been noticed and not to the third. The control arm then
    printed `PREFIX NOT REUSED` directly above `CACHE: 74.3%`. When you write the comment
    explaining a trap, grep for the other places you are standing in it. **This trap was then
    hit again while writing it down**: the fix went into the branch with the symptom, and the
    branch immediately above it had the same flaw and was never read. Fixing a chain means
    re-reading the whole chain, in order, including the parts you did not touch.
30. **In an if/elif chain, ordering is a correctness property, not style.** `ALREADY WARM`
    preceded the `FAIL` branches, so an inconclusive-looking result shadowed a real failure and
    the process exited 0. Branch order decides which of several true statements gets to be the
    answer — put the ones that must never be missed first, and enumerate the chain against
    concrete inputs rather than reading it.
31. **A guard whose trigger depends on the provider's mood is not a guard.** Whether the harness
    caught a broken cache depended on whether an opportunistic third-party cache warmed turn 1.
    Two separately-tolerable findings composed into one that hides the other: the more active
    implicit caching of #28 is precisely what would have masked the broken adapter of #30.
32. **Review the fix by attacking the file you just changed.** Both of the above came from
    pointing the oracle at the diff with "attack this", not from re-reading it. The run that
    asked it to confirm a fix returned "none" in every category; the run that asked it to break
    one returned the defect one branch up. Same model, same minute — the question was the
    variable.
28. **An opportunistic provider behaviour measured once is a sample, not a constant.** Gemini's
    implicit cache read 0% on 7 tries on one machine and 74.3% on 6 of 8 on another, unchanged
    code. It went into a table that read like a specification. Anything a vendor describes as
    best-effort needs an `n`, a date, and a re-run before it is quoted.
29. **When two outputs of one run disagree, find out which is lying before you believe either.**
    The same `livecheck` run printed a verdict and a cache share that could not both be true.
    The verdict was the headline, so the verdict was believed — and it was the broken one.
33. **A fallback only catches an encoding that fails to decode.** PowerShell 5.1's `>` writes
    UTF-16LE, so `git diff > changes.txt` on Windows produced a file we read as
    NUL-interleaved mojibake — and NUL is a legal UTF-8 byte, so the *strict* `decode("utf-8")`
    **succeeded**. No exception, nothing to fall back from, only the two BOM bytes replaced.
    Every count in the envelope looked healthy and a review came back confident over garbage.
    Trap #18 said "pin `utf-8` on every write"; this is the read-side half nobody wrote down,
    and it is worse, because the write bug crashed loudly and this one did not. `decode_text`
    now sniffs BOM-less UTF-16 from NUL-byte parity **before** attempting UTF-8, not after.
34. **A caveat on stderr does not reach the party that has to act on it.** When a file decodes
    lossily, the entity about to reason over the damage is the model, and the model never sees
    stderr. The warning had been printed for a long time and bought nothing. The note now goes
    into the payload beside the content (`# FILE: x.md (decoded as utf-8 with 3 unreadable
    character(s))`) and into the envelope as `lossy_decode`. Generalise it: for every warning,
    name who must change behaviour because of it, then check they are on that channel.
35. **A permission you must declare before asking the question is a guess, not a policy.**
    `apply` used to refuse patches against files not selected with `-e`. It fired on the answer
    to its own question, so it mostly blocked *correct* patches — most sharply for `triage`,
    whose entire job is to discover which files matter and which emitted a selection
    (`--from-file` → `ref`) that was by construction unpatchable. Removed; `apply --only` is the
    opt-in replacement, evaluated when the patch is in hand. The observation was worth keeping
    and the refusal was not, so `outside_focus` still reports it.

---

## 4. Where the spike went

`spike/` is **deleted**. It existed to prove the pipeline and make the measurements above
reproducible; the real implementation now does both, so keeping a second copy of the same
logic would only let the two drift apart.

| Was | Is now |
|---|---|
| `spike/oracle.py` | `kopipasta/core/ask.py`, `apply.py`, `map.py` — the real verbs |
| `spike/backends.py` | `kopipasta/core/backend.py` |
| `spike/check_backends.py` | `tests/test_core_backend.py` — 29 tests against the same local HTTP mock |
| `spike/livecheck.py` | **nothing** — see below |

**One capability went with it: live cache measurement.** `livecheck.py` sent the same prefix
twice with different suffixes, against a real provider, and reported whether turn 2 had to
re-write the prefix. That is how §2.4, §2.7 and §2.9 were measured, and no unit test can
replace it — the whole point was to observe a real provider rather than a mock.

`kopipasta session reap` covers the orphan check it also did. If the cache-economics questions
in §5 are worth re-opening, the harness is in git history — `git show "$(git log --diff-filter=D --format=%H -1 -- spike/livecheck.py)^:spike/livecheck.py"` — and it is better
reborn as a marked live test than restored as a spike.

References to `spike/…` further up this document are **historical**: they record how a
measurement was taken, not a command you can still run.

---

## 5. Open questions

The original open question is closed — see §2.7. The Gemini one is closed too — see §2.9.
What remains:

**`openai:` has still never seen a real response.** Wire format verified against a mock only.
Its cached-token accounting (`prompt_tokens_details.cached_tokens`) is unexercised, and by the
§2.9 lesson we should assume its implicit caching gives us nothing we can plan around until
measured — and measured more than once, since the Gemini figure moved between two machines.

```bash
export OPENAI_API_KEY=...           # shell env; adapters never write keys to session files
kopipasta ask --backend openai:gpt-5 --all -q "..." --json   # and read `usage` in the envelope
```

**What the Gemini explicit cache actually costs at target scale.** §2.9 proves the mechanism at
16k tokens. The design targets 400k–1M, where storage rent is 25–60× larger per hour and the
break-even between "keep the cache warm" and "re-send the payload" moves. The 300s default TTL
is a guess that has not been costed against a real session's think-time between turns.

**Whether the no-lag result survives a realistic payload.** §2.7 used 56k chars (~20k tokens).
The design targets 400k–1M. Cache behaviour, TTL and minimum-cacheable-prefix rules may differ
at that scale, and a 5-minute ephemeral TTL is short relative to how long an agent might sit
between turns. Worth one run at full size before betting the phasing on it.

**Whether the ~34k sandbox floor stays put.** It grew from ~29k to ~34k between two
measurements days apart (§2.14). It is set by a system prompt we do not own and will keep
moving, so any budget arithmetic that hardcodes it needs a way to notice when it is wrong —
which is why `docs/SANDBOX_BACKEND.md` §4 proposes a live test marked to skip where `claude`
is not authenticated, rather than a constant.

**Whether `claude-cli:`'s lag is a lag at all.** §2.4 showed a stable prefix reused across runs
a minute apart but not back-to-back. That is consistent with write-visibility delay, but also
with the CLI varying something in its own prefix between rapid invocations. Not worth chasing
unless `claude-cli:` stays in the design.

---

## 6. Next actions, in order

1. ~~Fix `estimate_tokens` (§2.5)~~ — done, §2.12: per-provider ratios, Gemini's `countTokens`
   under `--strict-budget`, verified against a live bill.
2. Phase 0 from the spec: injectable policies, `--json` plumbing and the stdout/stderr
   contract, `.kopipasta/` layout, per-project cache fix.
3. Phase 1: `pack` / `apply` / selection grammar / budget ladder / sessions — **and
   `anthropic:` alongside `exec:`**, not after it. §2.7 moved that adapter forward: it is the
   only backend measured to make follow-up turns actually cheap. `gemini:` joins it on the
   strength of §2.9, with the caveat that its cache is a resource the session must own and
   release, not a per-request flag.
4. ~~Run §5's Gemini check~~ — done, §2.9. Gemini-for-triage stands, but only *with* explicit
   `cachedContents`; the docs must not imply the caching is automatic.
5. Act on `docs/SANDBOX_BACKEND.md` (§2.14): default to `claude-cli:` when it is the only
   backend available, and give the budget ladder the backend's fixed overhead — it currently
   under-reports by 14× on that path.
6. Re-run `livecheck anthropic gemini` at 400k+ to confirm both the no-lag result and the
   explicit-cache economics hold at target scale, and to cost the TTL default.
