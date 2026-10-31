#!/usr/bin/env python3
"""End-to-end checks against tests/mock_upstream.py. Requires a fresh database."""
import asyncio
import os
import sqlite3
import time

import httpx

B = os.environ.get("GUARD_URL", "http://127.0.0.1:8317")
TOKEN = os.environ.get("GUARD_TOKEN", "test-token")
DB = os.environ.get("GUARD_DB", "/tmp/guard-test.db")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
ok = bad = 0


def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {name} {extra}")
    else:
        bad += 1
        print(f"  FAIL  {name} {extra}")


def stats():
    return httpx.get(f"{B}/stats", headers=H, timeout=10).json()


print("1) a key without the model (404 model_not_found) fails over, without blacklisting")
r = httpx.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-6-sol", "input": "hi"}, timeout=30)
check("200", r.status_code == 200, f"got {r.status_code}")
check("moved to org2", r.headers.get("x-guard-key") == "org2", r.headers.get("x-guard-key"))
hz = httpx.get(f"{B}/healthz", timeout=10).json()
check("org1 not marked bad", all(not k["bad"] for k in hz["keys"]))

print("2) streaming: SSE passes through, usage read from response.completed")
with httpx.stream("POST", f"{B}/v1/responses", headers=H,
                  json={"model": "gpt-6-sol", "input": "s", "stream": True}, timeout=30) as s:
    body = b"".join(s.iter_bytes())
check("200", s.status_code == 200)
check("SSE intact", b"response.completed" in body)
time.sleep(1)
g1 = {k: v["G1"]["used"] for k, v in stats()["keys"].items()}
check("each key charged 1200", sorted(g1.values()) == [0, 1200, 1200], str(g1))
check("reservations released", all(v["G1"]["reserved"] == 0 for v in stats()["keys"].values()))

print("3) exhausted group -> 429 with retry-after")
r = httpx.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-6-sol", "input": "x"}, timeout=30)
check("429", r.status_code == 429, f"got {r.status_code}")
check("error code", r.json().get("error", {}).get("code") == "free_tier_quota_exhausted")
check("retry-after", "retry-after" in r.headers)

print("4) the other group is independent")
r = httpx.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-5.6-terra", "input": "l"}, timeout=30)
check("200", r.status_code == 200, f"got {r.status_code}")

print("5) streaming chat completions get include_usage injected")
with httpx.stream("POST", f"{B}/v1/chat/completions", headers=H,
                  json={"model": "gpt-5.6-terra", "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]}, timeout=30) as s:
    b = b"".join(s.iter_bytes())
check("200 (upstream 400s without it)", s.status_code == 200, f"got {s.status_code}")
check("usage chunk present", b"usage" in b)

print("6) unknown model -> 403; passthrough model -> 200 and not charged")
check("403", httpx.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-nope", "input": "x"}).status_code == 403)
before = stats()
r = httpx.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-image-2", "input": "x"}, timeout=30)
check("200", r.status_code == 200, f"got {r.status_code}")
check("not charged", before == stats())

print("7) 8 concurrent requests: accurate total, no leaked reservation")
before = sum(v["G2"]["used"] for v in stats()["keys"].values())


async def one(c, i):
    r = await c.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-5.6-terra", "input": f"p{i}"})
    return r.status_code


async def conc():
    async with httpx.AsyncClient(timeout=60) as c:
        return await asyncio.gather(*[one(c, i) for i in range(8)])


codes = asyncio.run(conc())
check("all 200", all(c == 200 for c in codes), str(codes))
time.sleep(1)
st = stats()
check("+9600 tokens", sum(v["G2"]["used"] for v in st["keys"].values()) - before == 9600)
check("reservations released", all(v["G2"]["reserved"] == 0 for v in st["keys"].values()))

print("8) client disconnect releases the reservation")
with httpx.stream("POST", f"{B}/v1/responses", headers=H,
                  json={"model": "gpt-5.6-terra", "input": "a", "stream": True}, timeout=30) as s:
    next(s.iter_bytes())
time.sleep(2)
check("released", all(v["G2"]["reserved"] == 0 for v in stats()["keys"].values()))

print("9) daily rollover: move the server's day rows away, today starts at zero")
day = httpx.get(f"{B}/healthz", timeout=10).json()["day"]
con = sqlite3.connect(DB)
con.execute("UPDATE usage SET day='2000-01-01' WHERE day=?", (day,))
con.commit()
check("today is zero", sum(v["G2"]["used"] for v in stats()["keys"].values()) == 0)
r = httpx.post(f"{B}/v1/responses", headers=H, json={"model": "gpt-6-sol", "input": "d"}, timeout=30)
check("budget available again", r.status_code == 200, f"got {r.status_code}")

print("10) default boundary is UTC (OpenAI resets there, not in local time)")
hz2 = httpx.get(f"{B}/healthz", timeout=10).json()
check("reset_timezone == UTC", hz2.get("reset_timezone") == "UTC", str(hz2.get("reset_timezone")))

print("11) bad token -> 401")
r = httpx.post(f"{B}/v1/responses", headers={**H, "Authorization": "Bearer nope"},
               json={"model": "gpt-6-sol"}, timeout=30)
check("401", r.status_code == 401, f"got {r.status_code}")

print(f"\n{ok} passed / {bad} failed")
raise SystemExit(1 if bad else 0)
