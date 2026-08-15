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

### Gemini explicit caching (spike/)

`spike/backends.py` `GeminiBackend` creates/reuses/deletes `cachedContents` explicitly, with
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
| `uv run python -u spike/oracle.py ...` | same (`-u` still worth it) |
| `Select-String -Path x -Pattern y` | `ag y x` or `rg y x` |
| `$env:GEMINI_API_KEY="..."` | `export GEMINI_API_KEY=...` |
| `Get-ChildItem "$env:USERPROFILE\.cache\kopipasta" -Recurse` | `find ~/.cache/kopipasta` |

---

## 4. How to verify the branch

```sh
uv run pytest -q                              # 220, Windows and macOS
uv run ruff check kopipasta spike tests
uv run python spike/check_backends.py         # 53 checks, no API key needed, spins a local HTTP mock
```

Live checks (needs `GEMINI_API_KEY`, costs a few cents):

```sh
uv run python -u spike/livecheck.py gemini gemini-implicit
uv run python -c "import sys; sys.path.insert(0,'spike'); import backends; print(backends.GeminiBackend.reap_orphans())"
```

**Expect 99.9% on the explicit arm, every run. Do not expect a fixed number on the implicit
arm** — it was 0% on Windows and 74.3% on 6 of 8 macOS runs with nothing changed on our side,
which is the finding, not noise to be averaged away (§2.9 of the findings doc). A single
implicit run tells you nothing; if you need to quote it, run it five times.

`reap_orphans()` must print `0`. A non-zero number means cached content was left rented on
Google's side — the caches are billed per token-hour until their TTL expires, so orphans cost
money silently. Checked after ~12 cache creations across the runs above: `0`.

### Dogfooding loop (this is how most of the defects above were found)

```sh
export KOPIPASTA_NONINTERACTIVE=1
uv run python -u spike/oracle.py ask --mode triage --backend gemini:gemini-3.7-flash --json \
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

## 6. What is not done

**The §3 verbs — `pack`, `ask`, `patch`, `apply`, `map`, `session` — are unbuilt.** They are
recognised and exit 1 with "not implemented yet" instead of being silently treated as
filenames (`main.py:_resolve_subcommand`). `pack` is the natural next step and the highest
value: `_print_and_copy` is currently the only artifact emitter and it is reachable *only*
through the TUI, so there is no headless way to produce a prompt today. The output contract
(§11.2b) was built to make `pack` a small change — it should be roughly "select from argv,
render, `output.emit`".

Also outstanding:

- **§11.4 template zoning** — split `## File Contents` into editable vs read-only, so `-e` vs
  `-r` becomes a contract the patcher can enforce (reject patches to `-r` files).
- **`--json` flag** — trivial on top of `output.emit` now, but no verb needs it yet.
- **The 300s cache TTL is an uncosted guess.** Caches are rented; nobody has priced the
  frontload-and-idle case against just re-sending the prompt.
- **Cache economics are proven only at ~23k tokens**, against a design targeting 400k–1M.
  The 99.9% reuse figure may not survive the jump — larger caches cost more to hold.
- **`openai:` backend has never been live-tested.** Mock checks only.
- **Anthropic `cache_creation_input_tokens`** is counted but the multi-turn economics were
  never measured end-to-end the way Gemini's were.

### Suggested order from here

1. ~~Run the suite and the live checks; fix whatever macOS surfaces.~~ **Done** — see §3.
2. Build `pack` — it is the smallest verb that makes the tool usable headlessly, and it
   validates the output contract against a real consumer.
3. Only then revisit cache economics at realistic size, since `pack` is what will let you
   generate a 400k-token frontload without driving the TUI by hand.

One addition to that list, from the §3 pass: **the implicit-caching number moved between two
machines.** Before the 400k re-measurement is worth anything, decide what `n` a cache figure
needs to be quotable. The explicit arm was stable across every run on both machines; the
implicit arm was not stable across runs on *one* machine.

---

## 7. Committing

All committed and pushed on `claude/kopipasta-agentic-cli-ta26qs`. Line endings did not churn:
the Windows work landed as LF, and the only CRLF file in the tree is `LICENSE`, which predates
this branch. No `.gitattributes` was needed.
