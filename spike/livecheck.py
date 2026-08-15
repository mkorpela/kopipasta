#!/usr/bin/env python3
"""Live-verify the backends against real providers — especially prompt caching.

The spec's central economic claim is "turn 1 pays for the repo, turns 2..n pay
for a question." Everything else in the backend layer is verified against a
mock; that claim cannot be. This runs it for real.

Method: send the SAME prefix twice with DIFFERENT suffixes, and check whether
turn 2 reports cache reads. Reports real cost for both turns.

    uv run python spike/livecheck.py                  # every provider with a key
    uv run python spike/livecheck.py anthropic gemini # only these

Providers without credentials are skipped loudly, never silently. Costs real
money on providers that have keys — the payload is ~20k tokens x 2 turns.

Model overrides: ANTHROPIC_MODEL, GEMINI_MODEL, OPENAI_MODEL.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backends  # noqa: E402

TARGET_CHARS = 60_000

SUFFIX_A = "## Task\nIn one short sentence: what is this project for?"
SUFFIX_B = "## Task\nIn one short sentence: which file applies patches to disk?"


def build_prefix() -> str:
    """A realistic repo payload — big enough to clear every provider's cacheable minimum.

    Carries a per-run nonce. Both turns of a run share it, so turn 2 can reuse
    turn 1's cache entry; a later run gets a different one, so turn 1 is always
    COLD. Without this the second run of the day reads a warm cache on turn 1
    and the experiment silently measures nothing.
    """
    # LIVECHECK_NONCE pins the prefix across separate runs, to test whether a
    # cache entry written by an earlier invocation becomes readable later.
    nonce = os.environ.get("LIVECHECK_NONCE") or f"{time.time_ns():x}{uuid.uuid4().hex[:8]}"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "kopipasta")
    parts = [f"<!-- livecheck run {nonce} -->", "# Project Overview", "", "## File Contents", ""]
    total = 0
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(src, name)
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read()
        if total + len(body) > TARGET_CHARS:
            continue
        parts += [f"# FILE: kopipasta/{name}", "```python", body, "```", ""]
        total += len(body)
    return "\n".join(parts)


# (label, backend spec, credential present?, caching is explicit-and-ours?)
def discover() -> List[Tuple[str, str, bool, bool, str]]:
    return [
        ("claude-cli", f"claude-cli:{os.environ.get('CLAUDE_MODEL', '-')}",
         shutil.which("claude") is not None, False,
         "claude on PATH" if shutil.which("claude") else "claude not on PATH"),
        ("anthropic", f"anthropic:{os.environ.get('ANTHROPIC_MODEL', 'claude-opus-5')}",
         bool(os.environ.get("ANTHROPIC_API_KEY")), True, "ANTHROPIC_API_KEY"),
        ("gemini", f"gemini:{os.environ.get('GEMINI_MODEL', 'gemini-3.7-flash')}",
         bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")), False,
         "GEMINI_API_KEY or GOOGLE_API_KEY"),
        ("openai", f"openai:{os.environ.get('OPENAI_MODEL', 'gpt-5')}",
         bool(os.environ.get("OPENAI_API_KEY")), False, "OPENAI_API_KEY"),
    ]


def turn(b, prefix: str, suffix: str) -> Tuple[Optional[backends.Completion], float, str]:
    t0 = time.time()
    try:
        c = b.complete(prefix, suffix, max_tokens=256)
    except backends.BackendError as e:
        return None, time.time() - t0, str(e)[:300]
    return c, time.time() - t0, ""


def main(argv: List[str]) -> int:
    only = {a.lower() for a in argv[1:]}
    prefix = build_prefix()
    print(f"\npayload: {len(prefix):,} chars (~{len(prefix)//4:,}-{len(prefix)//2:,} tokens "
          f"depending on tokenizer)\n")

    ran = failed = 0
    for label, spec, have, explicit, credname in discover():
        if only and label not in only:
            continue
        if not have:
            print(f"── {label:<11} SKIPPED — no credential ({credname})")
            continue

        print(f"── {label:<11} {spec}")
        b = backends.build(spec)

        c1, dt1, err = turn(b, prefix, SUFFIX_A)
        if err:
            print(f"   turn 1 ERROR: {err}\n")
            failed += 1
            continue
        c2, dt2, err = turn(b, prefix, SUFFIX_B)
        if err:
            print(f"   turn 2 ERROR: {err}\n")
            failed += 1
            continue
        ran += 1

        for n, c, dt in ((1, c1, dt1), (2, c2, dt2)):
            cost = f"  ${c.cost_usd:.4f}" if c.cost_usd else ""
            print(f"   turn {n}: in={c.input_tokens:<8} cached={c.cached_tokens:<8} "
                  f"created={c.cache_creation_tokens:<8} out={c.output_tokens:<5} "
                  f"{dt:5.1f}s{cost}")

        # `cached_tokens > 0` is NOT evidence our prefix was reused — a harness
        # backend caches its own system prompt, so that number is nonzero on
        # turn 1 too. The real question is whether turn 2 had to WRITE the
        # prefix into the cache again. If cache_creation stays flat, we paid
        # for the repo twice and the multi-turn economics do not hold.
        wrote_on_1 = c1.cache_creation_tokens > 0
        read_on_2 = c2.cached_tokens > 0
        rewrote_2 = c2.cache_creation_tokens > c1.cache_creation_tokens * 0.5

        if wrote_on_1 and read_on_2 and not rewrote_2:
            print(f"   VERDICT: PREFIX REUSED, NO LAG — turn 1 wrote "
                  f"{c1.cache_creation_tokens:,} tokens; turn 2 read "
                  f"{c2.cached_tokens:,} back and wrote "
                  f"{c2.cache_creation_tokens:,}, seconds later.")
        elif not wrote_on_1 and c1.cached_tokens > 0:
            warm = ("expected — LIVECHECK_NONCE pins the prefix across runs"
                    if os.environ.get("LIVECHECK_NONCE")
                    else "unexpected — the per-run nonce should have made turn 1 cold")
            print(f"   VERDICT: ALREADY WARM — turn 1 read {c1.cached_tokens:,} from cache "
                  f"and wrote nothing ({warm}). Inconclusive; compare against a cold run.")
        elif explicit and rewrote_2:
            print(f"   VERDICT: FAIL — we set an explicit cache breakpoint, yet turn 2 "
                  f"re-wrote {c2.cache_creation_tokens:,} tokens. The adapter, not the "
                  f"provider, is the suspect.")
            failed += 1
        else:
            print(f"   VERDICT: PREFIX NOT REUSED — turn 2 re-wrote "
                  f"{c2.cache_creation_tokens:,} tokens (turn 1: "
                  f"{c1.cache_creation_tokens:,}). Back-to-back calls get no "
                  f"benefit from a stable prefix here.")

        # Cost is the headline where the provider reports it; where it does not
        # (raw APIs return tokens only), cache share is the honest proxy.
        if c1.cost_usd and c2.cost_usd:
            drop = (1 - c2.cost_usd / c1.cost_usd) * 100
            label = "no saving" if abs(drop) < 10 else f"{drop:+.0f}%"
            print(f"   COST:    ${c1.cost_usd:.4f} -> ${c2.cost_usd:.4f}  ({label})")
        share = (c2.cached_tokens / c2.input_tokens * 100) if c2.input_tokens else 0.0
        print(f"   CACHE:   turn 2 served {c2.cached_tokens:,}/{c2.input_tokens:,} "
              f"input tokens from cache ({share:.1f}%)")
        print()

    if not ran:
        print("Nothing ran. Set at least one provider key, or ensure `claude` is on PATH.\n")
        return 2
    print(f"{ran} provider(s) exercised, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
