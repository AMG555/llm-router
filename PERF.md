# Performance notes

Measurements are medians over 30–80 requests against a local mock upstream
(TCP, `TCP_NODELAY`, keep-alive), comparing a **direct** call to the upstream
API against the **same call through the gateway**. Harness: real `uvicorn`
over a socket (not ASGI in-process, which buffers streaming and distorts TTFT).

## Result (current)

| Scenario | Direct | Via gateway | Overhead |
| --- | --- | --- | --- |
| Non-stream, ~1 ms upstream | 0.94 ms | 4.24 ms | **+3.3 ms** |
| Stream TTFT, ~1 ms upstream | 1.11 ms | 5.76 ms | **+4.7 ms** |
| Non-stream, 250 ms upstream | 251.7 ms | 255.1 ms | **+3.4 ms (1.4%)** |
| Stream TTFT, 250 ms upstream | 251.8 ms | 257.0 ms | **+5.2 ms (2.1%)** |

Interpretation: against a real model (hundreds of ms to seconds) the gateway
adds **~1–2%**. The remainder is the unavoidable second network hop
(client → gateway → provider) plus ~1 ms of request processing. Streaming is
forwarded progressively (first byte is not buffered).

## What was changed (kept)

| Change | Effect |
| --- | --- |
| SQLite `journal_mode=WAL`, `synchronous=NORMAL`, buffered log writes | Removed a per-request fsync/COMMIT (~5 ms → ~0 ms) |
| Raw ASGI middleware instead of Starlette `BaseHTTPMiddleware` for headers | ~1.8 ms off every request; also avoids stream buffering |
| `uvicorn --no-access-log --log-level warning` | Less per-request I/O under load (neutral at low volume) |

## Tried and reverted

| Idea | Result | Verdict |
| --- | --- | --- |
| Cheap regex model-rewrite in SSE passthrough (skip JSON re-encode) | 46 µs → 44 µs per chunk (within noise) | Reverted — no gain, and invalid JSON if a model name contains `"` |

Per-chunk overhead (~44 µs) is dominated by the per-chunk ASGI `send`, not JSON;
on Linux `uvloop` it is lower.

## Operational notes for lowest latency

- Run on Linux where `uvicorn[standard]` uses **uvloop + httptools**.
- Keep **one worker** (rate/cooldown state is in-process; SQLite is single-file).
- Don't place an egress proxy in front unless required for IP allowlisting
  (it adds a hop). Prefer `EGRESS_PROXY` only when you must pin an IP.
- Keep `LOG_REQUESTS` on (now buffered) — it is not on the hot path.
- Free-tier idle sleep adds a cold start to the first request; keep the instance
  warm with an external monitor if that matters.