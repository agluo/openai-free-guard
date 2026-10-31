#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Switch a New API channel from talking to OpenAI directly to talking through
this guard, and roll back with one command.

The original channel is *disabled*, never deleted: its keys stay in the
database, so rollback costs nothing.

  python3 newapi_switch.py --dry-run
  python3 newapi_switch.py --apply --proxy-url http://openai-free-guard:8317 --proxy-token xxx
  python3 newapi_switch.py --rollback --apply
"""
import argparse
import json
import os
import sys
import time
import urllib.request

BASE = os.environ["NEWAPI_BASE_URL"].rstrip("/")
TOKEN = os.environ["NEWAPI_ACCESS_TOKEN"]
UID = os.environ.get("NEWAPI_USER_ID", "1")
ORIGIN_ID = int(os.environ.get("ORIGIN_CHANNEL_ID", "1"))
GUARD_NAME = "openai-free-guard"


def api(path, body=None, method=None):
    req = urllib.request.Request(
        BASE + path,
        method=method or ("POST" if body is not None else "GET"),
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + TOKEN, "New-Api-User": UID,
                 "Content-Type": "application/json"},
    )
    raw = urllib.request.urlopen(req, timeout=30).read().decode()
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def all_channels():
    d = api("/api/channel/?p=1&page_size=200")["data"]
    return d.get("items", d) if isinstance(d, dict) else d


def find_guard():
    for c in all_channels():
        if c.get("name") == GUARD_NAME:
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--proxy-url")
    ap.add_argument("--proxy-token")
    a = ap.parse_args()

    if a.rollback:
        g = find_guard()
        if g:
            print(f"delete guard channel id={g['id']}")
            if a.apply:
                api(f"/api/channel/{g['id']}", method="DELETE")
        print(f"re-enable origin channel {ORIGIN_ID}")
        if a.apply:
            print(api(f"/api/channel/{ORIGIN_ID}/status", {"status": 1}))
        return

    origin = api(f"/api/channel/{ORIGIN_ID}")["data"]
    models, group = origin.get("models") or "", origin.get("group") or "default"

    fn = f"backup-channel{ORIGIN_ID}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    print(f"[1] backup origin config -> {fn}")
    if a.apply:
        with open(fn, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.strftime("%FT%T%z"),
                       "channel": {k: origin.get(k) for k in
                                   ("id", "name", "type", "status", "base_url", "models",
                                    "group", "priority", "weight", "model_mapping",
                                    "setting", "settings")}},
                      f, ensure_ascii=False, indent=2)

    if not a.proxy_url or not a.proxy_token:
        if not a.dry_run:
            sys.exit("--proxy-url and --proxy-token are required")

    body = {
        "name": GUARD_NAME, "type": 1,
        "key": a.proxy_token or "DRY_RUN",
        "base_url": (a.proxy_url or "http://127.0.0.1:8317").rstrip("/"),
        "models": models, "group": group,
        "priority": min(int(origin.get("priority") or 0) + 10, 100),
        "weight": 0, "status": 1, "model_mapping": "",
        "setting": json.dumps({"responses_websocket_enabled": False}),
    }
    g = find_guard()
    print(f"[2] {'update' if g else 'create'} channel {GUARD_NAME}: {body['base_url']} priority={body['priority']}")
    if a.apply:
        print("    ", api("/api/channel/", {"mode": "single", "channel": {**body, "id": g["id"]} if g else body}))

    print(f"[3] disable origin channel {ORIGIN_ID} so nothing bypasses the guard")
    if a.apply:
        print("    ", api(f"/api/channel/{ORIGIN_ID}/status", {"status": 2}))


if __name__ == "__main__":
    main()
