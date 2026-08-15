#!/usr/bin/env python3
"""Raw-model backends for the context oracle — the layer *below* agent CLIs.

Zero new dependencies: `requests` is already a kopipasta dependency, and every
one of these APIs is a single JSON POST for the non-streaming case.

    exec:<command>                 any CLI: stdin -> stdout (an agent, tools off)
    claude-cli:<model|->           claude -p, with real usage + enforced schema
    anthropic:<model>              native /v1/messages    — explicit cache breakpoints
    gemini:<model>                 native :generateContent — responseSchema, 1M+ ctx
    openai:<model>                 /v1/chat/completions   — OpenAI, OpenRouter, vLLM,
                                                            Groq, LM Studio, and Gemini
                                                            via its compat endpoint

Two things the oracle needs that generic "just use an OpenAI-compatible URL"
advice loses, and which is why the two native adapters exist:

  1. CACHE CONTROL. The repo payload is a stable prefix reused across turns.
     Anthropic lets you place the breakpoint explicitly (cache_control);
     Gemini has explicit cachedContents; the compat layer exposes neither.
  2. SERVER-ENFORCED SCHEMA. Triage mode wants guaranteed JSON, not begging.
     Gemini responseSchema and OpenAI json_schema are enforced by the API.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0
    # Tokens written into the cache on this call. For CLI backends this lumps
    # the harness system prompt together with our payload and does NOT
    # separate them — do not report it as "our" input. Measure the harness
    # floor by running an empty prompt and subtracting.
    cache_creation_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


class BackendError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# exec: — an agent CLI, driven as a completion (tools MUST be off; see spec §7)
# ---------------------------------------------------------------------------
class ExecBackend:
    def __init__(self, command: str, timeout: int = 900):
        self.command, self.timeout = command, timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        try:
            p = subprocess.run(
                self.command, shell=True, input=prefix + "\n\n" + suffix,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(f"timed out after {self.timeout}s")
        if p.returncode != 0:
            raise BackendError((p.stderr or "non-zero exit")[-800:])
        # No usage accounting available — that is the price of this backend.
        return Completion(text=p.stdout, model=self.command)


# ---------------------------------------------------------------------------
# claude-cli: — exec:, but using what the CLI actually exposes.
#
# `claude -p --output-format json` returns real usage and cost, and
# `--json-schema` enforces structured output server-side. So the two things
# plain exec: gives up are NOT inherent to CLI-backed oracles — they were
# just flags we had not read. Still zero key custody.
#
# What it does NOT give you: control over the cache breakpoint, freedom from
# the harness's own ~34k-token system prompt, or clean attribution — the CLI
# reports our payload and its own prompt together under cache_creation, with
# input_tokens counting only the uncached delta.
# ---------------------------------------------------------------------------
class ClaudeCliBackend:
    TOOLS_OFF = "Edit,Write,Read,Bash,Glob,Grep,Task,WebFetch,WebSearch,NotebookEdit,TodoWrite"

    def __init__(self, model: str = "", timeout: int = 900, binary: str = "claude"):
        self.model, self.timeout, self.binary = model, timeout, binary

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        cmd = [self.binary, "-p", "--output-format", "json",
               "--disallowedTools", self.TOOLS_OFF]
        if self.model:
            cmd += ["--model", self.model]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        try:
            p = subprocess.run(cmd, input=prefix + "\n\n" + suffix,
                               capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise BackendError(f"timed out after {self.timeout}s")
        if p.returncode != 0:
            raise BackendError((p.stderr or "non-zero exit")[-800:])
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise BackendError(f"expected --output-format json, got: {p.stdout[:300]}")
        if d.get("is_error"):
            raise BackendError(str(d.get("result"))[:600])

        if schema:
            so = d.get("structured_output")
            text = json.dumps(so) if so is not None else str(d.get("result", ""))
        else:
            text = str(d.get("result", ""))

        u = d.get("usage") or {}
        models = list((d.get("modelUsage") or {}).keys())
        return Completion(
            text=text,
            input_tokens=u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
            cost_usd=d.get("total_cost_usd", 0.0) or 0.0,
            model=",".join(models) or self.model,
            raw=d,
        )


# ---------------------------------------------------------------------------
# anthropic: — cache_control marks the end of the stable repo prefix
# ---------------------------------------------------------------------------
class AnthropicBackend:
    def __init__(self, model: str, base_url: Optional[str] = None, timeout: int = 900):
        self.model = model
        self.base = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        content = [
            # The repo payload. Everything up to here is cached and reused
            # verbatim on the next turn of this session.
            {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": suffix},
        ]
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if schema:
            # Anthropic has no response_format; a single forced tool is the
            # equivalent, and is enforced server-side the same way.
            body["tools"] = [{"name": "emit", "description": "Return the result.",
                              "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "emit"}

        d = _post(f"{self.base}/v1/messages", body, timeout=self.timeout, headers={
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })

        if schema:
            text = ""
            for blk in d.get("content", []):
                if blk.get("type") == "tool_use":
                    text = json.dumps(blk.get("input", {}))
                    break
        else:
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")

        u = d.get("usage", {})
        return Completion(
            text=text,
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            # creation is what you paid to write the cache; read is what you saved.
            cached_tokens=u.get("cache_read_input_tokens", 0),
            model=d.get("model", self.model),
            raw=d,
        )


# ---------------------------------------------------------------------------
# gemini: — native. The 1M+ context is the reason this adapter exists.
# ---------------------------------------------------------------------------
class GeminiBackend:
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: str, base_url: Optional[str] = None, timeout: int = 900):
        self.model = model
        self.base = (base_url or os.environ.get("GEMINI_BASE_URL") or self.BASE).rstrip("/")
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        self.timeout = timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        gen: Dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_tokens}
        if schema:
            # Enforced by the API. This is what upgrades triage mode from
            # "please return JSON" to a guarantee.
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = schema

        body = {
            "contents": [{"role": "user", "parts": [{"text": prefix}, {"text": suffix}]}],
            "generationConfig": gen,
        }
        # Header, not ?key= — query params leak into proxy and access logs.
        d = _post(f"{self.base}/models/{self.model}:generateContent", body,
                  timeout=self.timeout,
                  headers={"x-goog-api-key": self.key, "content-type": "application/json"})

        cands = d.get("candidates") or []
        text = ""
        if cands:
            text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        if not text and cands and cands[0].get("finishReason") not in (None, "STOP"):
            raise BackendError(f"gemini stopped early: {cands[0].get('finishReason')}")

        u = d.get("usageMetadata", {})
        return Completion(
            text=text,
            # NB: cachedContentTokenCount is *included* in promptTokenCount.
            input_tokens=u.get("promptTokenCount", 0),
            output_tokens=u.get("candidatesTokenCount", 0),
            cached_tokens=u.get("cachedContentTokenCount", 0),
            model=self.model,
            raw=d,
        )


# ---------------------------------------------------------------------------
# openai: — the widest-reach shape. Gemini also speaks this (see GEMINI_COMPAT).
# ---------------------------------------------------------------------------
GEMINI_COMPAT = "https://generativelanguage.googleapis.com/v1beta/openai/"  # trailing / matters


class OpenAICompatBackend:
    def __init__(self, model: str, base_url: Optional[str] = None, timeout: int = 900):
        self.model = model
        self.base = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout

    def complete(self, prefix: str, suffix: str, *, schema=None, max_tokens=8192) -> Completion:
        body: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "user", "content": prefix + "\n\n" + suffix}],
        }
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "oracle_result", "strict": True, "schema": schema}}

        d = _post(f"{self.base}/chat/completions", body, timeout=self.timeout, headers={
            "authorization": f"Bearer {self.key}", "content-type": "application/json"})

        choices = d.get("choices") or []
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        u = d.get("usage", {})
        return Completion(
            text=text or "",
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            model=d.get("model", self.model),
            raw=d,
        )


# ---------------------------------------------------------------------------
def _post(url: str, body: Dict[str, Any], *, headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise BackendError(f"transport: {e}") from e
    if r.status_code >= 400:
        raise BackendError(f"HTTP {r.status_code}: {r.text[:600]}")
    try:
        return dict(r.json())
    except ValueError as e:
        raise BackendError(f"non-JSON response: {r.text[:300]}") from e


def build(spec: str, *, base_url: Optional[str] = None, timeout: int = 900):
    """`exec:cmd` | `anthropic:model` | `gemini:model` | `openai:model`"""
    kind, _, rest = spec.partition(":")
    if not rest:
        raise BackendError(f"backend needs a target: '{spec}' (e.g. gemini:gemini-3-pro)")
    if kind == "exec":
        return ExecBackend(rest, timeout)
    if kind == "claude-cli":
        return ClaudeCliBackend("" if rest == "-" else rest, timeout)
    if kind == "anthropic":
        return AnthropicBackend(rest, base_url, timeout)
    if kind == "gemini":
        return GeminiBackend(rest, base_url, timeout)
    if kind in ("openai", "openai-compat"):
        return OpenAICompatBackend(rest, base_url, timeout)
    if kind == "gemini-compat":
        return OpenAICompatBackend(rest, base_url or GEMINI_COMPAT, timeout)
    raise BackendError(f"unknown backend kind: {kind}")
