# The sandbox backend: `claude-cli:` as the primary path

Measured inside Claude Code on the web (Linux sandbox, `remote_mobile` entrypoint), where
`kopipasta ask` was run for real against `claude -p`. Every number below came from that
environment; nothing here is inferred.

---

## 1. Why this is not a fallback

The design targets an agent that shells out to kopipasta for a second, larger context window.
Run that agent inside Claude Code and look at what it actually has:

- **No provider API key.** `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` are all
  unset, and there is no way to set them that does not mean asking a human to paste a secret
  into a sandbox.
- **`claude` on PATH, already authenticated.** Its credential arrives on a file descriptor
  the harness owns; nothing in the environment exposes it as a key.

So in the environment the design is aimed at, `claude-cli:` is not the cheap option or the
zero-custody option. **It is the only option.** Every other backend exits 2.

That inverts the ordering in the spec, which treats raw APIs as the real path and CLI backends
as the convenient one. For a hosted agent it is the other way round, and the hosted agent is
the primary user.

---

## 2. What it costs — measured

### The floor is ~34k tokens per call and you cannot lower it

| Variant | total input | notes |
|---|---|---|
| plain, deny-list tools off | **34,389** | the baseline |
| `--allowedTools ""` (allow-list) | 38,722 | *higher*, not lower |
| cwd = repo root (CLAUDE.md, skills, MCP) | 34,382 | — |
| cwd = empty directory | 34,376 | **no material difference** |

Two hypotheses died here. Running the child from a bare directory to dodge the project's
`CLAUDE.md`, skills and MCP config saves nothing — the floor is the system prompt, not the
project context. And switching the deny-list for an allow-list makes it slightly worse.

There is no flag that gets a `claude -p` call below roughly 34k tokens of input. Plan around
it rather than trying to remove it.

### `--json-schema` doubles the bill

| | total input | cost |
|---|---|---|
| plain | 34,389 | $0.0110 warm / $0.0401 cold |
| `--json-schema` | **69,064** | **$0.2203** |

Almost exactly 2×. The schema is enforced by a second model pass that re-sends the entire
harness prefix. Two models appear in `modelUsage` on every call (`claude-haiku-4-5` plus the
chosen model), consistent with a structuring pass on top of the answer.

**This matters because `triage` is the default mode**, and triage is the mode that wants the
schema. Under `claude-cli:` the default path is the expensive one, and nothing currently says
so.

### Context window, by model

| model | context | 
|---|---|
| haiku | 200,000 |
| **sonnet** | **1,000,000** |
| opus | 1,000,000 |

The frontload use case — the whole reason the oracle exists — works here with no API key at
all. That is the headline.

*(Cost per model is not comparable from single samples: cache state dominates. A warm sonnet
call measured $0.0104 while a cold haiku call measured $0.0508. Do not read a model price
ranking out of that.)*

### Latency

22–26s per `ask`, against roughly 4s for a raw API call carrying the same payload. Harness
startup, not generation. Multi-turn deadline arithmetic should assume ~25s per turn.

---

## 3. Proposal

### 3.1 Detect the sandbox and default to it

Today, an agent inside Claude Code that runs `kopipasta ask` gets **exit 2, no backend
configured** — in an environment where a perfectly good backend is sitting on PATH. That is
the whole tool failing closed at the exact moment it should work.

When *all* of these hold, default to `claude-cli:`:

- nothing in `--backend`, `KOPIPASTA_BACKEND`, or the config file
- no provider API key in the environment
- `claude` resolvable on PATH

**Announce it on stderr.** A default that silently spends money is the wrong kind of magic,
and the floor above means even a trivial question is not free:

```
kopipasta: no backend configured; using claude-cli (claude is on PATH, no API key present).
           ~34k tokens of harness overhead per call. Set one explicitly with --edit-config.
```

This is the single change that makes kopipasta work out of the box in a hosted agent.

### 3.2 Make the schema cost a choice, not a surprise

Add `--no-enforce-schema` (and consider making it the default for `claude-cli:` specifically).
Without it, triage asks for JSON in the prompt and parses the reply, exactly as `exec:` does:
half the cost, no server-side guarantee.

The trade is real in both directions, so it should be stated rather than decided silently:
enforcement costs 2× here, and unenforced JSON is what the `exec:` path has always done.

### 3.3 Budget accounting must include the floor

A measured run reported `est_input_tokens: 4796` while 68,722 were billed — **14× under**.
The ladder is calibrated on our payload alone, so `--budget` currently means "our share of the
input", not "the input".

Let a backend declare its fixed overhead, and have the budget ladder and `--dry-run` add it:

```
backend.overhead_tokens -> 34_000 for claude-cli, 0 for raw APIs
```

Then `--budget 400k` against a 1M window leaves the right headroom, and a caller can see the
real number before spending it.

### 3.4 Make the recursion guard deliberate

`ClaudeCliBackend.TOOLS_OFF` is unconditional, which today prevents kopipasta → `claude -p` →
Bash → kopipasta. That guard is currently **accidental**: it exists to force completion shape,
and the recursion safety is a side effect.

Two reasons not to leave it that way:

- `--disallowedTools` is a **deny-list**, and deny-lists are incomplete by construction. With
  the current list the child still reported `Agent` and `Artifact` available. `--allowedTools ""`
  does not close it either — both were still there.
- Anything that relaxes the flag for a good reason silently removes the recursion guard too.

So carry an explicit depth marker in the child's environment and refuse to spawn past depth 1:

```
KOPIPASTA_ORACLE_DEPTH=1
```

Cheap, independent of whatever the tool flags do, and it fails with a clear message instead of
forking. Note the harness has its own `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` (observed: `1`),
which governs its subagents — not a process we spawn ourselves.

### 3.5 Choose sonnet explicitly

`claude-cli:-` inherits whatever the CLI defaults to. For an oracle the deciding property is
the context window, and only sonnet and opus report 1M. Default `claude-cli:` to sonnet rather
than leaving it implicit, so a 400k frontload does not fail against a 200k model.

### 3.6 Timeouts

Defaults are fine (900s per call), but `--deadline` guidance should assume ~25s per turn on
this backend rather than the ~4s a raw API gives.

---

## 4. Verifying this environment

The measurements above are reproducible only where `claude` is authenticated and no API key
exists — a hosted Claude Code session. They belong in a **live test marked to skip elsewhere**,
not in the normal suite:

```sh
# skipped unless `claude` is on PATH and no provider key is set
uv run pytest -q -m sandbox
```

Worth asserting, because each is a number that moved once already:

- a plain call stays near the ~34k floor (alert if it drifts far, since it is not ours to control)
- `--json-schema` still costs ~2× a plain call
- `ask --backend claude-cli:` returns a parseable triage envelope
- the child cannot reach Bash, so the recursion guard holds

The floor grew from ~29k to ~34k between two measurements days apart. It is set by a system
prompt we do not own and it will keep moving, so any budget arithmetic that hardcodes it needs
a way to notice when it is wrong.
