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

### What `claude -p` actually sends

Pointing `ANTHROPIC_BASE_URL` at a local capture server answers this exactly. One `claude -p
"say OK"`:

```
POST /v1/messages?beta=true            153,164 bytes
Authorization: Bearer <115 chars>      ← attached BY THE CLI, not by the sandbox
anthropic-beta: claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,…
User-Agent: claude-cli/2.1.233 (external, remote_mobile)

model=claude-sonnet-5  max_tokens=64000  stream=true
system:   3 blocks, 15,464 B   (2 of them cache_control ephemeral ttl=1h)
tools:   38 tools, 129,804 B   ← 85% of the request
messages:            9,342 B
```

**The credential comes from the CLI, not from the sandbox border.** That kills the idea of
having kopipasta POST directly and letting the boundary attach auth: `*.anthropic.com` is in
the proxy's `noProxy` list, so those requests never traverse the egress proxy at all, and a raw
`curl` to `/v1/messages` returns `x-api-key header is required` — nothing added anything. The
token lives on a file descriptor the harness owns and the CLI reads.

### The floor is 85% tool schemas — and it *is* reducible

| Variant | tools | tool bytes | body | measured input |
|---|---|---|---|---|
| baseline, no flags | 38 | 129,804 | 153,160 | — |
| `--allowedTools ""` | 38 | 129,804 | 153,160 | **no-op** |
| `--strict-mcp-config` | 38 | 129,804 | 153,160 | no change |
| deny 4 names | 34 | ~114,000 | — | 34,382 |
| deny 11 names (kopipasta today) | 30 | 98,014 | 118,268 | ~34k |
| **deny all 38 names** | **2** | **4,739** | **20,625** | **7,070** |

**Denying every tool cuts the floor 4.9×, from ~34,382 to 7,070 tokens.** The system prompt is
only ~15 KB of the request; the rest is JSON schemas for tools an oracle never calls.

Two corrections to earlier claims in this document:

- **`--disallowedTools` does remove the definitions from the request**, not merely deny their
  use. An earlier version said otherwise, because it compared the deny-list against the
  allow-list rather than against a no-flag baseline.
- **`--allowedTools ""` is a no-op** — all 38 tools are still sent. It is strictly worse than
  the deny-list, and the slightly higher token count first attributed to it was cache-state
  noise.

Still true: cwd makes no difference (34,382 from the repo root vs 34,376 from an empty
directory), so dodging `CLAUDE.md`, skills and MCP config saves nothing.

Six names carry 56% of the tool payload — `Workflow` (21,870 B), `Artifact` (13,448 B), `Bash`
(11,991 B), `DesignSync` (9,324 B), `Agent` (8,616 B), `Monitor` (7,659 B) — so even partial
coverage pays proportionally.

**The tool list is environment-specific.** `Workflow`, `Artifact`, `DesignSync`,
`ShareOnboardingGuide` and friends are hosted-surface tools; a laptop CLI ships far fewer. So
~34k is a `remote_mobile` figure, not a universal constant — another reason to measure it at
runtime rather than hardcode it.

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

### 3.5 Deny every tool, not eleven

`ClaudeCliBackend.TOOLS_OFF` names 11 tools, of which 7 exist in this environment. Extending it
to the full set takes the floor from ~34,382 to **7,070 tokens per call** — the single largest
saving available on this backend, for a one-string change.

The list cannot be complete for all time: it is environment- and version-specific, and a tool
added tomorrow ships enabled. That is acceptable because the failure is graceful — an unknown
tool costs its own schema and nothing else — but it argues for ordering the list by payload
size, so the six names that carry 56% of the bytes are never the ones that drift.

It also makes the recursion guard in §3.4 more urgent rather than less: the more the deny-list
is treated as a cost lever, the more likely someone trims it for a reason unrelated to safety.

### 3.6 Choose sonnet explicitly

`claude-cli:-` inherits whatever the CLI defaults to. For an oracle the deciding property is
the context window, and only sonnet and opus report 1M. Default `claude-cli:` to sonnet rather
than leaving it implicit, so a 400k frontload does not fail against a 200k model.

### 3.7 Timeouts

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

- a fully-denied call stays near the ~7k floor, and a bare one near ~34k (alert on drift:
  neither is ours to control)
- `--json-schema` still costs ~2× a plain call
- `ask --backend claude-cli:` returns a parseable triage envelope
- the child cannot reach Bash, so the recursion guard holds

The floor grew from ~29k to ~34k between two measurements days apart. It is set by a system
prompt we do not own and it will keep moving, so any budget arithmetic that hardcodes it needs
a way to notice when it is wrong.
