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
| `gemini:` backend | **live-verified** | real API; implicit caching fails, explicit works — §2.9 |
| Gemini cache lifecycle (TTL/close/reap) | **live-verified** | 99.9% reuse, 0 orphan resources after |
| `openai:` backend | **wire format only** | mock; never saw a real response |
| Gemini 1M context for a real repo | **CONFIRMED** | `inputTokenLimit: 1048576` on `gemini-3.7-flash` |

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

### 2.9 Gemini implicit caching gives 0% on the access pattern we actually have

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
10. **"Implicit caching" is not caching you can budget against.** Measured, Gemini's gave 0% on
    "same repo, different question" and 100% on exact request repeats — the opposite of what an
    oracle needs (§2.9). If a provider offers an explicit cache, the implicit one is a bonus,
    never the plan.
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
    Linux. Case folding is a per-platform decision, not a portability helper.
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

The original open question is closed — see §2.7. The Gemini one is closed too — see §2.9.
What remains:

**`openai:` has still never seen a real response.** Wire format verified against a mock only.
Its cached-token accounting (`prompt_tokens_details.cached_tokens`) is unexercised, and by the
§2.9 lesson we should assume its implicit caching does nothing for us until measured.

```bash
export OPENAI_API_KEY=...           # shell env; adapters never write keys to session files
uv run python spike/livecheck.py openai
```

**What the Gemini explicit cache actually costs at target scale.** §2.9 proves the mechanism at
16k tokens. The design targets 400k–1M, where storage rent is 25–60× larger per hour and the
break-even between "keep the cache warm" and "re-send the payload" moves. The 300s default TTL
is a guess that has not been costed against a real session's think-time between turns.

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
   only backend measured to make follow-up turns actually cheap. `gemini:` joins it on the
   strength of §2.9, with the caveat that its cache is a resource the session must own and
   release, not a per-request flag.
4. ~~Run §5's Gemini check~~ — done, §2.9. Gemini-for-triage stands, but only *with* explicit
   `cachedContents`; the docs must not imply the caching is automatic.
5. Re-run `livecheck anthropic gemini` at 400k+ to confirm both the no-lag result and the
   explicit-cache economics hold at target scale, and to cost the TTL default.
