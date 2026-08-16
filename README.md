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
              --mode patch -q "$(cat task.md)" --json

kopipasta apply current --verify 'pytest -q' --revert-on-fail --json
```

One model call with the whole subsystem in view, zoned into editable (`-e`) and read-only
(`-r`), applied deterministically, verified, reported as a diffstat — and rolled back if the
tests fail.

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
pattern is expected. `.gitignore` and binary filtering always apply.

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

[patch]
provider = "anthropic"
model    = "claude-opus-5"
```

Sections are per verb: triage over a 400k frontload wants a big cheap context window, a
coordinated patch wants the strongest model. Edit with `kopipasta --edit-config`.

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
max_tokens   4096               config.toml [ask]
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
uv sync                        # for development
```

Python 3.10+.

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
