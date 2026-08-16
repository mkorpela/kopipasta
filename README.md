# kopipasta

[![Version](https://img.shields.io/pypi/v/kopipasta.svg)](https://pypi.python.org/pypi/kopipasta)
[![Downloads](http://pepy.tech/badge/kopipasta)](http://pepy.tech/project/kopipasta)

**Explicit context control for LLM coding — for you at a terminal, or for an agent
shelling out to it.**

No black boxes, no hidden RAG, no embedding search. You say which files go in the prompt,
and you can see exactly what was sent.

kopipasta has two faces over one engine:

- **`kopipasta`** — the interactive TUI. Pick files, copy a prompt, paste the reply back,
  apply the patches. The original loop, unchanged.
- **`kopipasta ask`** — the headless CLI. Selection comes from argv, the question goes
  straight to a model, and the answer lands on disk. Built to be called by an agent.

---

## Why an agent would call this

An agentic harness has a small, precious context window that degrades as it fills. It reads
files one at a time and pays for each of them for the rest of the session. Two things it
therefore does badly:

- **Whole-repo reasoning.** "Which of these 800 files matter?" needs 800 files in view. An
  agent that reads them all to find out has already poisoned the context it needed them for.
- **Large coordinated changes.** Harnesses are tuned for many small verified edits, not one
  400-line change across seven files with a single design behind it.

`kopipasta ask` is a **context oracle**: a separate process with its own large, disposable
context window, keeping its conversation on disk and handing back *pointers* — a hypothesis,
a ranked file list, a path to the full answer. The caller spends a few hundred tokens on a
question that took 500k tokens of reading to answer, and its own context stays clean.

```bash
kopipasta ask --all -q "Tokens are accepted after expiry. Which files implement
                        validation, and where is the bug likely?" --json
```

```json
{
  "ok": true, "session": "2026-08-16-0e5f", "turn": 1, "mode": "triage",
  "request":  ".kopipasta/sessions/2026-08-16-0e5f/001-request.md",
  "response": ".kopipasta/sessions/2026-08-16-0e5f/001-response.md",
  "usage": {"input": 412330, "cached": 389100, "output": 1840},
  "sent": {"edit": 0, "ref": 0, "map": 380, "demoted": 22},
  "files_cited": ["src/auth/tokens.py", "src/auth/session.py"]
}
```

---

## The three workflows

### 1. Triage — frontload the repo, get back a short answer

```bash
kopipasta ask --all -q "Where does rate limiting happen?" --json
```

Returns a hypothesis, a ranked `relevant_files`, a `suggested_selection`, and
`missing_context` — the last being the honest signal that the model answered without
seeing something it needed.

**Triage tells you where to look, not what is missing.** A skeleton shows that an interface
exists, not which fields it already has, so "must edit, 0.9" over a file whose plumbing is
already in place is the expected failure of the cheap pass, not a malfunction. One `rg`
disproves it in thirty seconds, and that is still the right trade against reading the tree.

### 2. Distill — pivot to the minimal working set

```bash
kopipasta ask --from-file selection.txt -m 'src/**/*.py' \
              -q "Trace the path from validation to refresh." --json
```

The file list from step 1 feeds straight back in, so you go from scanning the whole repo to
a tight, high-signal window without re-reading anything irrelevant.

### 3. Coordinated patch — one call, many files, verified

```bash
kopipasta ask -e 'src/auth/tokens.py' -e 'src/auth/session.py' \
              -r 'tests/test_auth*.py' -m 'src/**/*.py' \
              --mode patch -q @task.md --json

kopipasta apply current --verify 'pytest -q' --revert-on-fail --json
```

One model call with the whole subsystem in view, zoned into editable (`-e`) and read-only
(`-r`), applied deterministically, verified, reported as a diffstat — and rolled back if the
tests fail.

`ask` **proposes**. Nothing is written until `apply` runs, and the envelope says so:
`patches_proposed` and `patches_applied` are separate counts, and `next` is the exact command
that would apply them.

> **`-q @file`, not `-q "…"`, for anything longer than a sentence.** Prompts contain braces,
> quotes and newlines, and every shell mangles a different subset of them — PowerShell splits
> `{'—'}` before the tool ever sees it. Reading the task from a file also makes it reviewable
> and re-runnable.

### The verify command is the quality ceiling

This is the one thing worth internalising before the flag list. `apply --verify` is not a
safety net you configure once — it is the entire mechanism by which a wrong answer is caught.
Nothing downstream of it checks harder than the command you hand it.

A model can produce output that is green and broken at the same time: tests that pass while
three promises reject unhandled, a lint error traded for a different lint error. Neither is
visible to `--verify 'tsc --noEmit'`; both are visible to the project's real gate. **A cheap
verify buys a cheap answer.** Pass the same command CI runs.

`--format-cmd` exists so the gate is not spent on whitespace:

```bash
kopipasta apply current \
  --format-cmd 'npx prettier --write {files}' \
  --verify 'npm run check' --revert-on-fail
```

`{files}` is replaced with the files this run actually wrote, so the formatter cannot wander
into unrelated uncommitted work. It runs after the patch and before the verify, and its
failure is reported without becoming the verdict — exit 7 keeps meaning "`--verify` failed".

---

## Commands

```
kopipasta                            interactive TUI (default)
kopipasta tui                        the same, named explicitly
kopipasta ask     [selectors] -q …   assemble context, ask a model, record the turn
kopipasta apply   [file|-|current]   apply a patch, verify it, report a diffstat
kopipasta map     [paths]            symbol skeleton of the repo. No model, no cost.
kopipasta session {ls|show|diff|rm|reap}
kopipasta config  --show             what resolved, and where each value came from
```

Note what is **absent**: the model and the provider. Those are configuration you set once,
not arguments you repeat. `kopipasta ask -q "…"` is the whole common path.

To assemble a payload without calling anything, use `kopipasta ask --dry-run` — it writes
the request and records the session exactly as a real run would.

## Selecting files

The TUI's four-state model, as flags. Repeatable and order-independent; the most detailed
role wins, so `-m '**/*.py' -e src/api.py` skeletons the tree but sends that one file whole.

| Flag | Rendering | Means |
|---|---|---|
| `-e, --edit` | full content | active workspace — editable, attention goes here |
| `-r, --ref` | full content | reference — read for dependencies, don't change |
| `-m, --map` | AST skeleton | signatures and one docstring line |
| `-s, --snippet` | first 50 lines | a coarse peek |
| `-x, --exclude` | dropped | applied last, wins over everything |

```
--all                 every non-ignored file, as a skeleton
--changed             git working-tree changes, including untracked
--changed-since REF   git diff --name-only REF...HEAD
--from-file PATH      newline-delimited paths — feeds a triage answer back in
```

Globs, directories and literal paths all work; `@file` reads patterns from a file anywhere a
pattern is expected — including `-q @task.md`, which is the recommended form for any prompt
long enough that a shell could mangle it. `.gitignore` and binary filtering always apply.

**A selection that matches nothing is an error, not a warning** — a typo'd glob would
otherwise produce a confident answer built from nothing:

```
kopipasta: no files matched.
  -e kopipasta/pacher.py               0 files   (did you mean kopipasta/patcher.py?)
```

## Budget

Whole repos exceed even a 1M-token window, so files **demote** rather than vanish:

```
full content  →  AST skeleton  →  path-only (still in the structure tree)
```

`--budget 400k` sets the target. Files named by `-e` are never demoted; everything demoted
is reported, because silent truncation is what makes an answer confidently wrong.
`--strict-budget` exits 6 instead of demoting.

## Configuration

Which model answers is an operator decision, made once — not an argument at every call site.

```
--backend gemini:gemini-3.7-flash    escape hatch: debugging, A/B, pinning a CI job
KOPIPASTA_BACKEND                    per-shell or per-job override
~/.config/kopipasta/config.toml      the normal place
```

```toml
[ask]
provider = "gemini"
model    = "gemini-3.7-flash"
thinking = "high"          # gemini thinkingLevel; "default" sends nothing

[patch]
provider   = "anthropic"
model      = "claude-opus-5"
max_tokens = 32000         # only needed where the provider caps output
```

Sections are named after the `--mode`: triage over a 400k frontload wants a big cheap context
window, a coordinated patch wants the strongest model. A mode with no section inherits
`[ask]`. Edit with `kopipasta --edit-config`.

Two defaults worth knowing, because both are about the same budget:

- **`max_tokens` is 65536**, what Google's console uses for this model — not the 8192 that
  used to kill the first patch call *after* the whole payload had been sent and billed. A cap
  is not a bill; providers charge for tokens produced, not permitted. Anthropic and OpenAI
  reject anything above their model's ceiling, so set `max_tokens` in that provider's section.
- **`thinking` is `"high"`.** Reasoning tokens are billed as output and spend the same
  allowance. By the time the model starts thinking, the expensive part of the call is already
  paid for, so cheap thinking over a large payload is the worst point on that curve.

**API keys live in the environment, never in that file** — `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`. Backends: `gemini:`, `anthropic:`, `openai:` (and any
OpenAI-compatible server), plus `exec:<command>` and `claude-cli:` which borrow whatever CLI
you are already authenticated as.

`kopipasta config --show` prints what resolved and where each value came from — never the
key itself, only whether it is set:

```
provider     anthropic          config.toml [patch]
model        claude-opus-5      config.toml [patch]
api key      ANTHROPIC_API_KEY  set (108 chars)
max_tokens   32000              config.toml [patch]
thinking     high               built-in default
timeout_s    900                built-in default
```

## Sessions

Every `ask` writes a turn to `.kopipasta/sessions/<id>/` as plain files — the request, the
response, and the record of exactly what was sent. Nothing is hidden, and `rm -rf .kopipasta`
is a complete reset.

```bash
kopipasta session ls                    # newest last; * marks what `apply current` uses
kopipasta session show <id>             # turns, usage, artifact paths
kopipasta session diff <id>             # what changed on disk since the context was built
kopipasta session reap                  # hand back provider caches no longer in use
```

`--session <id>` or `--continue` keeps a conversation going; files already sent and unchanged
are not resent.

`session reap` matters with Gemini: its caches are **rented**, billed per token-hour until
their TTL expires, so an abandoned one costs money quietly.

## For scripts and agents

`--json` on every verb makes stdout a single JSON object. Otherwise stdout is the artifact
and **everything else goes to stderr**, so `kopipasta > prompt.txt` gives you the prompt and
nothing else.

| Exit | Meaning | Retry? |
|---|---|---|
| 0 | success | — |
| 1 | usage or configuration error | no |
| 2 | no usable backend — no key, no command | no |
| 3 | backend error or timeout | usually |
| 4 | patch **partially** applied — worktree dirty | no |
| 5 | patch fully failed — worktree untouched | maybe |
| 6 | budget exceeded under `--strict-budget` | no |
| 7 | `--verify` command failed | no |
| 8 | needed a human, none attached | no |

Exit 3 covers both a rate limit and a model name that will never exist, so `--json` carries
an explicit `retryable` field rather than making you infer it from the code.

Errors name the exact thing to change — the env var, the config key, the flag — and quote the
provider verbatim rather than paraphrasing:

```
kopipasta: no API key for provider 'gemini'.
  Resolved from ~/.config/kopipasta/config.toml [ask]; GEMINI_API_KEY is unset.

  export GEMINI_API_KEY=...
  Or switch provider:  kopipasta --edit-config
  See what resolved:   kopipasta config --show
```

Nothing ever blocks waiting for a human that isn't there. With no terminal attached,
kopipasta either applies the safe default and says so on stderr, or exits 8 naming the flag
that would have answered the question. Set `KOPIPASTA_NONINTERACTIVE=1` to make that explicit
in scripts.

Two fields exist because their absence was read as the opposite of the truth:

- `patches_proposed` / `patches_applied` — never a bare `patches`, which reads as "applied"
  when `ask` has written nothing at all.
- `hint`, on a failed `--verify`, is derived from what the revert actually did. When a revert
  is declined — the file had uncommitted changes before the run, so it is the caller's, not
  ours — the hint says so and names the reason in `revert_declined_why`. A tool that reports a
  restoration it did not perform is the one failure mode that running it again cannot catch.

---

## The interactive TUI

Run `kopipasta` with no subcommand. Same engine, human at the wheel.

```text
➜  ~ kopipasta

  📁 Project Files
  |-- 📂 src/
  |   |-- ● 📄 main.py (4.2 KB)
  |   |-- ◐ 📄 utils.py (1.5 KB)
  |   |-- ○ 📄 server.py
  |-- ● 📄 AI_SESSION.md (0.8 KB)

  [j/k]: Nav  [Space]: Toggle  [m]: Map  [p]: Patch  [e]: Extend  [q]: Copy & Quit
  Context: 2 files | ~5,000 chars | ~1,400 tokens
```

1. **Context** — run `kopipasta`, select files, define the task.
2. **Generate** — paste the prompt into your LLM.
3. **Patch** — press `p`, paste the reply; patches get applied, or paths get imported.
4. **Iterate** — `git diff`, then round again.

### The four states

| State | Colour | Meaning |
|---|---|---|
| Unselected | dim | not in the prompt |
| **Base** | cyan | already sent in a previous turn |
| **Delta** | green | active focus — new, changed, or just patched |
| **Map** | yellow | skeleton only: signatures, no bodies |

`Space` toggles Unselected ↔ Delta. `m` toggles Unselected ↔ Map. `e` copies only the Delta
files and promotes them to Base. `p` promotes whatever it patched to Delta.

### Controls

| Key | Action |
|---|---|
| `↑/↓` `k/j` | navigate |
| `←/→` `h/l` | collapse / expand |
| `/` or `f` | fuzzy search |
| `Space` | toggle selection |
| `m` | map (skeleton) |
| `s` | snippet mode — first 50 lines, marked `◐` |
| `a` | add all in directory |
| `p` | process: apply patches, or import paths from pasted text |
| `e` | extend: copy Delta only, promote to Base |
| `c` | clear / reset menu |
| `r` | Ralph: configure the MCP server for Claude Desktop |
| `n` `u` `d` | session start / update / done |
| `q` | finalize, copy, and exit |

### Universal intake (`p`)

Paste *any* text from your LLM. Code blocks — full files, unified diffs, search/replace —
get applied. If there is no code, kopipasta scans for valid project paths instead, so you can
paste a traceback or a list of suggested files and choose to **[A]ppend** or **[R]eplace**
your selection.

### Project memory

- **`AI_CONTEXT.md`** — the laws of physics for your project. Always pinned to the prompt if
  it exists, in the TUI *and* in `ask`. The cheapest way to give a disposable oracle your
  non-obvious rules. `--no-project-context` opts out.
- **`AI_SESSION.md`** — a scratchpad for a long task, started with `n`, compressed with `u`,
  harvested into `AI_CONTEXT.md` with `d`.
- **`~/.config/kopipasta/ai_profile.md`** — your global preferences, injected into every
  prompt. Edit with `kopipasta --edit-profile`.

### Ralph loop (`r`) — MCP agents

> "If success is just a passing shell script, why are you doing the work?" — **mkorpela**

Press `r` to expose the current selection to an MCP-capable agent: **Delta files are
writable, the whole project is readable**, and a verification command you supply becomes the
agent's definition of done. kopipasta writes the config and registers the server with Claude
Desktop for you.

---

## Installation

```bash
uv tool install kopipasta      # recommended
```

Python 3.10+.

## Developing

```bash
uv sync --group dev
uv run pre-commit install --install-hooks     # installs pre-commit AND pre-push
```

The hooks run `ruff format`, `ruff check --fix` and `mypy` on commit, and the test
suite on push. The suite takes about a minute, which is a tax nobody pays on every
commit — they pay it by typing `--no-verify`, which switches off the fast checks at
the same time. Slow gates belong at the slow boundary.

Every tool runs through `uv run --frozen`, so the version is the one in `uv.lock` and
nothing else. That is not fussiness: this code passes `ruff check` clean on 0.15.1 and
reports ~670 findings on 0.16.3, because ruff's default rule selection moves between
minor releases. A hook and a CI job that disagree about what "lint" means is worse
than having neither — whichever one is red gets treated as the broken one.

CI runs the same three commands, plus the suite on Linux, **Windows** and macOS across
3.10 and 3.12. Windows is not thoroughness for its own sake: every blocker in
`docs/FIELD_REPORT_AMBIENT.md` was invisible on Linux, because cp1252 is the default
encoding there and nowhere else.

A fourth job installs the built package with no dev dependencies and runs it. It exists
because `tomli` was missing from the runtime dependencies for every 3.10 install and
nothing caught it for a long time: mypy depends on `tomli` below 3.11, so every
development environment had it by accident and the only configuration that mattered was
the one nobody ever ran.

## Safety

- Respects `.gitignore`; skips binaries.
- Detects secrets from `.env` in your files and offers to mask them — headless, it masks by
  default and says so, because leaking a secret to an API is worse than a broken value.
- Guards against "snippet hallucination", where a model returns a fragment as if it were a
  whole file, and would otherwise truncate it.
- `apply` refuses a dirty worktree by default, so `git checkout .` is always a complete undo.
  Deletes need `--allow-delete`. Patches are restricted to the session's editable set unless
  you pass `--any-file`.

## Design docs

- [`docs/AGENT_CLI_SPEC.md`](docs/AGENT_CLI_SPEC.md) — the design, written as though finished.
- [`docs/AGENT_CLI_FINDINGS.md`](docs/AGENT_CLI_FINDINGS.md) — what was actually measured,
  what broke, and what is still assumption.
- [`docs/HANDOFF.md`](docs/HANDOFF.md) — current state and how to verify it.
- [`docs/SANDBOX_BACKEND.md`](docs/SANDBOX_BACKEND.md) — running the oracle inside a
  hosted agent, where `claude -p` is the only model access there is.
