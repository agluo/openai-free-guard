#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI free-tier quota guard proxy.

Sits between an API gateway (e.g. New API) and OpenAI. It counts tokens per
"organization key x model group x UTC day" and returns HTTP 429 once a daily
free-tier budget is exhausted, instead of letting the request silently fall
through to paid billing.

Only depends on fastapi / uvicorn / httpx.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

CFG_PATH = os.environ.get("GUARD_CONFIG", "/app/config.json")
with open(CFG_PATH, "r", encoding="utf-8") as _f:
    CFG: dict[str, Any] = json.load(_f)

UPSTREAM = CFG["upstream_base"].rstrip("/")
PROXY_TOKEN = CFG.get("proxy_token") or ""
DB_PATH = CFG.get("db_path", "/data/guard.db")

# ---------------------------------------------------------------- model mapping
# Group membership is hard-coded from OpenAI's official list of models covered
# by the complimentary-tokens program. No name-based guessing: a model that is
# not listed is *never* free, so it is rejected by default.
_MODEL_GROUP: dict[str, str] = {}
for _g, _models in CFG.get("model_groups", {}).items():
    for _m in _models:
        _MODEL_GROUP[_m] = _g

PASSTHROUGH = set(CFG.get("passthrough_models") or CFG.get("unrestricted_models", []))
DENY = "__DENY__"
KEYS: list[dict[str, Any]] = CFG["keys"]

# Models seen upstream that are in neither model_groups nor passthrough_models
NOT_COVERED: dict[str, int] = {}


def classify(model: Optional[str]) -> Optional[str]:
    """Return "G1"/"G2" (metered), None (pass through, unmetered) or DENY."""
    if not model:
        return None
    if model in _MODEL_GROUP:
        return _MODEL_GROUP[model]
    if model in PASSTHROUGH:
        return None
    return DENY


# ------------------------------------------------------------------------- DB
_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS usage(
        key_id TEXT NOT NULL, day TEXT NOT NULL, grp TEXT NOT NULL,
        tokens INTEGER NOT NULL DEFAULT 0, reqs INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(key_id, day, grp));
    CREATE TABLE IF NOT EXISTS keyerror(
        key_id TEXT PRIMARY KEY, bad_until REAL, last_status INTEGER,
        last_msg TEXT, ts REAL);
    """
)
_conn.commit()
_db_lock = threading.Lock()


def utc_day() -> str:
    """The whole reset mechanism lives here: usage is bucketed by UTC date,
    so a new day simply has no rows yet. Nothing is ever cleared."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def seconds_to_reset() -> int:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))


def used_tokens(key_id: str, grp: str) -> int:
    with _db_lock:
        row = _conn.execute(
            "SELECT tokens FROM usage WHERE key_id=? AND day=? AND grp=?",
            (key_id, utc_day(), grp),
        ).fetchone()
    return int(row[0]) if row else 0


def charge(key_id: str, grp: str, tokens: int) -> None:
    with _db_lock:
        _conn.execute(
            """INSERT INTO usage(key_id, day, grp, tokens, reqs) VALUES(?,?,?,?,1)
               ON CONFLICT(key_id, day, grp)
               DO UPDATE SET tokens = tokens + excluded.tokens, reqs = reqs + 1""",
            (key_id, utc_day(), grp, int(tokens)),
        )
        _conn.commit()


def mark_bad(key_id: str, status: int, msg: str, ttl: float = 90.0) -> None:
    with _db_lock:
        _conn.execute(
            """INSERT INTO keyerror(key_id, bad_until, last_status, last_msg, ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(key_id) DO UPDATE SET bad_until=excluded.bad_until,
                 last_status=excluded.last_status, last_msg=excluded.last_msg, ts=excluded.ts""",
            (key_id, time.time() + ttl, status, msg[:400], time.time()),
        )
        _conn.commit()


def bad_until(key_id: str) -> float:
    with _db_lock:
        row = _conn.execute("SELECT bad_until FROM keyerror WHERE key_id=?", (key_id,)).fetchone()
    return float(row[0] or 0) if row else 0.0


# ------------------------------------------------------------------ reservation
# Concurrent requests reserve tokens before their real usage is known, so a burst
# cannot all slip through the same remaining budget at once.
_reserved: dict[tuple[str, str], int] = {}
_res_lock = threading.Lock()


def reserved(key_id: str, grp: str) -> int:
    with _res_lock:
        return _reserved.get((key_id, grp), 0)


def reserve(key_id: str, grp: str, tokens: int) -> None:
    with _res_lock:
        _reserved[(key_id, grp)] = _reserved.get((key_id, grp), 0) + tokens


def release(key_id: str, grp: str, tokens: int) -> None:
    with _res_lock:
        left = _reserved.get((key_id, grp), 0) - tokens
        if left > 0:
            _reserved[(key_id, grp)] = left
        else:
            _reserved.pop((key_id, grp), None)


def remaining(key_id: str, grp: str) -> int:
    return int(CFG["limits"][grp]) - used_tokens(key_id, grp) - reserved(key_id, grp)


# ------------------------------------------------------------------- affinity
# Codex CLI sends prompt_cache_key. Pinning a session to one key keeps the
# provider-side prompt cache warm (cached input is ~10x cheaper).
_sessions: dict[str, tuple[str, float]] = {}
_sess_lock = threading.Lock()
SESSION_TTL = float(CFG.get("session_ttl", 3600))
AFFINITY = bool(CFG.get("session_affinity", True))


def session_of(payload: Optional[dict], request: Request) -> Optional[str]:
    if not AFFINITY or not payload:
        return None
    sid = payload.get("prompt_cache_key") or payload.get("user") or request.headers.get("x-session-id")
    return str(sid) if sid else None


def pinned(sid: Optional[str]) -> Optional[str]:
    if not sid:
        return None
    with _sess_lock:
        rec = _sessions.get(sid)
        if rec and rec[1] > time.time():
            return rec[0]
        _sessions.pop(sid, None)
    return None


def pin(sid: Optional[str], key_id: str) -> None:
    if not sid:
        return
    now = time.time()
    with _sess_lock:
        if len(_sessions) > 20000:
            for k, v in list(_sessions.items()):
                if v[1] < now:
                    _sessions.pop(k, None)
        _sessions[sid] = (key_id, now + SESSION_TTL)


# 404 model_not_found is per (key, model), not per key: remember it briefly.
_model_miss: dict[tuple[str, str], float] = {}
_mm_lock = threading.Lock()
MODEL_MISS_TTL = float(CFG.get("model_miss_ttl", 3600))


def model_ok(key_id: str, model: Optional[str]) -> bool:
    if not model:
        return True
    with _mm_lock:
        until = _model_miss.get((key_id, model))
        if until is None:
            return True
        if until > time.time():
            return False
        _model_miss.pop((key_id, model), None)
    return True


def mark_model_miss(key_id: str, model: Optional[str]) -> None:
    if not model:
        return
    with _mm_lock:
        _model_miss[(key_id, model)] = time.time() + MODEL_MISS_TTL


# ------------------------------------------------------------------ estimation
def estimate_input_tokens(payload: dict) -> int:
    text = json.dumps(payload, ensure_ascii=False)
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    return int(cjk + (len(text) - cjk) / 3.6) + 128


def max_output_of(payload: dict) -> int:
    cap = int(CFG.get("max_reserve_per_request", 200000))
    val = (
        payload.get("max_output_tokens")
        or payload.get("max_tokens")
        or payload.get("max_completion_tokens")
        or CFG.get("default_max_output", 8192)
    )
    try:
        val = int(val)
    except (TypeError, ValueError):
        val = int(CFG.get("default_max_output", 8192))
    return max(1, min(val, cap))


def need_tokens(payload: dict) -> int:
    est = estimate_input_tokens(payload)
    if CFG.get("reserve_mode", "max_output") == "input_only":
        return est + int(CFG.get("margin_tokens", 0))
    return est + max_output_of(payload) + int(CFG.get("margin_tokens", 0))


# -------------------------------------------------------------------- selection
def candidates(grp: Optional[str], exclude: set[str], model: Optional[str] = None,
               need: Optional[int] = None) -> list[dict]:
    now = time.time()
    real = grp or "G1"
    pool = [k for k in KEYS
            if k["id"] not in exclude
            and bad_until(k["id"]) <= now
            and model_ok(k["id"], model)]
    pool.sort(key=lambda k: remaining(k["id"], real), reverse=True)
    if need is not None and grp is not None:
        pool = [k for k in pool if remaining(k["id"], real) >= need]
    return pool


def pick_key(grp: Optional[str], need: int, sid: Optional[str],
             model: Optional[str] = None) -> Optional[dict]:
    real = grp or "G1"
    p = pinned(sid)
    if p and model_ok(p, model) and bad_until(p) <= time.time():
        if grp is None or remaining(p, real) >= need:
            return next((k for k in KEYS if k["id"] == p), None)

    pool = candidates(grp, set(), model, need if grp else None)
    return pool[0] if pool else None


# ------------------------------------------------------------------ responses
def quota_error(model: str, grp: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": f"daily free-tier token quota exhausted for {grp} ({detail}); "
                           f"model={model}. Resets at 00:00 UTC.",
                "type": "rate_limit_error",
                "param": None,
                "code": "free_tier_quota_exhausted",
            }
        },
        headers={"retry-after": str(seconds_to_reset()),
                 "x-quota-reset-at": str(int(time.time()) + seconds_to_reset())},
    )


def oai_error(status: int, message: str, code: str = "proxy_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "invalid_request_error",
                           "param": None, "code": code}},
    )


class SSEUsageScan:
    """Parse usage out of an SSE stream while forwarding it untouched."""

    def __init__(self) -> None:
        self.buf = b""
        self.usage: Optional[tuple[int, int]] = None
        self.out_chars = 0

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        if b"\n" not in self.buf:
            return
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            u = extract_usage(obj)
            if u:
                self.usage = u
            if obj.get("type") == "response.output_text.delta":
                self.out_chars += len(obj.get("delta") or "")


def extract_usage(obj: dict) -> Optional[tuple[int, int]]:
    """Works for both APIs: Responses (input/output_tokens, nested under
    response.completed) and Chat Completions (prompt/completion_tokens)."""
    u = obj.get("usage")
    if not u and isinstance(obj.get("response"), dict):
        u = obj["response"].get("usage")
    if not isinstance(u, dict):
        return None
    inp = u.get("input_tokens", u.get("prompt_tokens"))
    out = u.get("output_tokens", u.get("completion_tokens"))
    if inp is None and out is None:
        return None
    return int(inp or 0), int(out or 0)


# ------------------------------------------------------------------------ app
app = FastAPI(title="free-tier quota guard")
client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=15, read=1800, write=120, pool=30),
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=60),
    follow_redirects=False,
)

HOP = {"host", "authorization", "content-length", "accept-encoding", "connection",
       "keep-alive", "transfer-encoding", "upgrade", "proxy-connection"}

DISCOVERY_INTERVAL = int(CFG.get("discovery_interval", 86400))


async def _discover_loop() -> None:
    """Poll upstream /v1/models (free) to surface models the guard does not know."""
    await asyncio.sleep(8)
    while True:
        for k in KEYS:
            try:
                r = await client.get(f"{UPSTREAM}/v1/models",
                                     headers={"authorization": f"Bearer {k['key']}"},
                                     timeout=20)
                if r.status_code != 200:
                    continue
                for m in r.json().get("data", []):
                    mid = m["id"]
                    if mid not in _MODEL_GROUP and mid not in PASSTHROUGH:
                        NOT_COVERED[mid] = NOT_COVERED.get(mid, 0) + 1
                break
            except Exception:
                continue
        if DISCOVERY_INTERVAL <= 0:
            return
        await asyncio.sleep(DISCOVERY_INTERVAL)


@app.on_event("startup")
async def _startup() -> None:
    if DISCOVERY_INTERVAL > 0:
        asyncio.create_task(_discover_loop())


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "day": utc_day(), "reset_in": seconds_to_reset(),
            "keys": [{"id": k["id"], "label": k.get("label", k["id"]),
                      "bad": bad_until(k["id"]) > time.time()} for k in KEYS],
            "not_in_official_free_list": sorted(NOT_COVERED)}


@app.get("/stats")
async def stats(request: Request) -> Any:
    if PROXY_TOKEN and request.headers.get("authorization") != f"Bearer {PROXY_TOKEN}":
        return oai_error(401, "unauthorized")
    out = {"day": utc_day(), "reset_in": seconds_to_reset(), "limits": CFG["limits"], "keys": {}}
    for k in KEYS:
        out["keys"][k["id"]] = {
            "label": k.get("label", k["id"]),
            "bad": bad_until(k["id"]) > time.time(),
            **{g: {"used": used_tokens(k["id"], g), "reserved": reserved(k["id"], g),
                   "limit": int(CFG["limits"][g])} for g in CFG["limits"]},
        }
    return out


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(full_path: str, request: Request) -> Response:
    if PROXY_TOKEN and request.headers.get("authorization") != f"Bearer {PROXY_TOKEN}":
        return oai_error(401, "invalid proxy token")

    raw = await request.body()
    payload: Optional[dict] = None
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None

    model = (payload or {}).get("model")
    grp = classify(model)

    if grp == DENY:
        return oai_error(
            403,
            f"model '{model}' is not in OpenAI's official complimentary-token list. "
            f"Remove it from the channel, add it to model_groups, or add it to passthrough_models.",
            code="model_not_covered",
        )

    need = need_tokens(payload or {}) if payload else 0
    sid = session_of(payload, request)
    key = pick_key(grp, need, sid, model)
    if key is None:
        ev = ", ".join(f"{k['id']} left {max(0, remaining(k['id'], grp or 'G1')):,}"
                       for k in KEYS) if grp else "no key"
        return quota_error(model or "-", grp or "-", ev)

    if grp and payload:
        reserve(key["id"], grp, need)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["authorization"] = f"Bearer {key['key']}"
    headers["accept-encoding"] = "identity"

    url = f"{UPSTREAM}/{full_path}"
    body = raw
    # Streaming chat completions only report usage when explicitly asked to.
    if payload and payload.get("stream") and full_path.endswith("chat/completions"):
        so = payload.setdefault("stream_options", {})
        if not so.get("include_usage"):
            so["include_usage"] = True
            body = json.dumps(payload, ensure_ascii=False).encode()

    tried: set[str] = set()
    while True:
        tried.add(key["id"])
        req = client.build_request(request.method, url, headers=headers, content=body,
                                   params=request.query_params)
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            if grp and payload:
                release(key["id"], grp, need)
            mark_bad(key["id"], 0, f"{type(exc).__name__}: {exc}", ttl=60)
            nxt = candidates(grp, tried, model, need if grp else None)
            if not nxt:
                return oai_error(502, f"upstream connection failed: {exc}", "upstream_error")
            key = nxt[0]
            headers["authorization"] = f"Bearer {key['key']}"
            if grp and payload:
                reserve(key["id"], grp, need)
            continue

        if resp.status_code >= 400:
            data = await resp.aread()
            await resp.aclose()
            if grp and payload:
                release(key["id"], grp, need)
            status = resp.status_code
            switch = status in (401, 403, 429)
            if status == 404:
                try:
                    code = json.loads(data).get("error", {}).get("code")
                except Exception:
                    code = None
                switch = code in ("model_not_found", "invalid_model")
                if switch:
                    mark_model_miss(key["id"], model)
            if switch and status != 404:
                mark_bad(key["id"], status, data[:300].decode("utf-8", "ignore"),
                         ttl=600 if status in (401, 403) else 90)
            nxt = candidates(grp, tried, model, need if grp else None) if switch else []
            if nxt:
                key = nxt[0]
                headers["authorization"] = f"Bearer {key['key']}"
                if grp and payload:
                    reserve(key["id"], grp, need)
                continue
            return Response(content=data, status_code=status,
                            media_type=resp.headers.get("content-type") or "application/json")
        break

    pin(sid, key["id"])
    ctype = resp.headers.get("content-type", "")
    is_sse = ctype.startswith("text/event-stream") or bool(payload and payload.get("stream"))

    if not is_sse:
        data = await resp.aread()
        await resp.aclose()
        tokens = None
        if data:
            try:
                tokens = extract_usage(json.loads(data))
            except Exception:
                tokens = None
        if grp and payload:
            release(key["id"], grp, need)
            used = sum(tokens) if tokens else need
            if used:
                charge(key["id"], grp, used)
        return Response(content=data, status_code=resp.status_code,
                        media_type=ctype or "application/json",
                        headers={"x-guard-key": key["id"]})

    scan = SSEUsageScan()

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                scan.feed(chunk)
                yield chunk
        finally:
            await resp.aclose()
            if grp and payload:
                release(key["id"], grp, need)
                if scan.usage:
                    charge(key["id"], grp, sum(scan.usage))
                else:
                    charge(key["id"], grp, estimate_input_tokens(payload) + scan.out_chars // 3)

    return StreamingResponse(body_iter(), status_code=resp.status_code,
                             media_type=ctype or "text/event-stream",
                             headers={"x-guard-key": key["id"]})
