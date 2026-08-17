# Field report: kopipasta 0.70.0 against the `ambient` repo

> **Round 2, 2026-08-17 (`a823e7c`): every bug in section 2 is fixed. Verified by running them,
> not by reading the diff — see section 7 at the bottom, which also records the one new problem
> the 2.6 fix created downstream.** Sections 1-6 are left exactly as first written, because a
> report that quietly edits itself after the fact is worth nothing.

Companion to `AGENT_CLI_FINDINGS.md`. That file is the spike log written by the people building
the tool. This one is written from the other side: an agent handed `kopipasta ask` and
`kopipasta apply` and told to fix a real bug in a repo the tool had never seen, **without
reading the source files into its own context**.

The task succeeded. `npm run check` on `ambient` is green and the fix is on disk. So read the
bug list below as "what it cost to get there", not as "the tool does not work".

| | |
|---|---|
| Date | 2026-08-16 |
| kopipasta | 0.70.0 (pip, user site) |
| Host | Windows, PowerShell 5.1, Python 3.12.6 |
| Backend | `gemini:gemini-3.7-flash` |
| Target repo | `ambient` - Electron 38 + TypeScript, ~19k source lines, vitest under Electron-as-Node |
| Task | `TODO.md` item 19: the demo UI renders a hardcoded `Simulation - no email is sent` badge while real calendar invitations are being sent |
| Outcome | 4 files changed, `npm run check` green: format, typecheck, lint, boundaries 13/13, 605 passed / 3 skipped, 0 unhandled errors, knip clean, size 102/103 files |

---

## 1. What the session actually looked like

| Turn | Command | Result |
|---|---|---|
| triage | `ask -m 'src/**/*.ts' -m 'src/**/*.tsx'` | 109 AST maps + 13 snippets, 26,410 est tok, 5.4s |
| patch 1a | `ask --mode patch` | **failed** - `MAX_TOKENS` at the 8192 default |
| patch 1b | same, `--max-tokens 32000` | 3 patches, 19.4s, `cached: 16358 / 20822` |
| apply | `apply --session ... --dry-run` | 5/5 hunks would apply |
| apply | `apply --verify "npm run typecheck"` | applied, verify exit 0 |
| gate | `npm run check` | **red**: 3 files unformatted, then 2 eslint errors |
| patch 2 | eslint output fed back verbatim | fixed one error, **introduced another** |
| patch 3 | new error fed back | lint clean |
| gate | `npm run check` | **red**: 605 tests pass, 3 unhandled rejections |
| patch 4 | rejection traces fed back | clean |
| control | sabotage patch re-hardcoding the badge | tests went **red**, 2 of 3 |
| gate | `npm run check` | **green** |

Four correction rounds. Every one of them was found by the project's own gate, none by the tool.

---

## 2. Bugs

### 2.1 BLOCKER (Windows): the `--verify` subprocess reader crashes and swallows the output

Every `--verify` command whose output contains a byte outside cp1252 kills the reader thread:

```
Exception in thread Thread-4 (_readerthread):
  File "C:\Python312\Lib\subprocess.py", line 1599, in _readerthread
    buffer.append(fh.read())
  File "C:\Python312\Lib\encodings\cp1252.py", line 23, in decode
UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 324
```

This fires on ordinary tool output - vitest's `U+23AF` rules, eslint's `U+2716`. It happened on
**3 of 5** verify runs. Consequences, in order of severity:

1. When *both* reader threads die, the envelope is `"verify": {"exit": 1, "output": ""}`. A
   failure is reported with **zero diagnostics**. That is the single worst moment to lose the
   output, and it is exactly when it is lost, because failing output is the output most likely
   to contain a box-drawing character.
2. When one thread survives, the captured text is **truncated from the front** - one run's
   `detail` began mid-stream at `> eslint .`, having lost `format:check` and `typecheck`
   entirely. A reader would conclude those stages never ran.
3. The traceback is printed raw to stderr, interleaved with the JSON envelope, so `--json` is
   no longer a single parseable object on stderr-merged capture.

The exit code is read correctly throughout, so the gate itself still functions. Only the
explanation is lost.

**Fix:** `encoding='utf-8', errors='replace'` on the `subprocess` call that feeds
`_readerthread`. There is no case where crashing beats a replacement character.

### 2.2 MAJOR: `--revert-on-fail` declines silently, and the hint says the opposite

On the turn-2 verify failure, the envelope was:

```json
"hint": "The files this run touched have been restored.",
"reverted": [],
"revert_declined": ["src/.../Demo.tsx", "src/.../Demo.test.tsx"]
```

`npm run lint` run immediately afterwards still showed the error, so `revert_declined` was the
truth and `hint` was false.

The decline is **correct behaviour** - the worktree was dirty via `--dirty-ok`, and reverting
would have destroyed an unrelated `prettier --write` I had run by hand. The bug is that `hint`
is an unconditional string. Anyone reading the console rather than parsing the JSON walks away
believing the tree was restored when it was not, and their next action is taken against a state
they have mismodelled.

**Fix:** derive `hint` from `reverted` vs `revert_declined`. When declining, say why:
`"revert declined: 2 file(s) had uncommitted changes before this run; they are untouched and
the patch is still applied."`

### 2.3 MAJOR: output is never prettier-clean

All four patch turns failed `prettier --check`. In a repo whose gate opens with
`prettier --check .`, that is a guaranteed red on turn 1, every time. I worked around it by
wrapping the verify:

```
--verify "npx prettier --write <files> && npm run check"
```

which works, but it means the verify command is mutating the tree, and it is not obvious enough
that a first-time user would arrive at it.

**Suggestion:** a `--format-cmd` hook run after apply and before verify would remove a whole
class of false failure. Failing that, document the wrap.

### 2.4 MINOR: `--max-tokens` default is too low for `--mode patch`

First patch call died at the 8192 default having produced 9,340 characters. One wasted call.
The error message was genuinely good - it named the limit, said reasoning tokens spend the same
budget, and gave two remedies - but patch mode emitting multi-file SEARCH/REPLACE blocks should
not share a default with `answer`.

### 2.5 MINOR: `ask` reports `"patches": N` and exits 0 without applying anything

`"patches": 3` plus `"ok": true` reads as "3 patches applied". Nothing had been written; `git
status` was clean. The separation of proposal from application is a *good* design decision - it
is the best thing about the tool - but the field name undersells it. `"patches_proposed"`, or
an `"applied": false` sibling, would remove the ambiguity.

### 2.6 MINOR: `.gitignore` is edited without asking

First run in the repo appended `.kopipasta/` to a tracked `.gitignore`. It was the only tree
change that run produced. Defensible default, but it should be announced as a change or gated
behind a prompt/flag.

### 2.7 MINOR (Windows): stderr chatter renders as a fatal error

`kopipasta: .gitignore detected.` and the uncommitted-changes notice go to stderr, and
PowerShell 5.1 renders any stderr from a native command as a red `NativeCommandError` block
with a stack trace. Every single invocation looks like it crashed. Consider routing
informational lines to stdout, or suppressing them entirely under `--json`.

### 2.8 MINOR (Windows): `-q` with braces or escaped quotes is destroyed by the shell

```
kopipasta ask ... -q "... <span className=\"d-sim\">Simulation {'\u2014'} no email is sent</span> ..."
-> {"error": "usage", "summary": "unrecognized arguments: {'\\u2014'} no email is sent</span> ..."}
```

PowerShell splits on the brace/quote combination before argparse sees it. `-q @file` is the
working form and should be the **documented** form for Windows, not a footnote. It is also the
better ergonomic default for prompts long enough to be worth writing.

---

## 3. Model-behaviour findings (not tool bugs, but they shape the tool)

### 3.1 Triage over-scoped, at high stated confidence

It marked `shared/model.ts`, `shared/ipc.ts` and `main/ipc/register.ts` as **must edit** at
0.85-0.90 confidence, to "carry the dry-run state across the IPC boundary". That plumbing
already existed: `SystemStatusView.dryRun` at `shared/model.ts:299`, and both `'system:status'`
and `'system:changed'` already carrying a whole `SystemStatusView`. Acting on the triage would
have produced a duplicate channel.

This is **structural to skeleton mode**, not bad luck: an AST map shows that an interface
exists, not which fields it already has. Worth a line in the docs - *triage tells you where to
look, not what is missing.* A 30-second `rg` disproved it.

### 3.2 It laundered a false premise from the prompt into a 0.95-confidence finding

I quoted a stale `TODO.md` claim that `App.tsx:63` contained a second hardcoded banner. Triage
returned `App.tsx` as must-edit at 0.95, "contains the hardcoded banner". The string does not
exist anywhere in the repo; the shell that held it was deleted months ago. **`App.tsx` was in
the payload it was given**, so it had the means to contradict me and instead agreed with me.

Related: `files_cited` listed `TODO.md`, which was never in the selection. Treat `files_cited`
as decorative unless it is computed from the payload rather than asked of the model.

### 3.3 It cannot self-correct without an external gate - and that is the design working

Two of the four turns produced output that was *worse* in a way the previous turn's tool would
not have caught:

- Turn 2 fixed `no-promise-executor-return` by writing `new Promise(() => {})`, which is
  `no-empty-function`. One lint error traded for another.
- Turn 1's tests were **green and broken simultaneously**: 11 passed, 3 unhandled rejections.
  Its `mockImplementation` answered `null` for every channel it did not name, and the component
  under test indexes into three concurrent IPC results, so the `TypeError` landed *after* each
  assertion had already succeeded.

Neither was caught by `--verify "npm run typecheck"`. Both needed the project's real gate. The
practical rule this implies, and it should be in the README:

> **The verify command is the quality ceiling of the tool.** A cheap verify buys a cheap
> answer. `apply --verify` is not a safety net you configure once; it is the entire mechanism.

### 3.4 It complies with sabotage requests, which is correct and worth preserving

For the negative control I asked it to re-introduce the bug deliberately. It did so, minimally,
without moralising and without "fixing" anything adjacent. Two of the three new tests then
failed with a real message. Any future safety tuning should keep this: a model that refuses to
write a failing case cannot be used to prove a test has teeth.

---

## 4. What worked, unqualified

- **Fuzzy hunk matching.** 8 hunks over 4 turns, 100% applied, no `--force`, no corruption -
  including turns 2-4 where the on-disk file had already been rewritten by prettier *and* by an
  earlier kopipasta patch. This is the load-bearing component and it held.
- **`apply --dry-run`.** Per-file hunk counts (`2/2`, `1/1`, `2/2`) before touching anything,
  and it agreed with the real run every time.
- **Editable-set restriction.** Patches confined to the session's `-e` files unless
  `--any-file`. Combined with the "N uncommitted change(s) not touched by this patch, and left
  alone: ..." notice, it was always clear what was in scope.
- **Session continuity and prefix caching.** Turns 2-4 reported `prefix_reused: true` and
  `cached: 20225`. Corrections cost 5-8s each. Pasting raw `tsc` / `eslint` / `vitest` output
  back in verbatim worked with no reformatting.
- **Triage economics.** 26k tokens to orient in a 109-file tree, and it independently found the
  correct reference implementation (`SettingsView.tsx`) - the single most useful line in the
  output.
- **Refusing to emit a truncated patch** on `MAX_TOKENS`, rather than returning something that
  parses and corrupts a file.

---

## 5. Priority

1. **2.1** - utf-8 the verify reader. It silently destroys the diagnostics that the whole
   `--verify` design exists to surface, on the platform this was run on.
2. **2.2** - make `hint` follow `reverted`. A tool that reports a restoration it did not
   perform is the one failure mode that cannot be caught by running it again.
3. **2.3** - a format hook, or documentation of the `prettier --write && gate` wrap.
4. **3.3** - put the "verify is the quality ceiling" rule in the README, above the flag list.
5. **2.4 / 2.5 / 2.8** - defaults and field naming.

---

## 6. Not verified

- The fix compiles, lints, and passes 605 tests. **It has never been seen rendered.** Typecheck
  and vitest say nothing about a UI, and `ambient`'s own AGENTS.md says so explicitly. A
  screenshot with the live flag set is still owed.
- `smoke:mail` and `smoke:schedule` were not re-run.
- Everything above is a single sample on one machine, one repo, one backend
  (`gemini:gemini-3.7-flash`). The Windows encoding bugs (2.1, 2.7, 2.8) should reproduce
  anywhere PowerShell and a cp1252 default encoding meet; the model-behaviour findings in
  section 3 should not be assumed to transfer to another backend.

---

## 7. Round 2 — verification of the fixes (2026-08-17, `a823e7c`, same machine)

Same host, same backend, same repo. `pip show` still reports **0.70.0** and there is no
`--version` flag, so the only way to tell the two builds apart is the git SHA of the editable
install. **Bump the version, or add `--version`** — a user who reports a bug against "0.70.0"
is naming two different programs.

Each fix was checked by reproducing the original failing invocation, not by reading the diff.

| # | Was | Now | How it was checked |
|---|---|---|---|
| 2.1 | `UnicodeDecodeError`, `"output": ""` | **FIXED** | Re-ran the exact sabotage-apply whose verify emits `U+23AF`. Full vitest text captured, `U+23AF` intact, no traceback |
| 2.2 | `hint` claimed a revert that did not happen | **FIXED** | Forced a declined revert. `hint` now reads `Revert declined: 1 file(s) had uncommitted changes before this run (...); they are untouched and the patch is still applied to them.` |
| 2.3 | output never prettier-clean | **FIXED** | `--format-cmd "npx prettier --write {files}"` exists, ran on all 3 applied files before verify, `format.exit: 0` |
| 2.4 | 8192 default killed patch mode | **FIXED** | A 12,212-output-token patch completed; envelope now reports `max_tokens: 65536` |
| 2.5 | `"patches": N` read as "applied" | **FIXED** | `ask` returns `patches_proposed: 3, patches_applied: 0` plus a `next:` command; `apply` returns `3 / 3` |
| 2.6 | `.gitignore` edited unasked | **FIXED** | Reverted the entry, re-ran: `.gitignore` untouched, `.kopipasta/` merely untracked |
| 2.7 | stderr chatter looks fatal in PowerShell | **partially** | Quieter, but `kopipasta: .gitignore detected.` and the uncommitted-changes notice still hit stderr under `--json` and still render as a red `NativeCommandError` |
| 2.8 | `-q` destroyed by the shell | n/a | A shell fact, not fixable upstream. `-q @file` is now the documented form and is what I use unconditionally |

Three additions I did not ask for and would keep: `revert_declined_why` (a per-file reason map),
`detail` mirroring `verify.output` at the top level, and `next` telling you the command to run.

### 7.1 One new problem, created by the 2.6 fix

Not adding `.kopipasta/` to `.gitignore` is the right default, but it moved a cost onto the user
that nothing warns them about. On the next run of `ambient`'s gate:

```
[warn] .kopipasta/sessions/2026-08-16-11f5/001-request.md
... 34 more ...
[warn] Code style issues found in 36 files. Run Prettier with --write to fix.
```

`prettier --check .` now walks the session transcripts and fails **before reaching a single
source file**. The same will happen to any repo-wide linter, formatter or dead-code scanner, and
the failure names kopipasta's files without explaining why they appeared. Worse, `npm run fix`
would have *rewritten the transcripts* — reformatting a verbatim record of what was sent to a
model, which is evidence.

I fixed it in `ambient` by adding `.kopipasta/` to both `.gitignore` and `.prettierignore` as a
deliberate repo decision. The tool should make that decision easy to find: on first run in a repo,
print one line — `kopipasta: writing to .kopipasta/; add it to .gitignore and any formatter's
ignore file` — or offer `kopipasta init`. Silently doing nothing is better than silently editing,
but saying nothing at all is not the same as saying nothing needs doing.

### 7.2 The round-2 task, as evidence the fixes hold under real use

Not a re-run of the old task. A different open item in the same repo: **Graph `/me/events`
bypassed `checkPolicy`**, so calendar invitations — which Microsoft really does email to
attendees — were never checked against the recipient-domain allowlist that every `mail.send` must
pass.

- One `ask --mode patch` over 3 editable + 2 reference files: 15,597 est input, 19.8s,
  **3 patches, 6 hunks, all applied**.
- `apply --format-cmd ... --verify "npm run typecheck && vitest ..." --revert-on-fail`: format
  exit 0, verify exit 0, first try.
- Full `npm run check` green with **no** correction round. Contrast with round 1, which needed
  four. The `--format-cmd` fix alone removed one guaranteed round trip, and the bigger token
  budget removed another.
- Negative control: deleting the six-line gate turns **exactly 3 of 14** tests red and leaves the
  original 11 green.
- `npm run smoke` and `npm run smoke:schedule` also pass.

The one thing the model still got right only because it was told: the gate must see the
**post-override** attendee list, not the original. It is a one-line ordering choice, it is
invisible to typecheck, and getting it backwards would have produced a fix that passes every gate
while still emailing the wrong person. Spelling that out in the prompt is what made this a
one-shot. **The verify command is the quality ceiling; the prompt is the quality floor.**

### 7.3 Still open from round 1

- **2.7**, above.
- **3.1 / 3.2** — nothing has changed about triage over-scoping, or about the model laundering a
  false premise from the prompt into a high-confidence finding. Round 2 confirmed the cost of
  that from the other direction: a subagent checking `ambient`'s own TODO against the code found
  **four of fourteen** items describing work that was already done, and one describing an
  endpoint the repo never calls. Any tool that reads a repo's stated intent — and `--mode triage`
  is exactly that — inherits its staleness. A line in the docs saying triage reports *claims*,
  not *facts*, would be honest.
- `files_cited` is still asked of the model rather than computed from the payload. In round 2 it
  happened to be correct; in round 1 it named a file that was never sent.

---

## 8. Round 3 — dogfooding kopipasta on kopipasta (2026-08-17, same machine)

Different in kind from rounds 1 and 2. This was not a task in another repo; it was `kopipasta ask`
and `kopipasta apply` used to change *kopipasta*, against an editable install, so every patch that
landed was running by the next invocation. Everything below was reproduced by running it.

### 8.1 §7.1 was misdiagnosed, and here is the actual cause

§7.1 opens "Not adding `.kopipasta/` to `.gitignore` is the right default". **The tool never
stopped adding it.** `Session._ensure_dir` still calls `add_to_gitignore` (session.py:198), and
`tests/test_ask.py:1049` pins that it does. What 2.6 fixed was the *announcement*, not the edit.
A fresh repo, one `ask`, and `git status` shows ` M .gitignore`.

Nor does prettier ignore `.gitignore`. Measured with prettier 3:

```
.gitignore contains .kopipasta/   -> prettier skips it        (1 file warned)
.gitignore entry removed          -> prettier walks it        (2 files warned)
```

The real defect is narrower and worse than either: **the ignore entry is written only when a new
session directory is created, and is never repaired.** `_ensure_dir` returns early when the
directory already exists, so:

```
remove '.kopipasta/' from .gitignore, then
  ask (new session)         -> re-added      OK
  ask --session <existing>  -> NOT re-added  <-- §7.1 happened here
```

Delete the line once, continue a session, and the transcripts are exposed to every repo-wide tool
from then on. That is what §7.1 observed, and it is a bug rather than a design decision.

`.git/info/exclude` is not the fix: it hides the directory from `git status` and prettier still
walks it (measured). And `git clean -xfd` reports `Would remove .kopipasta/` — a routine hygiene
command destroys the transcripts *and* `cache.json`, which is a lease on a cache billed per
token-hour.

### 8.2 `--revert-on-fail` cannot restore an untracked file, and left the tree unrunnable

`revert()` has two mechanisms: `os.remove` for files this run *created*, and `git checkout --` for
everything else. A file that is untracked **and** pre-existing falls between them — git cannot
check out a path it does not track — so it keeps its new contents and is recorded as `GIT_REFUSED`.

That is not a corner case; it is every second turn of a session that created a file on an earlier
turn. Observed: turn 3 rewrote the untracked `kopipasta/core/state.py` to import a symbol whose
defining patch the same turn had failed to emit. Verify failed, revert ran, and:

```
ERROR tests/test_state.py
ImportError: cannot import name 'load_toml' from 'kopipasta.core.config'
```

The tree before the run worked. The tree after the patch worked badly. The tree after the *undo*
did not import at all — a state that had never existed. An undo that can produce a state the
caller was never in is worse than no undo. Second-order: outside a git repository every modified
file takes the `git checkout` branch, so `--revert-on-fail` restores nothing it modified.

The fix is to snapshot pre-run bytes rather than delegate to git. That also retires
`WAS_ALREADY_DIRTY` as a decline reason: the decline existed because `git checkout` restores to
HEAD and would have destroyed uncommitted work, whereas a snapshot taken after the caller's edits
and before ours restores that work instead of discarding it.

### 8.3 A complete, correct patch was discarded in silence — now fixed

A `--mode patch` response declared two files. `kopipasta/core/apply.py` carried four well-formed
SEARCH/REPLACE hunks. The envelope said:

```
"patches_proposed": 1
```

and nothing else. Isolated to the byte: the same content standalone gives `{"error":
"no_patches"}`; with one opening fence added it parses. `modes.py:343-345` even warns the model
that "an unfenced block is skipped in silence, however correct it is." The skipping is defensible.
The silence is not — by this repo's own §2.2 standard, reporting one patch when the model sent two
is a report of what was intended rather than what occurred.

Measured causes are more varied than the fence rule suggests:

```
# FILE: a.py + SEARCH/REPLACE at column 0   -> parses
# FILE: a.py + prose, no body               -> dropped
indented # FILE: a.py + SEARCH/REPLACE      -> dropped whole
```

**Fixed in this round.** `patcher.declared_file_paths` / `skipped_file_paths` compare what the
response *declared* against what the parser accepted, and both `ask` and `apply` now report
`patches_skipped` and narrate it. Replaying the original failure now prints
`kopipasta: 6 file(s) the response named are not in this patch: kopipasta/patcher.py, ...`.
The message deliberately states the fact and not a diagnosis, because the cause is not knowable
from the artifact.

### 8.4 The inverse: test fixtures parsed as real patches

Asked to add tests *for the patch format*, the model emitted fixtures containing `# FILE: a.py`
headers. The parser extracted the fixtures as real patches and dropped both actual files:

```
Previewing 3 patch(es)...
Would create b.py
Would create a.py
Would create b.py
```

Confirmed minimal case: a `# FILE:` header inside a string literal inside a fenced block becomes a
patch. The editable-set guard cannot catch it — `apply.py:592-600` deliberately adds every
non-existent path to the zone, because refusing creations would make "add a new module"
impossible. So a hallucinated or misparsed creation is waved through by design, and only
`--dry-run` stands between it and the worktree. `--dry-run` earned its keep twice in this round.

### 8.5 Session state should not live in the worktree

`.git/kopipasta/` removes 8.1 entirely — there is nothing to ignore, so no tracked file is ever
edited — while staying reachable from the repo root by a relative path, which `.git`-relative
paths are: `git rev-parse --git-path kopipasta` returns `.git/kopipasta`. That matters because
many agent sandboxes permit writes only inside the workspace, so a `$HOME` state directory is a
write they would deny.

Greppability does not regress, measured:

```
rg MARKER              -> finds neither .kopipasta/ nor .git/kopipasta/  (both are dot-dirs)
rg --hidden MARKER     -> finds BOTH
```

Landed this round: two-pass project-root discovery (VCS markers anywhere beat manifests anywhere,
so **git repositories resolve exactly as before** and there is nothing to migrate), and
`core/state.py`, which resolves the state root through `--state-dir` > `KOPIPASTA_STATE_DIR` >
`config.toml [state] dir` > a `git -> repo -> xdg -> temp` chain, recording `source` and `kind`
separately so "you asked for xdg" and "it landed in temp" cannot be confused.

Two non-git defects found on the way, both verified: kopipasta writes a `.gitignore` into
directories that contain no git at all, and outside a repository every subdirectory you run from
becomes its own project, so `nogit/.kopipasta` and `nogit/sub/.kopipasta` both exist and
`--continue` from the wrong one silently finds nothing.

### 8.6 What dogfooding was good and bad at

Good: a focused single-concern patch over 2-3 files landed first try, repeatedly, including tests
with real teeth — sabotaging `.exists()` to `.is_dir()` turned 6 of them red. Prefix caching made
correction turns cost 10-40s. Feeding a verbatim pytest traceback back in worked with no editing.

Bad, and consistently so: **large editable sets.** Five files and 62k tokens timed out at 900s
with nothing to show. Three named editable files produced two, twice; on one of those it emitted
the tests and not the implementation, which is the worse half to hold alone. Whole-file rewrites
of a 726-line module matched 2 of 4 hunks. And it dropped a specified test case in silence — the
one case with a real bug behind it — which only reading the diff caught.

The rule this suggests, and it is the round-3 counterpart to "the verify command is the quality
ceiling": **the editable set is the reliability budget.** Two or three files is a patch that
lands; five is a patch that times out or arrives half-written. Splitting is not a workaround, it
is the unit of work.

Also worth stating: two of these round trips were spent asking a model to sort imports, which
`ruff check --fix` did instantly and correctly. Mechanical fixes belong to the mechanical tool.
