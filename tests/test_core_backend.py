"""The backend adapters, against a mock that speaks all three wire shapes.

No API key, no network beyond localhost. Both directions are asserted, because
each has cost a real defect:

  REQUEST  — did we send the right body? The cache breakpoint and the enforced
             schema are the two things the oracle is built on, and both are
             invisible in the response.
  RESPONSE — did we read the usage out of each provider's distinct shape? The
             numbers mean opposite things: Anthropic's `input_tokens` EXCLUDES
             cache traffic, Gemini's `promptTokenCount` INCLUDES it. Summing is
             right for one and double-counts for the other.

Every mocked field returns a distinctive non-zero value. A mock that returns
zeros cannot catch a dropped field — an adapter that silently discarded
`cache_creation` once passed 21 of 21 checks that way (findings, trap 8).
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kopipasta.core import backend as be
from kopipasta.core.config import BackendConfig
from kopipasta.core.errors import (
    EXIT_BACKEND,
    EXIT_NO_BACKEND,
    AuthRejected,
    ModelRejected,
    RateLimited,
    ResponseTruncated,
)

ANSWER = '{"relevant_files":[],"hypothesis":"none"}'
CACHE_NAME = "cachedContents/mock123"
CACHE_TOKENS = 16_277
PREFIX = "PREFIX-" + "x" * 200
SUFFIX = "SUFFIX-question"


class State:
    def __init__(self):
        self.seen = {}
        self.deleted = []
        self.created = []
        self.expire_next = False
        self.truncate_next = False
        self.status = None  # (code, body) forced on the next generate call
        self.count_status = None  # HTTP code forced on the next countTokens
        #: What GET /cachedContents answers with. Rented resources are the one
        #: thing this tool can leak money on, so the sweep is tested against a
        #: list it does not own as well as one it does.
        self.caches = []


@pytest.fixture
def server():
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            # The rental has to be handed back; recording the DELETE is the
            # only way to test that a cache was not left billing silently.
            state.deleted.append(self.path)
            self._send(200, {})

        def do_GET(self):
            if self.path == "/v1beta/cachedContents":
                return self._send(200, {"cachedContents": state.caches})
            self._send(404, {"error": {"message": "no such route"}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            path = self.path
            if path == "/v1/messages":
                state.seen["anthropic"] = (body, dict(self.headers))
                content = (
                    [{"type": "tool_use", "name": "emit", "input": json.loads(ANSWER)}]
                    if body.get("tool_choice")
                    else [{"type": "text", "text": ANSWER}]
                )
                return self._send(
                    200,
                    {
                        "model": "claude-opus-5",
                        "stop_reason": "max_tokens"
                        if state.truncate_next
                        else "end_turn",
                        "content": content,
                        # Real numbers from a live cold cached call: a ~20k
                        # prefix reports input_tokens=19.
                        "usage": {
                            "input_tokens": 19,
                            "output_tokens": 153,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 20343,
                        },
                    },
                )
            if path == "/v1beta/cachedContents":
                state.seen["gemini_cache_create"] = (body, dict(self.headers))
                state.created.append(body)
                return self._send(
                    200,
                    {
                        "name": f"{CACHE_NAME}-{len(state.created)}",
                        "displayName": body.get("displayName"),
                        "usageMetadata": {"totalTokenCount": CACHE_TOKENS},
                    },
                )
            if re.fullmatch(r"/v1beta/models/[^:/]+:countTokens", path):
                state.seen["gemini_count"] = (body, dict(self.headers))
                if state.count_status:
                    code = state.count_status
                    state.count_status = None
                    return self._send(code, {"error": {"code": code, "message": "no"}})
                text = "".join(
                    p.get("text", "")
                    for c in body.get("contents", [])
                    for p in c.get("parts", [])
                )
                return self._send(200, {"totalTokens": len(text) // 4})
            if re.fullmatch(r"/v1beta/models/[^:/]+:generateContent", path):
                state.seen["gemini"] = (body, dict(self.headers))
                if state.status:
                    code, message = state.status
                    state.status = None
                    return self._send(
                        code, {"error": {"code": code, "message": message}}
                    )
                if state.expire_next and body.get("cachedContent"):
                    state.expire_next = False
                    # The live API answers 403, not 404, and shares that code
                    # with genuine permission errors.
                    return self._send(
                        403,
                        {
                            "error": {
                                "message": "CachedContent not found (or permission denied)"
                            }
                        },
                    )
                finish = "MAX_TOKENS" if state.truncate_next else "STOP"
                return self._send(
                    200,
                    {
                        "candidates": [
                            {
                                "finishReason": finish,
                                "content": {"parts": [{"text": ANSWER}]},
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 16_293,
                            "candidatesTokenCount": 111,
                            "thoughtsTokenCount": 222,
                            "cachedContentTokenCount": CACHE_TOKENS
                            if body.get("cachedContent")
                            else 0,
                        },
                    },
                )
            if path.endswith("/chat/completions"):
                state.seen["openai"] = (body, dict(self.headers))
                return self._send(
                    200,
                    {
                        "model": "gpt-5",
                        "choices": [
                            {
                                "finish_reason": "length"
                                if state.truncate_next
                                else "stop",
                                "message": {"content": ANSWER},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 4242,
                            "completion_tokens": 77,
                            "prompt_tokens_details": {"cached_tokens": 1111},
                        },
                    },
                )
            self._send(404, {"error": {"message": "no such route"}})

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()


def cfg(provider, model, **kw):
    return BackendConfig(
        provider=provider,
        model=model,
        cache_ttl_s=kw.get("cache_ttl_s", 300),
        max_tokens=kw.get("max_tokens", 8192),
        timeout_s=kw.get("timeout_s", 30),
        sources={"provider": "--backend"},
    )


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(var, "test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


# -- none: the backend that makes everything else testable -----------------


def test_none_hands_the_payload_back():
    b = be.build(cfg("none", ""))
    out = b.complete(PREFIX, SUFFIX)
    assert PREFIX in out.text and SUFFIX in out.text
    assert b.dry_run is True
    # No usage is invented. An estimate reported as measured usage is the
    # class of mistake that hides a real cost.
    assert (out.input_tokens, out.output_tokens, out.cost_usd) == (0, 0, 0.0)


def test_none_with_a_file_is_not_a_dry_run(tmp_path):
    canned = tmp_path / "a.json"
    canned.write_text(ANSWER)
    b = be.build(cfg("none", str(canned)))
    assert b.complete(PREFIX, SUFFIX).text == ANSWER
    assert b.dry_run is False


# -- anthropic --------------------------------------------------------------


def test_anthropic_marks_the_cache_breakpoint_at_the_end_of_the_prefix(server):
    b = be.build(cfg("anthropic", "claude-opus-5"), base_url=server.url)
    b.complete(PREFIX, SUFFIX)
    blocks = server.seen["anthropic"][0]["messages"][0]["content"]
    assert blocks[0]["text"] == PREFIX
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # The task must NOT be inside the cached span, or reuse dies every turn.
    assert "cache_control" not in blocks[1]
    assert blocks[1]["text"] == SUFFIX


def test_anthropic_input_tokens_include_cache_traffic(server):
    """`input_tokens` counts only tokens neither read from nor written to the
    cache, so reporting it alone claims a 20k prefix sent 19 tokens."""
    out = be.build(cfg("anthropic", "m"), base_url=server.url).complete(PREFIX, SUFFIX)
    assert out.input_tokens == 19 + 20343
    assert out.cache_creation_tokens == 20343


def test_anthropic_enforces_a_schema_with_a_forced_tool(server):
    schema = {"type": "object", "properties": {}}
    out = be.build(cfg("anthropic", "m"), base_url=server.url).complete(
        PREFIX, SUFFIX, schema=schema
    )
    body = server.seen["anthropic"][0]
    assert body["tool_choice"] == {"type": "tool", "name": "emit"}
    assert body["tools"][0]["input_schema"] == schema
    assert json.loads(out.text) == json.loads(ANSWER)


def test_anthropic_truncation_is_a_failure_not_a_short_answer(server):
    server.truncate_next = True
    with pytest.raises(ResponseTruncated) as exc:
        be.build(cfg("anthropic", "m"), base_url=server.url).complete(PREFIX, SUFFIX)
    assert exc.value.exit_code == EXIT_BACKEND
    assert exc.value.retryable is False


# -- gemini: the cache is a rented resource --------------------------------


def gem(server, **kw):
    # base_url replaces the whole base, version path included — the same trap
    # as pointing a compat client at a URL missing its trailing segment.
    return be.build(
        cfg("gemini", "gemini-3.7-flash"), base_url=f"{server.url}/v1beta", **kw
    )


def test_gemini_does_not_create_a_cache_unless_asked(server):
    """A one-shot question would pay storage rent for a turn 2 that never comes."""
    gem(server).complete(PREFIX, SUFFIX)
    assert server.created == []
    assert server.seen["gemini"][0]["contents"][0]["parts"][0]["text"] == PREFIX


def test_gemini_cache_carries_an_explicit_ttl_and_an_identifiable_name(server):
    gem(server, cache=True, cache_ttl_s=120).complete(PREFIX, SUFFIX)
    body = server.created[0]
    assert body["ttl"] == "120s"  # never the server default, which is unbounded
    assert body["displayName"].startswith("kopipasta-")


def test_the_display_name_says_which_project_rented_it(server):
    """One API key serves every repo on the machine and the cache list is per
    key, so without this a sweep in repo A cannot tell repo B's live lease from
    its own orphan."""
    gem(server, cache=True, label="myrepo-abc123").complete(PREFIX, SUFFIX)
    assert server.created[0]["displayName"].startswith("kopipasta-myrepo-abc123-")


def test_the_sweep_leaves_a_cache_a_session_is_still_renting(server, monkeypatch):
    """The money bug, from the other side.

    A named session deliberately leaves its cache alive so turn 2 can reuse
    it, so "a cache exists that no process is using" is the expected state.
    An unfiltered sweep deleted those, and every following turn silently paid
    full price — measured live at 16,329 tokens re-created per turn.
    """
    monkeypatch.setenv("GEMINI_BASE_URL", f"{server.url}/v1beta")
    server.caches = [
        {"name": "cachedContents/leased", "displayName": "kopipasta-proj-aaaa"},
        {"name": "cachedContents/orphan", "displayName": "kopipasta-proj-bbbb"},
    ]
    assert be.GeminiBackend.reap_orphans(keep=["cachedContents/leased"]) == 1
    assert server.deleted == ["/v1beta/cachedContents/orphan"]


def test_the_sweep_leaves_another_project_alone(server, monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", f"{server.url}/v1beta")
    server.caches = [
        {"name": "cachedContents/mine", "displayName": "kopipasta-proj_a-aaaa"},
        {"name": "cachedContents/theirs", "displayName": "kopipasta-proj_b-bbbb"},
        {"name": "cachedContents/notours", "displayName": "something-else"},
    ]
    assert be.GeminiBackend.reap_orphans(label="proj_a") == 1
    assert server.deleted == ["/v1beta/cachedContents/mine"]
    # Without a label the sweep is machine-wide, which is what you want after
    # a crash and nowhere else.
    server.deleted.clear()
    assert be.GeminiBackend.reap_orphans() == 2


def test_a_session_lease_is_handed_back_by_name(server, monkeypatch):
    """`session rm` deletes the only record of the resource name, so the
    release has to happen first or the meter runs with nothing to read it."""
    monkeypatch.setenv("GEMINI_BASE_URL", f"{server.url}/v1beta")
    assert be.release_lease({"provider": "gemini", "name": "cachedContents/x"}) is True
    assert server.deleted == ["/v1beta/cachedContents/x"]
    # Providers whose caches cost nothing to abandon have no rental to return.
    assert be.release_lease({"provider": "anthropic", "name": "whatever"}) is False


def test_the_provider_counts_its_own_tokens(server):
    """spec §5. The estimator is calibrated per provider and still ~8% out;
    countTokens is free and exact, and `--strict-budget` decides between
    running and refusing, which is not a decision to make on a guess."""
    assert gem(server).count_tokens("x" * 400) == 100
    body, _ = server.seen["gemini_count"]
    assert body["contents"][0]["parts"][0]["text"] == "x" * 400


def test_a_provider_that_will_not_count_does_not_stop_the_run(server):
    """An exact number is an improvement on the estimate, never a
    precondition for running. Anything genuinely wrong with the credentials
    fails again immediately, with a real error, on the call itself."""
    server.count_status = 429
    assert gem(server).count_tokens("hello") is None
    # ... and the backend is still perfectly usable afterwards.
    assert gem(server).complete(PREFIX, SUFFIX).text == ANSWER
    gem(server, cache=True, cache_ttl_s=99999).complete(PREFIX, SUFFIX)
    assert server.created[0]["ttl"] == f"{be.GeminiBackend.MAX_TTL_S}s"
    server.created.clear()
    gem(server, cache=True, cache_ttl_s=0).complete(PREFIX, SUFFIX)
    assert server.created[0]["ttl"] == "1s"  # 0 must not become "server decides"


def test_gemini_does_not_resend_a_prefix_it_has_already_cached(server):
    gem(server, cache=True).complete(PREFIX, SUFFIX)
    body = server.seen["gemini"][0]
    assert body["cachedContent"].startswith(CACHE_NAME)
    assert PREFIX not in json.dumps(body["contents"])


def test_gemini_cached_tokens_are_not_summed_into_the_input(server):
    """Unlike Anthropic, cachedContentTokenCount is already inside promptTokenCount."""
    out = gem(server, cache=True).complete(PREFIX, SUFFIX)
    assert out.input_tokens == 16_293
    assert out.cached_tokens == CACHE_TOKENS
    assert out.cache_creation_tokens == CACHE_TOKENS  # this turn paid to write it


def test_gemini_counts_reasoning_tokens_as_output(server):
    out = gem(server).complete(PREFIX, SUFFIX)
    assert out.output_tokens == 111 + 222


def test_gemini_close_hands_the_rental_back(server):
    b = gem(server, cache=True)
    b.complete(PREFIX, SUFFIX)
    b.close()
    assert server.deleted, "a cache left alive bills per token-hour until its TTL"
    b.close()  # idempotent


def test_gemini_hand_over_transfers_ownership_instead_of_deleting(server):
    """A session owns the cache across processes, which is the only way turn 2
    of a conversation can read what turn 1 paid to create."""
    b = gem(server, cache=True)
    b.complete(PREFIX, SUFFIX)
    handle = b.handle()
    assert handle and handle["digest"] == be.GeminiBackend.digest(PREFIX)
    b.hand_over()
    b.close()
    assert server.deleted == []


def test_an_adopted_handle_says_it_was_adopted(server):
    """Reusing a cache does not renew its lease, and only the owner of the
    lease may set the expiry. Without this flag a session's record renews an
    expiry the provider never extended, and every later turn spends a round
    trip discovering the 403 before rebuilding."""
    created = gem(server, cache=True)
    created.complete(PREFIX, SUFFIX)
    assert created.handle()["adopted"] is False
    created.hand_over()

    inherited = gem(server)
    inherited.adopt(
        name=f"{CACHE_NAME}-1",
        digest=be.GeminiBackend.digest(PREFIX),
        expires_in_s=300,
        tokens=CACHE_TOKENS,
    )
    inherited.complete(PREFIX, SUFFIX)
    assert inherited.handle()["adopted"] is True


def test_gemini_adopts_a_handle_from_an_earlier_process(server):
    b = gem(server)
    b.adopt(
        name=f"{CACHE_NAME}-adopted",
        digest=be.GeminiBackend.digest(PREFIX),
        expires_in_s=300,
        tokens=CACHE_TOKENS,
    )
    b.complete(PREFIX, SUFFIX)
    assert server.created == []  # reused, not recreated
    assert server.seen["gemini"][0]["cachedContent"] == f"{CACHE_NAME}-adopted"


def test_gemini_survives_a_cache_that_expired_under_it(server):
    """A short TTL trades a cost risk for an availability risk. The retry is
    keyed on the message, not the 403, which is shared with auth failures."""
    b = gem(server)
    b.adopt(
        name=f"{CACHE_NAME}-stale",
        digest=be.GeminiBackend.digest(PREFIX),
        expires_in_s=300,
        tokens=1,
    )
    server.expire_next = True
    out = b.complete(PREFIX, SUFFIX)
    assert out.text == ANSWER  # the turn succeeds rather than surfacing a 403
    assert len(server.created) == 1  # rebuilt from scratch, once
    assert server.seen["gemini"][0]["cachedContent"] != f"{CACHE_NAME}-stale"


def test_gemini_degrades_to_full_price_when_a_cache_cannot_be_made(server, monkeypatch):
    """Below the provider's minimum cacheable size is a fallback, not an error:
    the call should cost money, not fail."""
    real = be._post

    def fail_on_create(url, *a, **kw):
        if url.endswith("/cachedContents"):
            raise be.BackendError("HTTP 400: payload too small to cache")
        return real(url, *a, **kw)

    monkeypatch.setattr(be, "_post", fail_on_create)
    b = gem(server, cache=True)
    out = b.complete(PREFIX, SUFFIX)
    assert out.text == ANSWER
    assert b.cache_disabled_reason
    assert PREFIX in json.dumps(server.seen["gemini"][0]["contents"])


def test_gemini_truncation_names_both_numbers(server):
    server.truncate_next = True
    with pytest.raises(ResponseTruncated) as exc:
        gem(server).complete(PREFIX, SUFFIX, max_tokens=8192)
    assert "8,192" in exc.value.detail
    assert exc.value.to_json()["finish_reason"] == "MAX_TOKENS"


def test_gemini_sends_the_key_in_a_header_not_the_query(server):
    gem(server).complete(PREFIX, SUFFIX)
    headers = server.seen["gemini"][1]
    assert headers["x-goog-api-key"] == "test-key"


# -- openai-compatible ------------------------------------------------------


def test_openai_enforces_a_schema_and_reports_cached_tokens(server):
    out = be.build(cfg("openai", "gpt-5"), base_url=server.url).complete(
        PREFIX, SUFFIX, schema={"type": "object"}
    )
    body = server.seen["openai"][0]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert out.input_tokens == 4242
    assert out.cached_tokens == 1111


def test_openai_length_stop_is_truncation(server):
    server.truncate_next = True
    with pytest.raises(ResponseTruncated):
        be.build(cfg("openai", "gpt-5"), base_url=server.url).complete(PREFIX, SUFFIX)


# -- the error taxonomy, spec §9 -------------------------------------------


@pytest.mark.parametrize(
    "status,message,expected,retryable,exit_code",
    [
        (401, "invalid api key", AuthRejected, False, EXIT_NO_BACKEND),
        (429, "quota exceeded", RateLimited, True, EXIT_BACKEND),
        (404, "model not found: nope", ModelRejected, False, EXIT_BACKEND),
    ],
)
def test_provider_refusals_map_to_the_right_kind_of_failure(
    server, status, message, expected, retryable, exit_code
):
    """Retryability is not derivable from the exit code: 3 covers both "the API
    is briefly down" and "you named a model that does not exist"."""
    server.status = (status, message)
    with pytest.raises(expected) as exc:
        gem(server).complete(PREFIX, SUFFIX)
    assert exc.value.retryable is retryable
    assert exc.value.exit_code == exit_code
    # The provider's own words survive: paraphrasing destroys the one detail
    # that identifies the failure.
    assert message in exc.value.render()


def test_a_server_error_is_retryable(server):
    server.status = (503, "upstream unavailable")
    with pytest.raises(be.BackendError) as exc:
        gem(server).complete(PREFIX, SUFFIX)
    assert exc.value.retryable is True
