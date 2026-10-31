#!/usr/bin/env python3
"""The daily boundary must follow reset_timezone, not UTC."""
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

B = os.environ.get("TZ_URL", "http://127.0.0.1:8320")
TOKEN = os.environ.get("TZ_TOKEN", "tz-test-token")
TZ = os.environ.get("EXPECT_TZ", "Asia/Shanghai")

hz = httpx.get(f"{B}/healthz", timeout=10).json()
zone = ZoneInfo(TZ)
now = datetime.now(zone)
nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
expected = max(1, int((nxt - now).total_seconds()))

ok = True
print(f"  server reset_timezone = {hz.get('reset_timezone')}  (expect {TZ})")
if hz.get("reset_timezone") != TZ:
    ok = False
print(f"  server day            = {hz.get('day')}  (expect {now:%Y-%m-%d})")
if hz.get("day") != now.strftime("%Y-%m-%d"):
    ok = False
print(f"  server reset_in       = {hz.get('reset_in')}s  (expect ~{expected}s)")
if abs(int(hz.get("reset_in", 0)) - expected) > 120:
    ok = False

utc = datetime.now(ZoneInfo("UTC"))
utc_next = (utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
utc_reset = int((utc_next - utc).total_seconds())
print(f"  (a UTC-based implementation would report {utc_reset}s)")

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
