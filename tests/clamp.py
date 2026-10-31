#!/usr/bin/env python3
"""clamp_max_output: the outgoing output cap must never exceed what is left."""
import os

import httpx

B = os.environ.get("CLAMP_URL", "http://127.0.0.1:8319")
H = {"Authorization": "Bearer clamp-test-token", "Content-Type": "application/json"}
LIMIT = 60000
ok = bad = 0


def check(n, c, e=""):
    global ok, bad
    if c:
        ok += 1
        print(f"  PASS  {n} {e}")
    else:
        bad += 1
        print(f"  FAIL  {n} {e}")


print("1) client asks for 200k output, key can afford much less -> clamped down")
r = httpx.post(f"{B}/v1/responses", headers=H,
               json={"model": "gpt-6-sol", "input": "hi", "max_output_tokens": 200000},
               timeout=30)
check("200", r.status_code == 200, f"got {r.status_code}")
seen = (r.json() or {}).get("_seen", {})
capped = seen.get("max_output_tokens")
print(f"     upstream received max_output_tokens = {capped}")
check("was clamped", isinstance(capped, int) and capped < 200000, str(capped))
check("fits the budget", isinstance(capped, int) and capped <= LIMIT, str(capped))

print("2) a modest client cap passes through untouched")
r = httpx.post(f"{B}/v1/responses", headers=H,
               json={"model": "gpt-6-sol", "input": "hi", "max_output_tokens": 128},
               timeout=30)
seen = (r.json() or {}).get("_seen", {})
check("128 kept", seen.get("max_output_tokens") == 128, str(seen.get("max_output_tokens")))

print("3) chat completions: clamps max_completion_tokens instead")
r = httpx.post(f"{B}/v1/chat/completions", headers=H,
               json={"model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
                     "max_completion_tokens": 999999}, timeout=30)
seen = (r.json() or {}).get("_seen", {})
capped = seen.get("max_completion_tokens")
print(f"     upstream received max_completion_tokens = {capped}")
check("clamped", isinstance(capped, int) and capped < 999999, str(capped))

print(f"\n{ok} passed / {bad} failed")
raise SystemExit(1 if bad else 0)
