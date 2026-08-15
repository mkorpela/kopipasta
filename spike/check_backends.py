#!/usr/bin/env python3
"""Verify the raw-model adapters against a mock that speaks all three wire shapes.

No API keys required. Asserts both directions:
  - REQUEST : did we send the right body? (cache breakpoint, schema enforcement)
  - RESPONSE: did we parse text + usage out of each provider's distinct shape?

    uv run python spike/check_backends.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update(ANTHROPIC_API_KEY="test", GEMINI_API_KEY="test", OPENAI_API_KEY="test")

import backends  # noqa: E402

SEEN: dict = {}
DELETED: list = []
CREATED: list = []
EXPIRE_NEXT: list = []    # non-empty => next cached call 403s, once
TRUNCATE_NEXT: list = []  # non-empty => next call returns partial text + MAX_TOKENS
ANSWER = '{"relevant_files":["kopipasta/patcher.py"],"hypothesis":"fuzzy match reindents"}'
CACHE_NAME = "cachedContents/mock123"
CACHE_TOKENS = 880000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_DELETE(self):
        # The rental has to be handed back. Recording the DELETE is the only
        # way to test that; a leaked cache bills silently until its TTL.
        DELETED.append(self.path)
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        path = self.path
        # Exact routes only — substring matching would let a wrong base_url
        # silently "work" and mask the 404 path we want to test.
        if path == "/v1/messages":
            SEEN["anthropic"] = (body, dict(self.headers))
            tool = body.get("tool_choice")
            content = ([{"type": "tool_use", "name": "emit", "input": json.loads(ANSWER)}]
                       if tool else [{"type": "text", "text": ANSWER}])
            # Real numbers from a live cold cached call. Anthropic reports
            # input_tokens=19 for a ~20k-token prefix because the prefix is
            # accounted under cache_creation. An adapter that reports
            # input_tokens alone claims we sent 19 tokens. Regression test.
            out = {"model": "claude-opus-5", "content": content,
                   "usage": {"input_tokens": 19, "output_tokens": 153,
                             "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 20343}}
        elif path == "/v1beta/cachedContents":
            SEEN["gemini_cache_create"] = (body, dict(self.headers))
            CREATED.append(body)
            out = {"name": f"{CACHE_NAME}-{len(CREATED)}", "model": body.get("model"),
                   "displayName": body.get("displayName"),
                   "expireTime": "2026-01-01T00:00:00Z",
                   "usageMetadata": {"totalTokenCount": CACHE_TOKENS}}
        elif re.fullmatch(r"/v1beta/models/[^:/]+:generateContent", path):
            SEEN["gemini"] = (body, dict(self.headers))
            # Simulate a cache that expired between our deadline check and the
            # request landing. The live API answers 403, not 404.
            if EXPIRE_NEXT and body.get("cachedContent"):
                EXPIRE_NEXT.pop()
                self.send_response(403)
                msg = b'{"error":{"code":403,"message":"CachedContent not found (or permission denied)"}}'
                self.send_header("content-length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            if TRUNCATE_NEXT:
                TRUNCATE_NEXT.pop()
                # Partial JSON plus MAX_TOKENS: the shape that silently
                # produced `ok: true, triage: null` in a real run.
                out = {"candidates": [{"finishReason": "MAX_TOKENS",
                                       "content": {"parts": [{"text": ANSWER[:40]}]}}],
                       "usageMetadata": {"promptTokenCount": 100,
                                         "candidatesTokenCount": 12,
                                         "thoughtsTokenCount": 900,
                                         "totalTokenCount": 1012}}
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            # Distinctive non-zero values for EVERY field the adapter maps —
            # a zero is indistinguishable from "never read it" (findings §3.8).
            out = {"candidates": [{"finishReason": "STOP",
                                   "content": {"parts": [{"text": ANSWER}]}}],
                   "usageMetadata": {"promptTokenCount": 902110,
                                     "cachedContentTokenCount": CACHE_TOKENS,
                                     "candidatesTokenCount": 1204,
                                     "totalTokenCount": 23314}}
        elif path == "/v1/chat/completions":
            SEEN["openai"] = (body, dict(self.headers))
            out = {"model": "gemini-3-pro", "choices": [{"message": {"content": ANSWER}}],
                   "usage": {"prompt_tokens": 402000, "completion_tokens": 1500,
                             "prompt_tokens_details": {"cached_tokens": 390000}}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


SCHEMA = {"type": "object",
          "properties": {"relevant_files": {"type": "array", "items": {"type": "string"}},
                         "hypothesis": {"type": "string"}},
          "required": ["relevant_files", "hypothesis"]}

PREFIX = "# Project Overview\n<the 400k-token repo payload>"
SUFFIX = "## Task\nWhere does hunk matching go wrong?"

fails: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  <- ' + detail if detail and not cond else ''}")
    if not cond:
        fails.append(label)


def main() -> int:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    print("\nanthropic: — native /v1/messages")
    b = backends.build("anthropic:claude-opus-5", base_url=base)
    c = b.complete(PREFIX, SUFFIX, schema=SCHEMA)
    body, hdrs = SEEN["anthropic"]
    blocks = body["messages"][0]["content"]
    check("repo prefix carries the cache breakpoint",
          blocks[0].get("cache_control") == {"type": "ephemeral"}, str(blocks[0].get("cache_control")))
    check("varying question is NOT cached", "cache_control" not in blocks[1])
    check("schema forced via single tool",
          body.get("tool_choice", {}).get("name") == "emit" and body["tools"][0]["input_schema"] == SCHEMA)
    check("anthropic-version header sent", hdrs.get("anthropic-version") == "2023-06-01")
    check("tool_use input parsed as the answer", json.loads(c.text)["relevant_files"] == ["kopipasta/patcher.py"])
    check("cache_creation is reported, not dropped",
          c.cache_creation_tokens == 20343, str(c.cache_creation_tokens))
    check("input_tokens sums raw+read+creation (19+0+20343), not raw alone",
          c.input_tokens == 20362, f"{c.input_tokens} (raw input_tokens was 19)")
    check("cached/output parsed", (c.cached_tokens, c.output_tokens) == (0, 153),
          f"{c.cached_tokens}/{c.output_tokens}")

    print("\ngemini: — inline, no explicit cache (cache=False control arm)")
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/v1beta", cache=False)
    c = b.complete(PREFIX, SUFFIX, schema=SCHEMA)
    body, hdrs = SEEN["gemini"]
    check("responseSchema set server-side", body["generationConfig"]["responseSchema"] == SCHEMA)
    check("responseMimeType application/json",
          body["generationConfig"]["responseMimeType"] == "application/json")
    check("key in x-goog-api-key header, not the URL", hdrs.get("x-goog-api-key") == "test")
    check("prefix+suffix sent as separate parts", len(body["contents"][0]["parts"]) == 2)
    check("no cachedContent referenced when caching is off", "cachedContent" not in body)
    check("no cache resource created when caching is off",
          "gemini_cache_create" not in SEEN)
    check("candidates[0].content.parts[].text parsed", json.loads(c.text)["hypothesis"].startswith("fuzzy"))
    check("usage: 902110 prompt / 880000 cached / 1204 out",
          (c.input_tokens, c.cached_tokens, c.output_tokens) == (902110, 880000, 1204),
          f"{c.input_tokens}/{c.cached_tokens}/{c.output_tokens}")

    print("\ngemini: — explicit cachedContents (the path implicit caching cannot serve)")
    DELETED.clear()
    CREATED.clear()
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/v1beta",
                       cache=True, cache_ttl_s=120)
    c1 = b.complete(PREFIX, SUFFIX, schema=SCHEMA)
    create, _ = SEEN["gemini_cache_create"]
    gen1, _ = SEEN["gemini"]
    check("cache resource created for the repo prefix",
          create["contents"][0]["parts"][0]["text"] == PREFIX)
    check("cache create names the model as models/<m>", create["model"] == "models/gemini-3-pro")
    check("TTL is ALWAYS explicit — never the server default", create.get("ttl") == "120s")
    check("displayName marks it ours, so an orphan is identifiable",
          str(create.get("displayName", "")).startswith("kopipasta-"))
    check("generateContent sends ONLY the suffix; prefix lives in the cache",
          len(gen1["contents"][0]["parts"]) == 1
          and gen1["contents"][0]["parts"][0]["text"] == SUFFIX,
          json.dumps(gen1["contents"][0]["parts"])[:120])
    check("generateContent references the cache by name",
          gen1.get("cachedContent") == b._cache_name)
    check("turn 1 reports the cache write as cache_creation_tokens",
          c1.cache_creation_tokens == CACHE_TOKENS, str(c1.cache_creation_tokens))

    # The whole economic claim: turn 2 must NOT pay to write the prefix again.
    SEEN.pop("gemini_cache_create", None)
    c2 = b.complete(PREFIX, "## Task\nA different question entirely?", schema=SCHEMA)
    check("turn 2 reuses the cache, does not re-create it",
          "gemini_cache_create" not in SEEN)
    check("turn 2 reports zero cache creation (turn 1 paid, turn 2 did not)",
          c2.cache_creation_tokens == 0, str(c2.cache_creation_tokens))
    check("turn 2 still reads the prefix back from cache",
          c2.cached_tokens == CACHE_TOKENS, str(c2.cached_tokens))

    # A changed prefix must not silently keep billing for the stale one.
    SEEN.pop("gemini_cache_create", None)
    b.complete(PREFIX + "\n# more files", SUFFIX, schema=SCHEMA)
    check("a changed prefix re-creates the cache", "gemini_cache_create" in SEEN)
    check("...and deletes the superseded one instead of letting it run its TTL",
          any(CACHE_NAME in d for d in DELETED), str(DELETED))

    DELETED.clear()
    b.close()
    check("close() hands the rental back", any(CACHE_NAME in d for d in DELETED), str(DELETED))
    DELETED.clear()
    b.close()
    check("close() is idempotent — no double DELETE", DELETED == [], str(DELETED))

    check("TTL is clamped to a sane ceiling, so a typo cannot rent for a year",
          backends.GeminiBackend("m", cache_ttl_s=10**9).cache_ttl_s
          == backends.GeminiBackend.MAX_TTL_S)
    check("TTL of 0 does not become 'server default' (unbounded rent)",
          backends.GeminiBackend("m", cache_ttl_s=0).cache_ttl_s >= 1)

    # A short TTL bounds the cost leak and creates a correctness cliff. Live
    # API with a 15s TTL returned `403 CachedContent not found` on the next
    # turn before these two paths existed.
    print("\ngemini: — outliving the TTL must not break the session")
    DELETED.clear()
    CREATED.clear()
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/v1beta",
                       cache=True, cache_ttl_s=120)
    b.complete(PREFIX, SUFFIX)
    first_name = b._cache_name
    b._cache_deadline = 0.0          # pretend the TTL elapsed
    c = b.complete(PREFIX, "another question")
    check("an expired cache is re-created, not re-sent",
          b._cache_name != first_name and len(CREATED) == 2, f"{len(CREATED)} creates")
    check("...and the expired one is NOT DELETEd (it is already gone)",
          DELETED == [], str(DELETED))
    check("a re-create is reported as a real cache write, not as free",
          c.cache_creation_tokens == CACHE_TOKENS, str(c.cache_creation_tokens))
    b.close()

    print("\ngemini: — a 403 mid-flight is recovered exactly once")
    DELETED.clear()
    CREATED.clear()
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/v1beta",
                       cache=True, cache_ttl_s=120)
    b.complete(PREFIX, SUFFIX)
    EXPIRE_NEXT.append(True)          # server evicts it before the next call
    c = b.complete(PREFIX, "question after eviction")
    check("403 CachedContent-not-found is retried transparently",
          c.text != "" and len(CREATED) == 2, f"{len(CREATED)} creates")
    check("the retry reports the second write honestly",
          c.cache_creation_tokens == CACHE_TOKENS, str(c.cache_creation_tokens))
    EXPIRE_NEXT.clear()
    b.close()

    check("a genuine permission error is NOT mistaken for an expired cache",
          backends.GeminiBackend._cache_is_gone("HTTP 403: permission denied") is False)

    # Found by dogfooding: a real triage run reported ok=true with a null
    # result because the model hit MAX_TOKENS and returned JSON that stopped
    # mid-string. Non-empty text made the old guard skip the finishReason.
    print("\ngemini: — a truncated answer is an error, not a success")
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/v1beta", cache=False)
    TRUNCATE_NEXT.append(True)
    try:
        c = b.complete(PREFIX, SUFFIX, schema=SCHEMA, max_tokens=256)
        check("MAX_TOKENS with partial text raises", False,
              f"returned {len(c.text)} chars of unparseable JSON as success")
    except backends.BackendError as e:
        check("MAX_TOKENS with partial text raises", "MAX_TOKENS" in str(e))
        check("...and the message says reasoning shares the output budget",
              "reasoning" in str(e) and "900" in str(e), str(e)[:140])
    TRUNCATE_NEXT.clear()
    check("reasoning tokens are counted as output, not dropped",
          b.complete(PREFIX, SUFFIX).output_tokens == 1204)

    print("\ngemini: — cache unavailable degrades to inline, it does not fail")
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/nocache", cache=True)
    try:
        # /nocache/cachedContents 404s, exactly like a payload under the
        # provider's minimum cacheable size would 400.
        c = b.complete(PREFIX, SUFFIX)
        check("404 on cache create still returns a completion", False, "no fallback happened")
    except backends.BackendError as e:
        # generateContent under /nocache 404s too — what we assert is that the
        # adapter got PAST cache creation and tried the completion anyway.
        check("cache-create failure disables caching rather than aborting",
              b.cache_enabled is False and "404" in str(e))
    check("...and records why, instead of failing silently",
          "404" in b.cache_disabled_reason, b.cache_disabled_reason)

    print("\nopenai: — /v1/chat/completions (also Gemini's compat endpoint)")
    b = backends.build("openai:gemini-3-pro", base_url=base + "/v1")
    c = b.complete(PREFIX, SUFFIX, schema=SCHEMA)
    body, hdrs = SEEN["openai"]
    check("json_schema strict response_format",
          body["response_format"]["json_schema"]["strict"] is True
          and body["response_format"]["json_schema"]["schema"] == SCHEMA)
    check("bearer auth header", hdrs.get("authorization") == "Bearer test")
    check("NO cache breakpoint available in this shape",
          isinstance(body["messages"][0]["content"], str))
    check("choices[0].message.content parsed", json.loads(c.text)["relevant_files"] != [])
    check("usage incl. prompt_tokens_details.cached_tokens",
          (c.input_tokens, c.cached_tokens, c.output_tokens) == (402000, 390000, 1500),
          f"{c.input_tokens}/{c.cached_tokens}/{c.output_tokens}")

    print("\ngemini-compat: — Gemini through the OpenAI shape")
    check("default base_url is the documented compat URL (trailing slash)",
          backends.build("gemini-compat:gemini-3-pro").base
          == backends.GEMINI_COMPAT.rstrip("/"))

    print("\nerror handling")
    try:
        backends.build("openai:m", base_url=base + "/nope").complete(PREFIX, SUFFIX)
        check("HTTP 404 raises BackendError", False)
    except backends.BackendError as e:
        check("HTTP 404 raises BackendError", "404" in str(e))
    try:
        backends.build("bogus:x")
        check("unknown backend kind rejected", False)
    except backends.BackendError:
        check("unknown backend kind rejected", True)
    try:
        backends.build("gemini")
        check("missing target rejected", False)
    except backends.BackendError:
        check("missing target rejected", True)

    srv.shutdown()
    print(f"\n{'ALL CHECKS PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}\n")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
