# Field report: kopipasta 0.70.0 against the `ambient` repo

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
