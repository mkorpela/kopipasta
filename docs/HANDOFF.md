# Handoff memo — agent-CLI branch, moving from Windows to macOS

**Branch:** `claude/kopipasta-agentic-cli-ta26qs`
**Base commit:** `5aa8df8` ("update gemini-3.7-flash")
**State:** everything is **staged but not committed** (`git diff --cached --stat` → 22 files,
+2754/−182). Nothing has been pushed.
**Green on Windows:** 220 pytest, 54 backend mock checks, `ruff` clean.
**Not yet run anywhere else.** See "What macOS will exercise for the first time" below — that
is the first thing to do on the macstudio, before writing any new code.

---

## 1. What this branch is for

Two threads, both aimed at the same goal — making kopipasta usable *by an agent* rather than
only by a human at a TUI:

1. **Never stall a caller who cannot answer.** A blocking prompt with nobody on the other end
   is an unbounded hang inside someone else's subprocess, and a harness cannot tell a hang from
   slow work. Spec §11.1b / §11.2.
2. **Make the Gemini backend's prompt caching real**, because multi-turn against a large
   frontloaded context is the whole economic case for the oracle. Spec §7.

Design docs: `docs/AGENT_CLI_SPEC.md` (what we intend) and `docs/AGENT_CLI_FINDINGS.md`
(what we measured, plus 26 numbered traps). The findings doc is the more useful of the two —
it is the record of things that were not true when assumed.

---

## 2. What is done

### Phase 0 (spec §12) is complete

| Item | Where | Notes |
| --- | --- | --- |
| Injectable interaction policy | `kopipasta/interaction.py` | `human_attached()`, `require_human()` (exit 8), `use_default_without_human()` |
| Exit codes | `kopipasta/main.py` | 1 = usage, 8 = no human. `UsageError` never opens the TUI |
| Per-project cache | `kopipasta/cache.py` | spec §11.3 |
| Output contract | `kopipasta/output.py` | spec §11.2b — stdout is the artifact, everything else stderr |

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

## 3. What macOS will exercise for the first time

**Do this before anything else.** Several code paths were written on Windows against
documentation rather than a running machine.

1. **`cache._norm_for_key` darwin branch** (`cache.py:52-55`). `os.path.normcase` is a **no-op
   on POSIX**, so macOS — whose default filesystem is case-*in*sensitive — needs an explicit
   `.lower()`. Linux must **not** fold, or two genuinely distinct directories collide. The test
   `test_case_differences_do_not_split_one_repo_into_two_caches` was Windows-only and now runs
   on darwin too; **it has never actually executed on macOS.**
2. **`/private/var` symlinking.** macOS `tmp_path` lives under `/var/folders/...`, which is a
   symlink to `/private/var/...`. `find_project_root()` and `get_project_key()` both call
   `.resolve()`; if any caller does not, keys will diverge for one directory. Watch for this in
   the cache tests specifically.
3. **The editor guard on darwin.** `prompt.py` / `config.py` take the `subprocess.call(("open",
   path))` branch, not `os.startfile`. The `require_human()` check sits above the branch, so it
   should hold — but the tests monkeypatch `os.startfile` with `raising=False` and on macOS that
   patches nothing. Confirm `--edit-template` and `--edit-profile` still exit 8 headlessly.
4. **`os.replace` retry** (`cache.py:105-118`). The `PermissionError` retry exists for Windows,
   where a rename fails while another process holds the destination. On macOS it is dead code
   that should simply never trigger. Harmless, but do not "simplify" it away — the Windows
   failure was real and reproducible.
5. **Encoding.** All the cp1252 defensiveness (`encoding="utf-8"` on every write,
   `errors="replace"` on narration, `chcp 65001`) is Windows-motivated and inert on macOS.
   Do not remove it; the project is developed on both.
6. **Terminal behaviour.** `human_attached()` now checks stdin *and the stream it narrates to*.
   Under the redirect that is stderr, so `kopipasta > prompt.txt` at a terminal keeps the TUI.
   Worth confirming by hand in a real zsh session — it is the one change with no automated
   coverage of the genuinely-interactive case.

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
uv run pytest -q                              # 220 on Windows; expect 220 ± the skipped case test
uv run ruff check kopipasta spike tests
uv run python spike/check_backends.py         # 54 checks, no API key needed, spins a local HTTP mock
```

Live checks (needs `GEMINI_API_KEY`, costs a few cents):

```sh
uv run python -u spike/livecheck.py gemini gemini-implicit   # expect ~99.9% vs ~0.0%
uv run python -c "import sys; sys.path.insert(0,'spike'); import backends; print(backends.GeminiBackend.reap_orphans())"
```

That last one must print `0`. A non-zero number means cached content was left rented on
Google's side — the caches are billed per token-hour until their TTL expires, so orphans cost
money silently.

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

### Suggested order on the macstudio

1. Run the suite and the live checks; fix whatever macOS surfaces (§3 above).
2. Build `pack` — it is the smallest verb that makes the tool usable headlessly, and it
   validates the output contract against a real consumer.
3. Only then revisit cache economics at realistic size, since `pack` is what will let you
   generate a 400k-token frontload without driving the TUI by hand.

---

## 7. Committing

Nothing is committed. The staged set is coherent but large; it may be worth splitting along
the seams it already has:

1. `interaction.py` + headless guards + `tests/test_headless_prompts.py`,
   `tests/test_dispatch.py`, `tests/test_patcher_headless.py`
2. `cache.py` + `tests/test_cache_isolation.py` + `tests/conftest.py`
3. `output.py` + `tests/test_output_contract.py`
4. `spike/` (Gemini caching, oracle fixes)
5. `docs/`

Note `git` reports CRLF→LF warnings for the new files; they were written on Windows. Check
`.gitattributes` before committing on macOS so the line endings do not churn.
