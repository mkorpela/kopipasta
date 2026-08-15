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
ANSWER = '{"relevant_files":["kopipasta/patcher.py"],"hypothesis":"fuzzy match reindents"}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

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
            out = {"model": "claude-opus-5", "content": content,
                   "usage": {"input_tokens": 412330, "output_tokens": 1840,
                             "cache_read_input_tokens": 389100,
                             "cache_creation_input_tokens": 0}}
        elif re.fullmatch(r"/v1beta/models/[^:/]+:generateContent", path):
            SEEN["gemini"] = (body, dict(self.headers))
            out = {"candidates": [{"finishReason": "STOP",
                                   "content": {"parts": [{"text": ANSWER}]}}],
                   "usageMetadata": {"promptTokenCount": 902110,
                                     "cachedContentTokenCount": 880000,
                                     "candidatesTokenCount": 1204,
                                     "totalTokenCount": 23314}}
        elif path == "/v1/chat/completions":
            SEEN["openai"] = (body, dict(self.headers))
            out = {"model": "gemini-3-pro", "choices": [{"message": {"content": ANSWER}}],
                   "usage": {"prompt_tokens": 402000, "completion_tokens": 1500,
                             "prompt_tokens_details": {"cached_tokens": 390000}}}
        else:
            self.send_response(404); self.end_headers(); return
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
    check("usage: 412330 in / 389100 cached / 1840 out",
          (c.input_tokens, c.cached_tokens, c.output_tokens) == (412330, 389100, 1840),
          f"{c.input_tokens}/{c.cached_tokens}/{c.output_tokens}")

    print("\ngemini: — native :generateContent")
    b = backends.build("gemini:gemini-3-pro", base_url=base + "/v1beta")
    c = b.complete(PREFIX, SUFFIX, schema=SCHEMA)
    body, hdrs = SEEN["gemini"]
    check("responseSchema set server-side", body["generationConfig"]["responseSchema"] == SCHEMA)
    check("responseMimeType application/json",
          body["generationConfig"]["responseMimeType"] == "application/json")
    check("key in x-goog-api-key header, not the URL", hdrs.get("x-goog-api-key") == "test")
    check("prefix+suffix sent as separate parts", len(body["contents"][0]["parts"]) == 2)
    check("candidates[0].content.parts[].text parsed", json.loads(c.text)["hypothesis"].startswith("fuzzy"))
    check("usage: 902110 prompt / 880000 cached / 1204 out",
          (c.input_tokens, c.cached_tokens, c.output_tokens) == (902110, 880000, 1204),
          f"{c.input_tokens}/{c.cached_tokens}/{c.output_tokens}")

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
