# openai-free-guard

A tiny reverse proxy that enforces OpenAI's **complimentary daily token quota** (the
"share your traffic with OpenAI, get free tokens" program) and refuses requests once a
budget is used up — instead of letting them silently fall through to paid billing.

It sits between your API gateway (tested with [New API](https://github.com/QuantumNous/new-api))
and OpenAI, and counts tokens per **organization key × model group × UTC day**.

```
client -> New API -> openai-free-guard -> OpenAI
                     (3 keys, per-key daily budgets)
```

## Why

The program gives a fixed number of tokens per day, per organization, in two model
groups. The rule that bites is:

> For each new request, we check against your running total for that day. If a single
> request would make your daily total exceed the token quota, that entire request is
> billed at normal rates.

There is no "stop when free quota is exhausted" switch upstream. If you overshoot, the
whole request is charged at full price — and with long-context agents a single request
can be 100k+ tokens. This proxy is that switch.

## How tokens are counted

```python
tokens = input_tokens + output_tokens
usage[key][group][utc_day] += tokens
```

* `input_tokens` includes **cached input and cache writes**, at face value. The quota is
  denominated in tokens, not dollars; prompt caching changes what you *pay*, not how many
  tokens you *used*. This is the deliberate, conservative choice.
* Only models on OpenAI's official list are metered. Anything else is either rejected
  (`403`) or listed in `passthrough_models` (allowed, not metered — but billed normally).
* Daily reset is implicit: usage rows are keyed by a date string, so a new day simply has
  no rows yet. Nothing is ever cleared, no cron job involved. The boundary is 00:00
  **UTC**, which is exactly when OpenAI resets its quota. `reset_timezone` exists only
  for other providers that roll over elsewhere (Google's free tier uses midnight
  Pacific); leave it alone if you are metering OpenAI.

Before every request the proxy reserves `estimate(input) + min(max_output_tokens, cap)`.
That headroom matters because of the "entire request is billed" rule above — the proxy
would rather return 429 than risk a request that lands on the paid side of the line.

## Behaviour

| Situation | What happens |
|---|---|
| Budget available | forwarded; charged with the real `usage` from the upstream response |
| Current key short | another key is used (budget re-checked on failover) |
| No key has room | `429 free_tier_quota_exhausted` + `Retry-After` (seconds to 00:00 UTC) |
| Key returns 401/403 | cooled down 10 min |
| Key returns 429 | cooled down 90 s, another key retried |
| Key lacks a model (404 `model_not_found`) | that model is remembered for 1 h on that key only |
| Model not on the official list | `403 model_not_covered` |
| UTC midnight | budgets reset automatically |

Both APIs are supported: `/v1/chat/completions` and `/v1/responses`, streaming and not.
For streaming chat completions the proxy injects `stream_options.include_usage` when
missing, otherwise the upstream reports no usage at all. For Responses streaming it reads
usage from the `response.completed` event. Everything else under `/v1/*` is passed
through untouched.

`prompt_cache_key` is used for **session affinity**: a conversation stays on one key so
the provider-side prompt cache stays warm (cached input is ~10x cheaper).

### Reserving vs clamping

Two different mechanisms, often confused:

* **Reservation** (`reserve_mode`, `max_reserve_per_request`) — always on. Before
  sending, the proxy reserves `estimate(input) + min(max_output_tokens, cap)` and
  refuses the request if no key can cover it. The reservation is released the moment
  the response finishes; only real usage is charged.
* **Clamping** (`clamp_max_output`) — off by default. Lowers the output cap *inside the
  request body* so the generation physically cannot exceed what is left. It protects
  against a single long generation walking past the line, at the cost of truncating
  answers. Worth turning on only if your workload produces long outputs.

Neither of them can stop a stream midway: usage is only known from the final
`response.completed` event, so by the time an overshoot is visible the request has
already been made — and per OpenAI's rule an overshooting request is billed in full.

## Endpoints

| Path | Purpose |
|---|---|
| `/healthz` | liveness, current day, reset timezone, seconds to reset, upstream models that are not covered |
| `/stats` | per-key, per-group usage and reservations, plus `top_models_today` (requires the proxy token) |
| everything else | forwarded to `upstream_base` |

## Quick start

```bash
cp config.example.json config.json      # fill in proxy_token and your keys
openssl rand -hex 20                    # <- paste this as proxy_token
bash deploy.sh                          # builds, starts, health-checks, prints the base URL
curl -s http://127.0.0.1:8317/healthz
```

The process refuses to start if `proxy_token` is empty or still looks like a
placeholder, because without it the proxy would accept any caller and `/stats`
would be public.

Then edit the gateway channel that used to talk to OpenAI directly:

| Field | New value |
|---|---|
| Base URL | `http://openai-free-guard:8317` (or `http://127.0.0.1:8317` if not containerised) |
| Key | the `proxy_token` from `config.json` |
| WebSocket | **off** — see Limitations |

Keep the old channel around but **disabled** rather than deleted: it still holds the
original keys, so rolling back is a single status flip if anything misbehaves.

## Configuration

| Key | Meaning |
|---|---|
| `limits` | daily token budget per key per group. Build tier: `250000` / `2500000`; Launch or Grow: `1000000` / `10000000` |
| `model_groups` | model → group. Copy from OpenAI's official list; aliases and dated snapshots both work |
| `passthrough_models` | allowed but unmetered (images, TTS, embeddings) — these are always billed normally |
| `reserve_mode` | `max_output` (strict, reserves the output budget too) or `input_only` |
| `max_reserve_per_request` | caps the output part of the reservation. Lower it if your clients send a huge `max_output_tokens` but produce short answers |
| `session_affinity` / `session_ttl` | keep a conversation on one key to preserve prompt caching |
| `discovery_interval` | seconds between `/v1/models` polls used to surface unknown models (`0` disables) |
| `reset_timezone` | IANA zone for the daily boundary. **Keep `UTC` for OpenAI** — that is where its quota resets. Other values exist only for providers that roll over elsewhere (e.g. Google: `America/Los_Angeles`) |
| `log_level` | `INFO` by default. Lifted to `DEBUG` via `GUARD_LOG_LEVEL` too |
| `notify_url` | optional webhook POSTed `{"title", "body"}` when a budget runs out (works with Bark) |
| `notify_cooldown` | seconds before the same group+model may notify again (default 1800) |
| `allow_insecure` | set `true` only to run without a real `proxy_token`; otherwise startup fails |
| `clamp_max_output` | **off by default.** When on, the outgoing output cap is lowered to what the chosen key can still afford — see below |
| `clamp_min_tokens` | below this headroom the clamp is skipped instead of truncating to something useless (default 256) |

## Tests

`tests/` contains a mock upstream and an end-to-end suite covering failover, quota
exhaustion, SSE usage parsing, reservations under concurrency, and the UTC rollover:

```bash
pip install fastapi uvicorn httpx
python -m uvicorn tests.mock_upstream:app --port 9099 &
GUARD_CONFIG=tests/config.example.json python -m uvicorn app.main:app --port 8317 &
python tests/e2e.py
```

## Limitations

* **No WebSocket.** `/v1/responses` over WebSocket bypasses an HTTP proxy entirely —
  turn that switch off on the gateway channel, or that traffic is invisible to the guard
  and counts as free while it is really being billed.
* Counting is deliberately conservative. If OpenAI turns out not to count cached input
  toward the quota, the guard will stop earlier than strictly necessary. Verify once
  against the "data sharing incentive tier - input tokens" line on your usage dashboard.
* Single process. State is a SQLite file; run one replica per database.

## License

MIT
