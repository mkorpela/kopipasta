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

# The answers wanted here are one sentence, so this looks absurdly generous.
# It is not: on a reasoning model the budget covers reasoning AND answer, and
# 256 was under the floor — gemini-3.7-flash spent 241 of it thinking, left 11
# for the answer, and tripped the MAX_TOKENS guard. That aborted turn 2 and the
# run measured no caching at all, which is the one thing this script exists to
# measure. Output tokens are also the cheap side of the ledger here: the payload
# is ~16k input tokens per turn, so the budget is not what this run costs.
MAX_TOKENS = int(os.environ.get("LIVECHECK_MAX_TOKENS", "2048"))

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


def _have_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def discover() -> List[dict]:
    """Each entry is one arm of the experiment.

    `explicit` means "we asked this provider to cache, and it gave us a
    mechanism to ask with". It drives the verdict: where we placed a
    breakpoint ourselves, a miss is OUR bug and must be reported as a
    failure; where caching is the provider's opportunistic business, a miss
    is a finding about the provider.
    """
    have_claude = shutil.which("claude") is not None
    gem = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
    return [
        {"label": "claude-cli", "spec": f"claude-cli:{os.environ.get('CLAUDE_MODEL', '-')}",
         "have": have_claude, "explicit": False,
         "cred": "claude on PATH" if have_claude else "claude not on PATH"},
        {"label": "anthropic",
         "spec": f"anthropic:{os.environ.get('ANTHROPIC_MODEL', 'claude-opus-5')}",
         "have": bool(os.environ.get("ANTHROPIC_API_KEY")), "explicit": True,
         "cred": "ANTHROPIC_API_KEY"},
        # Two arms, and the pair is the point. The delta between them is the
        # whole §2.9 finding; either number alone is uninterpretable.
        {"label": "gemini", "spec": f"gemini:{gem}",
         "have": _have_gemini(), "explicit": True,
         "cred": "GEMINI_API_KEY or GOOGLE_API_KEY",
         "kwargs": {"cache": True},
         "note": "explicit cachedContents, TTL-bounded"},
        {"label": "gemini-implicit", "spec": f"gemini:{gem}",
         "have": _have_gemini(), "explicit": False,
         "cred": "GEMINI_API_KEY or GOOGLE_API_KEY",
         "kwargs": {"cache": False},
         "note": "control arm: no cachedContents, implicit caching only"},
        {"label": "openai", "spec": f"openai:{os.environ.get('OPENAI_MODEL', 'gpt-5')}",
         "have": bool(os.environ.get("OPENAI_API_KEY")), "explicit": False,
         "cred": "OPENAI_API_KEY"},
    ]


def turn(b, prefix: str, suffix: str) -> Tuple[Optional[backends.Completion], float, str]:
    t0 = time.time()
    try:
        c = b.complete(prefix, suffix, max_tokens=MAX_TOKENS)
    except backends.BackendError as e:
        return None, time.time() - t0, str(e)[:300]
    return c, time.time() - t0, ""


def main(argv: List[str]) -> int:
    only = {a.lower() for a in argv[1:]}
    print(f"\npayload: {len(build_prefix()):,} chars (~{len(build_prefix())//4:,}-"
          f"{len(build_prefix())//2:,} tokens depending on tokenizer)\n")

    ran = failed = 0
    for arm in discover():
        label, spec, explicit = arm["label"], arm["spec"], arm["explicit"]
        if only and label not in only:
            continue
        if not arm["have"]:
            print(f"── {label:<15} SKIPPED — no credential ({arm['cred']})")
            continue

        note = f"   [{arm['note']}]" if arm.get("note") else ""
        print(f"── {label:<15} {spec}{note}")

        # A FRESH prefix per arm, not one shared across the run. Arms hit the
        # same provider, so an earlier arm's traffic warms the implicit cache
        # and the next arm's "cold" turn 1 is anything but — that silently
        # destroyed the control arm the first time this ran. When
        # LIVECHECK_NONCE is set the prefix is pinned on purpose and every arm
        # shares it, which is the whole point of pinning.
        prefix = build_prefix()
        b = backends.build(spec, **arm.get("kwargs", {}))

        # A backend may have rented a cache that bills until its TTL expires.
        # Hand it back even if the experiment blows up halfway.
        try:
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
        finally:
            close = getattr(b, "close", None)
            if callable(close):
                close()
        ran += 1

        why_off = getattr(b, "cache_disabled_reason", "")
        if why_off:
            print(f"   NOTE: explicit cache unavailable, fell back to inline — {why_off}")

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

        # ORDER IS LOAD-BEARING: the two `explicit` FAIL branches must come
        # before ALREADY WARM. They used to sit after it, and ALREADY WARM fires
        # on `not wrote_on_1 and c1.cached_tokens > 0` — conditions a BROKEN
        # explicit adapter satisfies whenever turn 1 happens to catch an
        # implicit hit. The run then printed "Inconclusive", skipped `failed`,
        # and exited 0. Whether this harness noticed a broken cache came down to
        # whether the provider's opportunistic cache warmed turn 1, which is a
        # coin flip measured at 74.3% (§2.9). A check that reports success
        # depending on the weather is not a check.
        if wrote_on_1 and read_on_2 and not rewrote_2:
            print(f"   VERDICT: PREFIX REUSED, NO LAG — turn 1 wrote "
                  f"{c1.cache_creation_tokens:,} tokens; turn 2 read "
                  f"{c2.cached_tokens:,} back and wrote "
                  f"{c2.cache_creation_tokens:,}, seconds later.")
        elif explicit and rewrote_2:
            print(f"   VERDICT: FAIL — we set an explicit cache breakpoint, yet turn 2 "
                  f"re-wrote {c2.cache_creation_tokens:,} tokens. The adapter, not the "
                  f"provider, is the suspect.")
            failed += 1
        elif explicit and not read_on_2:
            # The trap this branch exists for: a provider that reports no
            # cache-creation counter at all makes `wrote_on_1` structurally
            # False, so every earlier branch falls through to "not reused" —
            # and a perfectly working cache is indistinguishable from a broken
            # one. If we asked for caching and turn 2 read nothing back, that
            # is a failure we own, not a fact about the provider.
            print(f"   VERDICT: FAIL — we asked this provider to cache the prefix and "
                  f"turn 2 read {c2.cached_tokens:,} tokens back. Explicit caching is "
                  f"not working; suspect the adapter.")
            failed += 1
        elif not wrote_on_1 and c1.cached_tokens > 0:
            # NB `c1.cached_tokens > 0` is a weak proxy for "warm". On
            # `claude-cli:` it is ALWAYS true — the harness reads ~50% of input
            # from its own system prompt cache on every call — so that arm lands
            # here every run and can never produce a result. Same category error
            # the comment above the flags warns about: those cached tokens are
            # not necessarily OUR prefix. Left as-is because distinguishing them
            # needs a live `claude` run to calibrate against, and there is no
            # measurement to write that code from yet. Documented in §2.9.
            warm = ("expected — LIVECHECK_NONCE pins the prefix across runs"
                    if os.environ.get("LIVECHECK_NONCE")
                    else "unexpected — the per-run nonce should have made turn 1 cold")
            print(f"   VERDICT: ALREADY WARM — turn 1 read {c1.cached_tokens:,} from cache "
                  f"and wrote nothing ({warm}). Inconclusive; compare against a cold run.")
        elif explicit and read_on_2:
            print(f"   VERDICT: PREFIX REUSED — turn 2 served {c2.cached_tokens:,} tokens "
                  f"from the cache we created explicitly. (This provider reports no "
                  f"cache-creation counter on the completion, so turn 1's write shows up "
                  f"only as the cache resource's own size.)")
        elif read_on_2:
            # Same structural blind spot as the `explicit` branch above, and it
            # was only ever guarded there. On a provider with no cache-creation
            # counter `wrote_on_1` is always False, so a control arm that
            # genuinely read the prefix back fell through to the `else` and was
            # reported as "PREFIX NOT REUSED — turn 2 re-wrote 0 tokens" on the
            # same run whose CACHE line said 74.3% served from cache. The two
            # lines contradicted each other and the wrong one was the headline.
            # Report the share, do not characterise it. An earlier draft asserted
            # "it is not all of the prefix" here, which is a Gemini measurement
            # (74.3%) hardcoded into a branch every non-explicit provider reaches
            # — it would be a flat lie on a provider that serves 100%. The one
            # thing that IS true for all of them is that nobody promised this.
            share = c2.cached_tokens / c2.input_tokens * 100 if c2.input_tokens else 0.0
            print(f"   VERDICT: PREFIX REUSED WITHOUT BEING ASKED — turn 2 served "
                  f"{c2.cached_tokens:,} tokens ({share:.1f}%) from the provider's own "
                  f"implicit cache. Real, but unpromised: it is opportunistic, and the "
                  f"identical call misses on other runs. Do not budget on one reading — "
                  f"re-run before quoting a number.")
        else:
            print(f"   VERDICT: PREFIX NOT REUSED — turn 2 read "
                  f"{c2.cached_tokens:,} tokens back and re-wrote "
                  f"{c2.cache_creation_tokens:,} (turn 1 wrote "
                  f"{c1.cache_creation_tokens:,}). Back-to-back calls get no "
                  f"benefit from a stable prefix here. NB: implicit/opportunistic "
                  f"caching is not a promise — if this provider offers an explicit "
                  f"cache, this is an argument for using it.")

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
