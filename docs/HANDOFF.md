# Handoff memo — agent-CLI branch, Windows → macOS

**Branch:** `claude/kopipasta-agentic-cli-ta26qs`
**Base commit:** `5aa8df8` ("update gemini-3.7-flash")
**Green on Windows:** 220 pytest, backend mock checks, `ruff` clean.
**Green on macOS:** 220 pytest, 53 backend mock checks, `ruff` clean, live Gemini arms both
measured, `reap_orphans()` → 0.

**The macOS pass is done.** §3 below is now a record rather than a plan. It found five defects
— one in `cache.py`, four in the `livecheck` harness — and two of those four came from
dogfooding the fixes rather than from re-reading them. §2.9 of `docs/AGENT_CLI_FINDINGS.md`
changed as a result: the implicit-caching number did not reproduce, and the harness could exit
0 on a broken explicit cache. Read that before quoting any cache figure.

One structural caveat carried forward: **`claude-cli:` cannot currently produce a verdict from
`livecheck`** — it always reads ~50% of input from its own system-prompt cache, so it lands in
`ALREADY WARM` every run. Not fixed, because separating those tokens from ours needs a live
`claude` run to calibrate against.

---

## 1. What this branch is for

Two threads, both aimed at the same goal — making kopipasta usable *by an agent* rather than
only by a human at a TUI:

1. **Never stall a caller who cannot answer.** A blocking prompt with nobody on the other end
   is an unbounded hang inside someone else's subprocess, and a harness cannot tell a hang from
   slow work. Spec §12.
2. **Make the Gemini backend's prompt caching real**, because multi-turn against a large
   frontloaded context is the whole economic case for the oracle. Spec §6.

Design docs: `docs/AGENT_CLI_SPEC.md` (what we intend) and `docs/AGENT_CLI_FINDINGS.md`
(what we measured, plus 26 numbered traps). The findings doc is the more useful of the two —
it is the record of things that were not true when assumed.

---

## 2. What is done

### Phase 0 is complete

| Item | Where | Notes |
| --- | --- | --- |
| Injectable interaction policy | `kopipasta/interaction.py` | `human_attached()`, `require_human()` (exit 8), `use_default_without_human()` |
| Exit codes | `kopipasta/main.py` | 1 = usage, 8 = no human. `UsageError` never opens the TUI |
| Per-project cache | `kopipasta/cache.py` | spec §13 |
| Output contract | `kopipasta/output.py` | spec §8 — stdout is the artifact, everything else stderr |

### The interaction policy is deliberately not uniform

`require_human()` (exit 8) where **no safe default exists**: the task prompt, web full/snippet
choice, the TUI, and opening `$EDITOR`. `use_default_without_human()` (narrate on stderr, keep
running) where one **does**: env-var masking → mask, patcher destructive confirm → decline.
Refusing to run is a worse answer than the obviously-correct one, but guessing is worse than
both when there is nothing to guess from.

Two constraints that shaped the patcher work and will bite anyone who refactors it:

- **The refusal must not raise inside `apply_patches`.** Its per-file body has a broad
  `except Exception` that would swallow `NoHumanAttached` and misreport a policy refusal as a
  corrupt patch. Decisions are injected, not raised.
- **`--allow-delete` / `force` must not suppress the prompt when a human *is* present.** The
  flag means "this run may delete", not "delete without telling me".

### Gemini explicit caching

`kopipasta/core/backend.py` `GeminiBackend` creates/reuses/deletes `cachedContents` explicitly, with
TTL clamped to `[1s, 3600s]` (default 300s), `close()`, delete-superseded, an `atexit` sweep,
and `reap_orphans()`. Measured live: **99.9% reuse** vs **0.0%** for implicit caching on the
"same prefix, new question" pattern that actually matters. Implicit caching only ever hit on
byte-identical repeat requests.

TTL expiry returns `HTTP 403: CachedContent not found`, handled by a local monotonic deadline
(an optimisation) **plus** a one-shot retry keyed on the *string* — not the 403 status, which
is shared with genuine auth failures.

### Bugs fixed that predate this work

- **The global cache** (§11.3). It stored *relative* paths in one machine-wide directory, so
  opening repo B did not fail to load repo A's selection — it succeeded, on repo B's own
  `src/main.py`, because the existence filter passed. Task text (prose, often confidential)
  leaked the same way, and `clear_cache()` in one repo wiped every other repo's state. Nothing
  warned in any of the four cases.
- **The test suite wrote into the developer's real home.** Found by listing the actual
  `~/.cache/kopipasta` after a green run: a dozen stray project dirs and a `last_task.txt`
  containing `"Refactor logic"`, a fixture string from `test_main.py`. `tests/conftest.py` now
  isolates `HOME`/`USERPROFILE`/`XDG_*` per test.
- **Silent truncation in `GeminiBackend`.** The finish-reason guard only fired when the text
  was *empty*, so a `MAX_TOKENS` stop with partial text passed as success — under
  `responseSchema` that is JSON ending mid-string, reported as `"ok": true` with
  `"triage": null`. Reasoning tokens also spend `maxOutputTokens`, which is why an 8192 budget
  produced a 318-token answer.

---

## 3. What macOS exercised for the first time — results

Several code paths were written on Windows against documentation rather than a running machine.
All six have now been run on the macstudio. **One was broken.**

1. **`cache._norm_for_key` darwin branch — FOUND A BUG, FIXED.** `os.path.normcase` is a no-op
   on POSIX, so macOS — default filesystem case-*in*sensitive — needs an explicit `.lower()`;
   Linux must **not** fold or two distinct directories collide. That much was right. What was
   wrong: `get_project_key` folded the *hash* and not the *slug*, so one directory produced
   `repo_a-<h>` and `REPO_A-<h>` — two names, one hash. `test_case_differences_do_not_split_one_
   repo_into_two_caches` had never executed on macOS and failed on its first run there.
   Latent in production (the no-argument path goes through `os.getcwd()`, which canonicalises
   case; `Path.resolve()` does **not**, it only expands symlinks) and it would have surfaced
   the moment a root was passed in — which is exactly what a `pack` verb taking a path will do.
2. **`/private/var` symlinking — clean.** Verified directly that one directory yields one key
   across `/var/...`, `/private/var/...`, mixed case, and cwd-vs-explicit. Both
   `find_project_root()` and `get_project_key()` resolve, and no caller skips it.
3. **The editor guard on darwin — holds.** Confirmed by running the real binary, not the test
   double, since the tests monkeypatch `os.startfile` with `raising=False` and that patches
   nothing here. `--edit-template` and `--edit-profile` both exit 8 headlessly, and stdout stays
   empty — the narration is entirely on stderr, so the §11.2b contract holds on this path too.
4. **`os.replace` retry** (`cache.py`). Windows-only dead code on macOS, never triggered, as
   expected. Do not "simplify" it away — the Windows failure was real and reproducible.
5. **Encoding.** All the cp1252 defensiveness is Windows-motivated and inert here. Do not
   remove it; the project is developed on both.
6. **Terminal behaviour — holds.** `human_attached()` checks stdin *and the stream it narrates
   to* (stderr under the redirect). Exercised under a real pty rather than by hand, which is
   worth keeping since it is repeatable:
   - stdin+stderr on a tty, stdout to a file → TUI starts and stays up. `kopipasta >
     prompt.txt` still works from a terminal.
   - `echo ... | kopipasta` → exit 8 in 0.8s. No spin.
   - fully redirected → exit 8, **0 bytes on stdout**.

The suite also leaves the real `~/.cache/kopipasta` absent on macOS, checked by deleting it,
running the suite, and looking (§5) — the `conftest.py` isolation is not Windows-specific.

Command translations from the Windows session:

| Windows PowerShell | macOS zsh |
| --- | --- |
| `uv run python -u spike/oracle.py ...` | now just `kopipasta ask ...` |
| `Select-String -Path x -Pattern y` | `ag y x` or `rg y x` |
| `$env:GEMINI_API_KEY="..."` | `export GEMINI_API_KEY=...` |
| `Get-ChildItem "$env:USERPROFILE\.cache\kopipasta" -Recurse` | `find ~/.cache/kopipasta` |

---

## 4. How to verify the branch

```sh
uv run pytest -q                    # 471, Windows / macOS / Linux
uv run ruff check kopipasta tests
```

The backend wire checks are now part of the suite (`tests/test_core_backend.py`), spinning the
same local HTTP mock — no API key needed.

Live checks (needs `GEMINI_API_KEY`, costs a few cents):

```sh
kopipasta ask --backend gemini:gemini-3.7-flash --all -q "..." --json   # read `usage`
kopipasta ask --continue -q "a different question" --json               # turn 2: expect cached
kopipasta session reap                                                  # must report 0
```

`spike/livecheck.py` measured this directly and is gone (findings §4); the two-turn `ask`
above is the nearest equivalent. **Expect 99.9% reuse on turn 2, every run. Do not expect a
fixed number from implicit caching** — it was 0% on Windows and 74.3% on 6 of 8 macOS runs with nothing changed on our side,
which is the finding, not noise to be averaged away (§2.9 of the findings doc). A single
implicit run tells you nothing; if you need to quote it, run it five times.

`kopipasta session reap` must report `0`. A non-zero number means cached content was left rented on
Google's side — the caches are billed per token-hour until their TTL expires, so orphans cost
money silently. Checked after ~12 cache creations across the runs above: `0`.

### Dogfooding loop (this is how most of the defects above were found)

```sh
export KOPIPASTA_NONINTERACTIVE=1
kopipasta ask --mode triage --backend gemini:gemini-3.7-flash --json \
  -e kopipasta/cache.py -e kopipasta/main.py \
  -q "Concrete defects only, with file and line. ... If a category is clean, say none."
```

What actually works, from ~8 real runs:

- **Ask for concrete defects by category, and give it permission to say "none".** Open-ended
  "review this" produces a restatement of the design.
- **Feed a *fix* back with "attack this."** That direction produced four real defects in the
  cache work and three in the output-contract work, including one I had just introduced.
- **File-level attribution is reliable; line numbers are not.** Three separate runs cited
  plausible, wrong line numbers while naming exactly the right files at 0.95+ confidence.
  Always `rg` before acting.
- **`missing_context` is the tell.** Every wrong answer listed the relevant file as absent. A
  confident claim about a file the model never read is a guess wearing a confidence score.
- **Do not ask it which tests are load-bearing.** It named 6 of 16; `git stash` said 13 of 16.
  It was reasoning about tests it had read but never run.

---

## 5. Working practices this branch established

Worth keeping, because each came from something that went wrong:

- **Verify a new test against the old code.** `git stash push <file>`, run the test, confirm it
  *fails*, `git stash pop`. A test written alongside its fix that passes against the broken
  code tests nothing. This caught a weak test of my own within an hour of writing the rule down.
- **After a suite that touches user state passes, go and look at the user state.** No unit test
  can catch the suite polluting `$HOME`, because the pollution *is* the test run.
- **A rationale comment is a claim, not a description.** The TTL constant claimed expired caches
  "simply re-create". Nothing did, and no test asked.
- **Enumerate blocking calls by effect, not by name.** `subprocess.call(("vim", path))` hangs
  exactly like `input()` and matches no grep for `input(` or `click.`. Five greps missed it;
  semantic search found it.

---

## 6. What is not done & Active TODO

**Every verb in spec §3 is built**: `ask`, `apply`, `map`, `session`, `config`
(`kopipasta/core/`, see §8, §9 and §11 below). 472 tests pass, `ruff` clean.

### Key Design Refinements Agreed:
1. **Decouple `apply` from `ask`:** `ask` is strictly a context oracle (read-only reasoning) that reports `patches: <count>` and saves the response artifact. `kopipasta apply [file|-|current]` is a standalone verb handling worktree checks, patch application, `--verify`, and `--revert-on-fail`.
2. **Session Defaults:** `ask` *always* starts a fresh session by default. Implicit resumption via `current` is removed; explicit continuation uses `--session <id>` or `--continue`.

### TODO List:

1. ~~Build `kopipasta map`~~ — **done**, §11.
2. ~~Build `kopipasta session` CLI helpers~~ — **done**, §11.
3. **`--commit` for `apply`: cancelled, not deferred.** Everything it needs is in place —
   `PatchResult` knows exactly which files this run touched — but a tool whose job is assembling
   context does not need to write git history, and `apply && git commit` is one line the caller
   already knows how to write. Reinstating it needs a reason beyond "spec §11 listed it".
4. ~~The estimator is calibrated to the wrong tokenizer~~ — **done**, §11.
5. ~~Make the parser tolerate an unfenced patch block~~ — **done**, §11.
6. **Revisit Cache Economics at Target Scale:**
   - Re-measure Gemini and Anthropic at 400k+ tokens to cost the default 300s TTL against think-time.
7. **Decide the `n` a cache figure needs before it is quotable** (see below). Unchanged.
8. **Mixed fenced and unfenced patches in one response.** `_parse_unfenced` runs only when the
   fenced parse found *nothing*, so a response with three fenced patches and one unfenced drops
   the fourth and reports `patches: 3`. Detecting it means tracking which byte ranges the fenced
   parse consumed. Left alone deliberately: guessing at partial application is worse than the
   corner case, and no live run has produced this shape.

One addition to that list, from the §3 pass: **the implicit-caching number moved between two
machines.** Before the 400k re-measurement is worth anything, decide what `n` a cache figure
needs to be quotable. The explicit arm was stable across every run on both machines; the
implicit arm was not stable across runs on *one* machine.

**`reap_orphans()` no longer means what §4 says it means** — fixed in §11. Its signature is now
`reap_orphans(base_url=None, *, keep=(), label=None)`: `keep` is the list of live lease resource
names, `label` scopes the sweep to one project. A non-zero count is the expected result of any
`ask --session` run and is not a leak.

---

## 7. Committing

All committed and pushed on `claude/kopipasta-agentic-cli-ta26qs`. Line endings did not churn:
the Windows work landed as LF, and the only CRLF file in the tree is `LICENSE`, which predates
this branch. No `.gitattributes` was needed.

---

## 8. `ask` — what landed, and what dogfooding it found

```
kopipasta/core/
  resolver.py   patterns -> {path: role}, per-pattern match counts   (spec §4)
  budget.py     the demotion ladder, on a calibrated estimator       (spec §5)
  modes.py      template + enforced schema, together                 (spec §10)
  context.py    resolved selection -> (prefix, suffix), zoned         (spec §6/§11.4)
  session.py    the conversation on disk: turns, dedup, cache lease  (spec §7)
  backend.py    none / exec / claude-cli / anthropic / gemini / openai
  ask.py        the verb: orchestration, --json contract, exit codes (spec §8)
```

Verify: `uv run pytest -q` (362), `uv run ruff check kopipasta spike tests`,
`uv run pytest -q` (471).

Three decisions that are load-bearing and not obvious from the spec:

- **`--backend none` is a first-class backend, not a test double.** It runs selection, the
  ladder, rendering, the session record and the whole output contract for real, and hands the
  assembled payload back as the answer. `none:<file>` answers with a canned response instead,
  which is how the triage-parsing and failure paths are exercised. 42 of the tests are
  end-to-end runs of the real verb with no key, no network and no bill.
- **A session's prefix is fixed at turn 1.** It is the cache breakpoint, so it must stay
  byte-identical; re-rendering "the same" selection would miss on every turn the moment a file
  changed. New and changed files therefore ride in the *suffix*, marked as superseding.
  Dedup is the same rule from the other side, and it compares against a manifest of what is in
  the prefix — never against "everything ever sent", because an earlier turn's suffix is gone.
- **A Gemini cache is created only for a named session.** It is rented per token-hour until its
  TTL, so a one-shot question would pay storage for a turn 2 that never comes. Verified live: a
  one-shot `ask` leaves `cachedContents` empty; a `--session` run hands the lease to
  `.kopipasta/sessions/<id>/cache.json` and turn 2 — a different process — read 27,029 of
  37,952 input tokens from it.

### The dogfooding record

Six real defects, all found by pointing `ask` at its own diff with *"attack this"*, none by
re-reading. The pattern from §4 held exactly: the run that asked it to confirm a fix said
"none" in every category; the run that asked it to break one returned the defect.

1. `--strict-budget` was honoured by the first ladder pass and silently ignored by the
   corrective one — so a run under the estimate and over the rendered size demoted anyway and
   exited 0, which is the one thing the flag exists to prevent.
2. The ladder's four stages were snapshotted up front, so a `-r` file demoted to a skeleton in
   stage 1 was missing from the stage-4 list and could never reach path-only.
3. Dedup compared content hashes and ignored the role, so `-e file.py` on turn 2 was answered
   with the 50-line snippet turn 1 sent, and reported as `edit: 1`.
4. Dedup trusted every earlier turn, including files that rode in a *suffix*. Those are not
   replayed, so turn 3 withheld a file the record said had been sent.
5. Only full-text roles were rendered into a later turn's suffix, so a `-m` file first selected
   on turn 2 was counted in `sent["map"]` and never sent at all.
6. Reusing a cache re-derived `expires_at` as `now + ttl`, renewing a lease only the provider
   can renew.

And one found by running the finished thing rather than testing it: `ask` establishes the
output contract for itself, `main` had already established it, and the inner scope saved the
*redirected* stdout as the artifact stream. `kopipasta ask --json > out.json` wrote an empty
file and exited 0. `stdout_reserved_for_output` is now re-entrant, and the test for it goes
through `main` — the tests that called the verb directly could not see it.

Each fix has a test that was checked against the old code with the fix reverted, and fails.

---

## 9. `apply` — what landed

```
kopipasta/core/apply.py     the verb: target resolution, worktree guard, zone,
                            verify, revert, the §8 exit codes
kopipasta/patcher.py        PatchResult / FileOutcome — what the bare list could not say
```

Verify: `uv run pytest -q` (421), `uv run ruff check kopipasta spike tests`,
`uv run pytest -q` (471).

Three things that are load-bearing and not obvious from the spec:

- **A clean worktree is the undo, and everything else only narrows the window.** `--dry-run`,
  the editable zone and `--verify` all reduce the chance of needing it; the reason a 400-line
  one-shot patch is safe to *try* is that `git diff` shows what it did and `git checkout .`
  puts it back. Hence the refusal by default, and hence `--dry-run` not requiring it — a run
  that cannot write cannot need an undo.
- **Exit 4 and exit 5 are the product.** 5 says the worktree is untouched, so a retry is safe;
  4 says it is dirty and there is cleanup to do. `PatchResult.changed` is what separates them,
  and it is the reason the patcher had to stop returning a bare list first (§10).
- **`revert()` only reverts what this run wrote, and only if it was clean beforehand.** A file
  the caller had already modified is theirs. Reverting it to tidy up after a failed `--verify`
  would destroy uncommitted work — a far worse outcome than leaving the patch in place — so
  those are reported in `revert_declined` and left alone.

### Not built, deliberately

`--commit`. Spec §11 listed it, and `PatchResult` already knows exactly which files to stage —
so this was never a difficulty question. **Now cancelled outright** rather than deferred: a tool
that assembles context does not need to write git history, and `apply && git commit` is one line
the caller already knows how to write. Reinstating it needs a reason beyond the spec having
mentioned it.

---

## 10. Dogfooding round three: what using the tool found

Everything in this section was found by *running* kopipasta at itself, not by reading it. The
§4 rules held exactly. The first review — "attack the implemented surface", five categories —
returned `findings: []` and a verdict that the code satisfied its contracts. The runs that
produced defects were the ones that named a specific claim to break and said "I already found
these three, find the rest".

**Six defects, all verified in the code before being acted on:**

1. **A partially applied file was reported as a success.** `_apply_diff_patch` returned `False`
   only when *zero* hunks matched, so 1-of-3 was written to disk and appended to the modified
   list. The counts were already being printed — "(1/2 hunks applied)" — and thrown away at the
   `return`. This made spec §8's exit 4 unproducible, so `apply` would have exited 0 on a
   half-patched file.
2. **The `current` pointer was written only when `--json` was off.** Spec §1's third workflow is
   `ask --json` then `apply current`: the only mode that produces a patch artifact was the only
   mode that left no handle to it. The *reading* rule (an agent must not inherit a racy pointer)
   was sound and stays; write-always and follow-never are two rules, and they had been one.
3. **A follow-up turn recorded `files: {}`.** With no selectors the turn inherits the whole
   prefix, but the record said nothing was in play — so `apply`, which reads the latest turn to
   enforce the Active Workspace, would either reject every patch or read "empty" as
   "unrestricted". The record is now the prefix with this turn's selection laid over it.
4. **Every argparse usage error exited 2**, which spec §8 reserves for "no usable backend — no
   key, no command". A mistyped flag told the caller its credentials were missing. Now 1, via
   `HelpToStdoutParser.error()`; `exit()` is left alone so `--help` stays a success.
5. **`revert()` compared paths raw.** git says `app.py`, a model writes `./app.py`, so
   "was this file already dirty?" answered no and `git checkout --` went over uncommitted work.
   One `normalise_path` in `patcher.py` now serves both the zone check and the revert check,
   because two copies of that rule is how they drift apart.
6. **`editable_set()` collapsed two opposite answers into `None`.** "No record to enforce
   against" and "the record says nothing was editable" are not the same; `return editable or
   None` turned the strictest case — a triage session, all `-r` and `-m` — into the most
   permissive one.

5 and 6 were found by pointing the oracle at `apply.py` an hour after writing it, which is the
§4 pattern working exactly as advertised: it beat a careful re-read of code I had just written.

### The one the tests could never have found

`ask --mode patch` against the configured default model failed with
`backend_not_a_completion`, exit 3, and a hint to disable the backend's file and shell tools.
Gemini has none — it is a raw HTTP call. It had returned a **correct** search/replace patch,
unfenced, and `parse_llm_output` skips anything outside a ``` fence.

The cause is that the PATCH template and the parser disagreed: the template said "every code
block starts with a path comment" and never said to fence it, so a compliant model produced
output the parser could not see the edges of. Two fixes, and the second matters more than it
looks:

- The template now demands the fence, and a test asserts it does. This alone made the live run
  work — `patches: 1`, and `apply current --dry-run` applied 3 of 3 hunks.
- `unparseable_patch` (exit 5, retryable) is now distinct from `backend_not_a_completion`
  (exit 3). "It never tried" and "it tried and the format was wrong" are indistinguishable from
  the patch count and need opposite responses, and sending a caller to reconfigure a backend
  that behaved correctly costs it the one thing it cannot get back.

**Now fixed** (§11): `_parse_unfenced` reads the block the model did not fence. Making the
template stricter fixed the observed failure by asking the model to be careful, which is the
opposite of the format tolerance §14 calls the asset.

### The estimator is calibrated against the wrong tokenizer

`budget.py` sets `CHARS_PER_TOKEN = 2.5`, citing findings §2.5. That measurement — 46,102 chars
→ 18,474 tokens — is the `cache_creation` delta from §2.3's table, i.e. **Claude's tokenizer via
`claude-cli`**. The configured default provider in this repo is Gemini. Measured against
Gemini's own `countTokens` on four real kopipasta payloads:

| payload | chars | real tokens | chars/token |
|---|---|---|---|
| `--all`, skeletons + structure blob | 59,592 | 17,088 | 3.49 |
| dense code (`core/*.py` + `patcher.py`) | 204,961 | 54,951 | 3.73 |
| prose (the two design docs) | 86,496 | 22,915 | 3.77 |
| mixed, 277k | 277,393 | 75,727 | 3.66 |

Stable at ~3.66 across every content mix, so the constant is **47% pessimistic on Gemini**. The
direction is the safe one — it never overflows the window — but `--budget 400k` ships ~273k real
tokens, so the ladder demotes about a third of what would have fit, in a tool whose entire
product is frontloading. Note also that §2.5's "dense code tokenises far worse than prose" is
directionally right and nearly worthless in magnitude here: 3.49 vs 3.77 is an 8% spread.

The real finding is structural: **one global constant cannot serve two providers that differ by
~46%.** Spec §5 already names the fix — count with the provider's `count_tokens`. **Both landed
in §11**, and the corrected estimate was verified end to end against a live call: 21,854
estimated against 21,772 actually billed, 0.4% high and still on the safe side.

---

## 11. Finishing the spec: `map`, `session`, and four defects dogfooding found

Every verb in spec §3 now exists. 472 tests, `ruff` clean.

### `kopipasta map` (`core/map.py`)

A skeleton of the selection, with no model, no session and no network. It reuses the whole
selection grammar (`-e/-r/-m/--all/--changed`, positional paths) and the budget ladder, and
writes nothing to `.kopipasta/` — a verb that only reads should leave no trace, and a test pins
that it does not.

Two decisions worth keeping:

- **Every selected file is rendered as a skeleton, whatever role flag it arrived with.** `map`
  answers "what is in here", and a caller who wrote `-e` did not mean "and please hide it".
- **A demoted file stays listed, with an empty symbol list.** Dropping it would make the map say
  the file does not exist. `--json` separates the two cases: `path_only` names the demotions,
  and a file with genuinely no symbols (`LICENSE`, `.gitignore`) is simply empty in `map`.

The budget needs a corrective pass here for the same reason `ask` does: the ladder works from
file sizes and cannot see the per-file path lines the renderer adds. `--strict-budget` reports
the *pre-demotion* size, because `demote_to_fit` mutates the selection it is handed.

### `kopipasta session` (`core/session_cmd.py`)

`ls`, `show`, `diff`, `rm`, `reap`. Reporting subcommands follow `current` when given no id —
reading is cheap and racy-safe. **`rm` never defaults to `current`**, because "delete the thing
I did not name" is not a default anything should have. Ids are validated against
`^[A-Za-z0-9._-]+$` on top of the existing `.`/`..`/separator/absolute checks, which matters
because `rm` is the one operation in the package that calls `shutil.rmtree`.

`--json` is accepted at both levels (`session --json ls` and `session ls --json`); the
subparser's copy uses `argparse.SUPPRESS` so it cannot clobber the outer one with a default.

### The four defects, and how each was found

**1. Deleting a session leaked its rented cache.** `session rm` removed the only record of the
resource name while the cache stayed rented on the provider. Fixed with `release_lease()`,
called before the directory is removed — order matters, since afterwards nothing knows what to
release. Proven live: `released: [{session: leasetest, tokens: 16329}]`.

**2. The sweep deleted live leases.** `reap_orphans()` is now
`reap_orphans(base_url=None, *, keep=(), label=None)`. Cache display names became
`kopipasta-<projectlabel>-<digest16>` so a sweep in repo A cannot delete repo B's lease. The
money bug was reproduced live before the fix: with `keep`, turn 2 read 16,329 cached tokens;
after an unfiltered sweep, turn 3 reported `cache_creation: 16329` — paying twice for the same
prefix, silently.

**3. `session reap --all-projects` reintroduced that bug one scope up.** Found by pointing
`ask --mode review` at `session_cmd.py` an hour after writing it — the §4 loop working exactly
as advertised, on code that already had 20 passing tests. A lease lives in the project that took
it, so a machine-wide sweep can read *this* project's leases and no others; it would delete a
cache another repo was holding mid-conversation. **The flag is gone**, and the guard is on the
primitive rather than the flag: no command-line path may produce `label=None`. The asymmetry
decides it — an abandoned cache costs storage rent bounded by a TTL of at most an hour, a
destroyed live one costs a full re-creation every following turn. Crash recovery never needed
the wider scope anyway: the leases are still on disk, so `reap` inside that project is correct.

**4. Skeletons hid 37% of every file.** `extract_symbols` dropped single-underscore names — an
API-documentation instinct in a tool whose reader is a model about to change the code.
`core/map.py` showed 2 of its 7 functions. A skeleton that omits them does not read as partial,
it reads as complete, so the model writes a helper that already exists or asks for the whole
file and gives back the saving. Measured cost of including them: **+18%** on a full map (623 →
733 symbols), in a role already an order of magnitude cheaper than file content.

### The estimator, resolved

`chars_per_token(provider)` replaces the global constant; `CHARS_PER_TOKEN = 2.5` is now only
the default for a provider nobody has measured. Gemini is 3.4 — the *lowest* of four fresh
measurements (3.42–3.87 over 379k chars), not the mean, because within a provider the
under-counting direction is the dangerous one. `Payload` carries the ratio, and `--dry-run`
resolves the *planned* backend purely to size the payload for the provider that would have read
it, while still running without a key.

`GeminiBackend.count_tokens()` implements spec §5's exact count. It is called in exactly one
place: the final check before sending, under `--strict-budget`. That flag promises to refuse
rather than overshoot, and no heuristic can keep that promise. It catches overshoot only — the
earlier strict check fires before the payload is rendered, so there is nothing to count yet, and
a refusal there rests on the estimate. That is the safe direction, and buying back the last
percent would mean rendering before deciding.

End-to-end verification against a live billed call: **21,854 estimated, 21,772 actual.** The old
constant would have said 29,721.

### Unfenced patches

`_parse_unfenced` runs only when the fenced parse found nothing, and it is deliberately narrow.
Without a closing fence there is no end marker, so it requires an unmistakable patch marker and
**never accepts a whole-file replacement** — `# FILE: x` followed by two paragraphs of a model
explaining itself would otherwise overwrite `x` with the prose.

This changed an existing test's subject. `unparseable_patch` (5) versus `backend_not_a_completion`
(3) is still the distinction that matters, but an unfenced patch is no longer an example of
either: it applies. The slug split is now pinned with markers that carry no path, which is a
model that tried and got the format wrong.
