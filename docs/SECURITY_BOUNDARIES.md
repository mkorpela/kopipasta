# Security boundaries

Lines this project will not cross, and *why* — recorded so the next person who has the idea
finds the reasoning instead of re-deriving the temptation. Each entry is a real thing that was
investigated and deliberately not built.

---

## 1. The Claude Code OAuth token is not ours to replay

**Status: rejected. Do not implement.**

### Where it came from

While measuring the `claude-cli:` backend (see `SANDBOX_BACKEND.md`), we wanted to know what
`claude -p` actually puts on the wire, so we pointed its endpoint at a local capture server:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:<port>   claude -p "say OK"
```

The capture server printed the request shape and returned a 400, so nothing was ever sent to a
real endpoint. This is a legitimate diagnostic — it is how we learned the request is 85% tool
schemas, which produced the sanctioned 4.9× saving in `SANDBOX_BACKEND.md`. **The capture script
redacted the `Authorization` header by default** — it printed `<Bearer redacted, 115 chars>`,
never the value. That default is the whole point of this entry: the credential passed through
the diagnostic, and the diagnostic was written not to keep it.

### What was observed

The request carried, among normal headers:

```
Authorization: Bearer <~115 chars>
anthropic-beta: claude-code-20250219,oauth-2025-04-20,...
x-app: cli
User-Agent: claude-cli/2.1.233 (external, remote_mobile)
```

That is an **OAuth token issued to the Claude Code client**, not an `x-api-key`. The
`oauth-2025-04-20` beta, the `x-app: cli`, and the `cc_entrypoint` billing metadata all mark it
as the credential for the Claude Code entitlement, attached **by the CLI** — the sandbox border
adds nothing. (`*.anthropic.com` is in the proxy's `noProxy` list, so these calls never traverse
the egress proxy, and a bare `curl` to `/v1/messages` returns `x-api-key header is required`:
proof that nothing in the environment injects auth.)

### The idea, stated plainly

Capture that Bearer token from the wire (or read it off the file descriptor the harness owns),
then use it in kopipasta's own raw `POST /v1/messages`. That would skip the CLI's ~34k-token
harness prefix entirely and get raw-API economics — cache control, ~4s latency — with no API
key present in the sandbox.

It would technically work. The token authenticates. **We are not building it.**

### Why not

1. **The token is scoped to a client.** An OAuth token is granted *to the CLI* for use
   *through the CLI*. Lifting it into requests we construct ourselves uses a credential outside
   the client it was issued to — the definition of token misuse, even when the account is your
   own.
2. **The sandbox keeps it off-limits on purpose.** It lives on a file descriptor the harness
   owns rather than an environment variable, and `anthropic.com` is routed around the egress
   proxy deliberately. Harvesting it to make off-proxy authenticated calls routes around a
   boundary that was built to stand there — the same class of action as unsetting `HTTPS_PROXY`
   or disabling TLS verification, both of which this environment forbids outright.
3. **It would ship into every user's sandbox.** This is not a one-off probe on our own machine;
   it is a mechanism baked into a distributed tool that would extract and replay *each user's*
   subscription credential on every run. "kopipasta exfiltrates your Claude Code token" is not a
   property any tool should have, whatever the intent behind it.
4. **It circumvents the billing model.** Claude Code auth is a subscription entitlement; raw
   `/v1/messages` access is metered API billing. Using the former to obtain the latter is using
   the service in a way its terms do not permit, and would put both the tool and its users
   offside.

Reasons 1–2 would hold even for a private, personal, one-machine script. Reasons 3–4 are why it
is categorically out for a tool meant to be published.

### What we do instead

The engineering goal — a cheap oracle inside a hosted sandbox — does not require any of this.
Two sanctioned paths reach it:

- **Shrink the CLI floor honestly.** Deny all of the CLI's tools (`SANDBOX_BACKEND.md` §3.5):
  the floor drops from 34,382 to ~7,070 tokens, because 85% of the request is JSON schemas for
  tools an oracle never calls. No boundary crossing, ships to everyone. The remaining ~15 KB
  system prompt is the CLI's, and it is the honest floor of the `claude-cli:` path — we do not
  get under it, and that is fine.
- **Bring an API key for raw economics.** `export ANTHROPIC_API_KEY=...` lights up the
  `anthropic:` backend, which already has no harness prefix, explicit cache control, and ~4s
  latency (findings §2.7). The API key **is** the sanctioned way to get raw access; the boundary
  we declined to cross is exactly the one that says "raw access is gated on a key with its own
  billing."

So the sandbox offers a real choice: the CLI (subscription auth, a floor we can cut to ~7k) or
your own API key (raw, no floor). The token-replay shortcut between the two is the single move
that is off the table.

### The rule

- Never read, capture, persist, or transmit the Claude Code OAuth token, whether from the wire,
  from `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`, or from any credentials file.
- Any diagnostic that inspects an outgoing request must redact the `Authorization` header before
  printing or storing it, and must not send the request onward.
- kopipasta authenticates a backend only from an API key the operator placed in the environment
  (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`), or by invoking the `claude` CLI and
  letting it use its own credential. It never sources auth by any other route.

---

## Adding to this file

An entry belongs here when the project deliberately declined a capability that would otherwise
have been useful, for a safety, privacy, or trust reason. Record what was investigated, what was
observed, the honest statement of the tempting version, why it was rejected, and the sanctioned
alternative. The value is in preserving the *reasoning* — a bare "don't do X" invites someone to
re-litigate it from scratch.
