---
title: LLM Router
emoji: 🛰️
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# LLM Router

A self-hosted, OpenAI-compatible gateway. Point it at one or more providers,
add as many keys as you like, define **virtual model** names of your own, and
your applications talk to a single URL, a single key and a single model name.

Behind the scenes every request is scored against live per-key RPM/TPM windows,
per-key cooldowns and concurrency caps, then routed with automatic failover —
before the first byte is streamed.

> **Compliance.** This gateway pools keys **you control**. Aggregating keys you
> are authorised to use (separate projects, teams or organisations) for
> reliability, cost control and a unified endpoint is the intended use. Using it
> to evade a provider's per-account rate limits, or to run many accounts of the
> same provider to farm free quota, may breach that provider's terms and can get
> accounts banned. Respect each provider's terms.

---

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API](#api)
- [Admin console](#admin-console)
- [Security model](#security-model)
- [Testing](#testing)
- [Deployment](#deployment)
  - [Docker](#docker)
  - [Docker Compose](#docker-compose)
  - [Cloudflare Tunnel (optional)](#cloudflare-tunnel-optional)
  - [Render (free) + UptimeRobot keep-alive](#render-free--uptimerobot-keep-alive)
  - [Hugging Face Spaces](#hugging-face-spaces)
- [Request lifecycle](#request-lifecycle)
- [Limitations](#limitations)

---

## Features

- **One endpoint, many providers.** OpenAI-compatible `/v1` surface. Anthropic
  (`/v1/messages`) and Google Gemini (`:generateContent`) are first-class and
  translated both ways, including SSE streaming.
- **Unlimited keys per provider.** Keys are pooled; add as many as you need.
- **Per-key rate governor.** Sliding 60s RPM/TPM windows plus concurrency caps.
  A key at budget is skipped, never queued.
- **Correct cooldowns.** `Retry-After` and `x-ratelimit-reset-*` are honoured and
  parsed (`ms`/`s`/`m`). Cooldowns are per key, so one key's limit never blocks
  its siblings.
- **Failover before first byte.** Status is inspected before the body is streamed,
  so a 429/5xx/timeout on one key transparently retries on the next.
- **Routing strategies.** `balanced` (priority tiering + weighted round robin),
  `priority`, `round_robin`, `least_latency`, `random`, with optional sticky
  sessions for prompt-cache affinity.
- **Tool calling and multimodal passthrough** preserved end to end.
- **Operator console.** Live key health, per-key cooldown timers, traffic log,
  config import/export, all behind an admin token.

---

## Quick start

### Local

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
export SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
uvicorn app.main:app --port 7860
```

Open <http://localhost:7860>, paste the admin token, add a provider and a key,
create a virtual model with a target, then call it:

```bash
curl http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer <gateway key from Settings>" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast-chat","messages":[{"role":"user","content":"hi"}]}'
```

### Docker

```bash
docker build -t llm-router .
docker run -d --name router -p 7860:7860 \
  -e ADMIN_TOKEN=change-me -e SECRET_KEY=change-me \
  -v "$PWD/data:/data" llm-router
```

---

## Configuration

All configuration is environment variables (see `.env.example`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_TOKEN` | auto-generated | Console/admin auth. Generated and printed if unset. |
| `SECRET_KEY` | — | Enables AES-256-GCM encryption of stored keys/headers at rest. |
| `ENV` | `production` | `production` keeps docs off and defaults strict. |
| `DB_PATH` | `./data/router.db` | SQLite location (persist this). |
| `REQUIRE_GATEWAY_AUTH` | `true` | Require a key on `/v1`. |
| `GATEWAY_RPM` | `0` | Per gateway-key requests/minute (0 = unlimited). |
| `MAX_BODY_BYTES` | `2097152` | Reject oversized request bodies with 413. |
| `CORS_ORIGINS` | *(empty)* | Comma list; empty means same-origin only. |
| `TRUSTED_HOSTS` | `*` | Allowed `Host` header values. |
| `ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc`, `/openapi.json`. |
| `FORCE_HTTPS` | `false` | Always emit HSTS. |
| `ADMIN_MAX_FAILS` / `ADMIN_LOCK_SECONDS` | `10` / `300` | Admin brute-force lockout. |
| `BLOCK_PRIVATE_UPSTREAMS` | `false` | Reject upstreams in private/loopback ranges. |
| `MAX_ATTEMPTS` | `8` | Max upstream attempts per request. |
| `DEFAULT_COOLDOWN` / `SHORT_COOLDOWN` | `20` / `5` | Cooldown seconds. |
| `REQUEST_TIMEOUT` / `CONNECT_TIMEOUT` | `600` / `15` | Upstream timeouts. |
| `BOOTSTRAP_CONFIG` | — | base64 JSON imported on first boot (ephemeral hosts). |

---

## API

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/health`, `/healthz` | none | Liveness. |
| GET | `/readyz` | none | Readiness (DB reachable). |
| GET | `/v1/models` | gateway | List virtual models. |
| POST | `/v1/chat/completions` | gateway | Chat (streaming supported). |
| POST | `/v1/completions` | gateway | Legacy completions. |
| POST | `/v1/embeddings` | gateway | Embeddings passthrough. |
| GET | `/admin/api/*` | admin | Console backend. |

Responses carry `x-router-provider`, `x-router-key`, `x-router-model` and
`x-router-attempts` so you can see exactly which key served each request.

**Escape hatch.** Request `"model": "<provider-name>:<upstream-model>"` to route
straight at a provider's whole key pool without defining a virtual model.

---

## Admin console

Served at `/`. The token field stores your admin token in the browser only.
Everything dynamic is HTML-escaped and rendered without inline handlers, so the
strict `script-src 'self'` Content-Security-Policy holds.

---

## Security model

- Admin surface is **never** unauthenticated: a token is generated if unset.
- Constant-time token comparisons; per-IP admin lockout after repeated failures.
- **Column allow-list on every write**, closing SQL injection via column names.
- Optional **AES-256-GCM at-rest encryption** for upstream keys and headers.
- Same-origin by default (no wildcard CORS), plus HSTS, `nosniff`, `DENY`
  framing, `no-referrer`, and a restrictive CSP.
- Request body size cap; docs/OpenAPI off in production; config export masks
  secrets unless `reveal=1` is explicitly requested.
- Upstream `base_url` validation; optional private-range blocking (SSRF guard).
- Secrets are never logged; keys are masked everywhere except a deliberate,
  admin-authenticated export.

---

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q              # full suite
python -m pytest --cov=app       # with coverage
```

The suite runs against a local mock upstream (no network, no real keys) and
covers the rate governor, adapters (OpenAI/Anthropic/Gemini incl. SSE),
deterministic failover, weighted A/B routing, config portability and the
security controls. CI runs it on Python 3.11–3.13 (`.github/workflows/ci.yml`).

---

## Deployment

### Docker

The image runs as a non-root user, ships a `HEALTHCHECK` and honours
`--proxy-headers` for correct HTTPS/HSTS detection behind a proxy. Mount a
volume at `/data` to persist SQLite.

### Docker Compose

```bash
cp .env.example .env      # set ADMIN_TOKEN and SECRET_KEY
docker compose up -d
```

### Cloudflare Tunnel (optional)

**When you need it:** only for **self-hosted** deployments that must not open
inbound ports (home/lab servers, NAT, no public IP), or when you want
Cloudflare's WAF, DDoS protection and Zero Trust Access in front of the gateway.

**When you don't:** managed hosts (Render, Fly, Cloud Run, Hugging Face Spaces)
already terminate TLS and expose an HTTPS URL — a tunnel adds nothing there and
an extra hop of latency.

To enable it, create a tunnel in Cloudflare Zero Trust (**Networks → Tunnels**),
copy its token into `TUNNEL_TOKEN`, and start the profile:

```bash
docker compose --profile tunnel up -d
```

With the tunnel you can drop the published `7860` port. A named-tunnel config
example lives at `deploy/cloudflared/config.example.yml`.

### Render (free) + UptimeRobot keep-alive

1. Push to GitHub, then Render → **New → Blueprint** (uses `render.yaml`).
2. Set `ADMIN_TOKEN` and `SECRET_KEY` (Render can generate them).
3. **Free instances sleep after ~15 minutes idle.** An in-process self-ping
   cannot wake a sleeping service, so use an **external** monitor:
   - Create a free UptimeRobot **HTTP(s)** monitor.
   - URL: `https://<your-app>.onrender.com/health` (or `/healthz`).
   - Interval: **5 minutes** (free plan supports 5-minute checks).
   - Expected: HTTP 200, keyword `ok`.

   The 5-minute cadence keeps the instance warm through the 15-minute idle
   window, so cold starts become rare.

### Hugging Face Spaces

Docker Spaces require a paid plan; **Static** Spaces are free but cannot run this
server. If you do host it on a Docker Space:

1. New Space → SDK **Docker** → Blank; upload the repo.
2. Space Settings → *Variables and secrets* → set `ADMIN_TOKEN`, `SECRET_KEY`.
3. The filesystem is ephemeral — export your config, base64 it, and store it as
   `BOOTSTRAP_CONFIG` so the router rebuilds itself on every cold boot.

---

## Request lifecycle

```
client ──▶ /v1/chat/completions
             │  gateway auth + rate limit
             ▼
        resolve virtual model ──▶ candidate list
             │  order by strategy (sticky sessions honoured)
             ▼
        skip keys in cooldown / at RPM, TPM or concurrency budget
             ▼
        try candidate ──▶ upstream (headers only)
             │  429/401/5xx/timeout ──▶ per-key cooldown, next candidate
             │  client 4xx ──▶ pass through unchanged
             ▼
        2xx ──▶ stream or return, record usage, update health
```

---

## Limitations

- **Failover only happens before the first byte.** Once an upstream starts
  streaming, mid-stream errors cannot be re-routed.
- If **every** key is limited, the gateway returns a `429` (`no_capacity`);
  "never any lag, ever" is not physically possible.
- Sticky-session hashing uses the supplied session id; without one, requests
  rotate freely.
- SQLite is single-file state — fine for one instance. For multiple replicas,
  move state to a shared database and a shared rate-limit store.

---

## License

MIT — see `LICENSE`.
