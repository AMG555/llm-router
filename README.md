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
  - [Container image](#container-image)
  - [Docker Compose](#docker-compose)
  - [Running behind a reverse proxy or tunnel (optional)](#running-behind-a-reverse-proxy-or-tunnel-optional)
  - [Managed / PaaS platforms](#managed--paas-platforms)
  - [Keeping an idle-sleeping instance warm](#keeping-an-idle-sleeping-instance-warm)
  - [Ephemeral filesystems](#ephemeral-filesystems)
- [Operations](#operations)
- [Request lifecycle](#request-lifecycle)
- [Project layout](#project-layout)
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

### Container

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
| `PORT` | `7860` | Listen port. |
| `DB_PATH` | `./data/router.db` | SQLite location (persist this). |
| `REQUIRE_GATEWAY_AUTH` | `true` | Require a key on `/v1`. |
| `GATEWAY_RPM` | `0` | Per gateway-key requests/minute (0 = unlimited). |
| `GATEWAY_KEY` | — | Fixed client key registered at boot (stable across restarts). |
| `MAX_BODY_BYTES` | `2097152` | Reject oversized request bodies with 413. |
| `CORS_ORIGINS` | *(empty)* | Comma list; empty means same-origin only. |
| `TRUSTED_HOSTS` | `*` | Allowed `Host` header values. |
| `ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc`, `/openapi.json`. |
| `FORCE_HTTPS` | `false` | Always emit HSTS. |
| `ADMIN_MAX_FAILS` / `ADMIN_LOCK_SECONDS` | `10` / `300` | Admin brute-force lockout. |
| `BLOCK_PRIVATE_UPSTREAMS` | `false` | Reject upstreams in private/loopback ranges. |
| `EGRESS_PROXY` | — | Force upstream calls through a fixed-IP proxy (for IP allowlisting). |
| `EGRESS_CHECK_URL` | — | IP-echo URL used by `/admin/api/egress?check=1`. |
| `MAX_ATTEMPTS` | `8` | Max upstream attempts per request. |
| `DEFAULT_COOLDOWN` / `SHORT_COOLDOWN` | `20` / `5` | Cooldown seconds. |
| `REQUEST_TIMEOUT` / `CONNECT_TIMEOUT` | `600` / `15` | Upstream timeouts. |
| `LOG_REQUESTS` / `MAX_LOG_ROWS` | `true` / `5000` | Traffic log and retention. |
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
| GET | `/admin/api/egress` | admin | Report the outbound IP / proxy configuration. |
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

Operational guidance:

- Always set `ADMIN_TOKEN` and `SECRET_KEY` to long random values and keep them
  out of version control.
- Terminate TLS in front of the router (your platform or a reverse proxy) and
  run with `--proxy-headers` so HSTS is emitted correctly.
- Back up the SQLite file (or take a config export) before upgrades.

---

## Performance

The gateway adds minimal latency and streams progressively (first byte is not
buffered). Measured against a local mock upstream through real `uvicorn`:

| Scenario | Overhead vs direct |
| --- | --- |
| Non-stream, 250 ms upstream | **+3.4 ms (1.4%)** |
| Stream TTFT, 250 ms upstream | **+5.2 ms (2.1%)** |

The balance is the unavoidable second network hop plus ~1 ms of processing.
Hot-path choices: WAL + buffered logging (no per-request fsync), a raw ASGI
middleware instead of Starlette's `BaseHTTPMiddleware`, and `--no-access-log`.
Full methodology, including a reverted experiment, is in [`PERF.md`](PERF.md).

For the lowest latency, run on Linux (uvloop/httptools), keep one worker, and
avoid an egress proxy unless you need IP pinning.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q              # full suite
python -m pytest --cov=app       # with coverage
pip-audit -r requirements.txt    # dependency vulnerability scan
```

The suite runs against a local mock upstream (no network, no real keys) and
covers the rate governor, adapters (OpenAI/Anthropic/Gemini incl. SSE),
deterministic failover, weighted A/B routing, config portability and the
security controls. CI runs it on Python 3.11–3.13 (`.github/workflows/ci.yml`).

---

## Deployment

The service is a single container that listens on `PORT` (default `7860`) and
stores state in one SQLite file. Any environment that can run a container or a
Python process can host it. Nothing below depends on a particular vendor.

### Container image

The image runs as a non-root user, ships a `HEALTHCHECK` and honours
`--proxy-headers` so HTTPS/HSTS detection works behind a proxy. Mount a volume
at `/data` to persist SQLite:

```bash
docker build -t llm-router .
docker run -d --name router -p 7860:7860 \
  -e ADMIN_TOKEN=… -e SECRET_KEY=… \
  -v llm-router-data:/data llm-router
```

Health probe endpoints: `/health` (liveness) and `/readyz` (readiness).

### Docker Compose

```bash
cp .env.example .env      # set ADMIN_TOKEN and SECRET_KEY
docker compose up -d
```

### Running behind a reverse proxy or tunnel (optional)

Put TLS and a hostname in front of the router using whatever you already run —
nginx/Caddy/Traefik, a load balancer, a platform ingress, or a Cloudflare Tunnel.
Then start the app with `--proxy-headers --forwarded-allow-ips=*` so it trusts
`X-Forwarded-Proto` and emits HSTS.

A **Cloudflare Tunnel** is only needed for **self-hosted** deployments that must
not open inbound ports (NAT, no public IP, home/lab servers) or when you want
Cloudflare's WAF, DDoS protection and Zero Trust Access in front. It is *not*
needed where the platform already terminates TLS. To enable it, put the tunnel
token in `.env` and start the profile:

```dotenv
TUNNEL_TOKEN=…        # Zero Trust → Networks → Tunnels → create → connector token
```

```bash
docker compose --profile tunnel up -d --build
```

Then add a public hostname in the tunnel dashboard pointing at HTTP
`router:7860`, and (optionally) drop the router's published `7860` port. Full
runbook, named-tunnel and no-compose options, Access policy and troubleshooting:
 **[`deploy/cloudflared/README.md`](deploy/cloudflared/README.md)**.

### Managed / PaaS platforms

When deploying to a platform that builds from source, use a **Dockerfile-based**
builder if one is offered (the repo includes a `Dockerfile`); otherwise use a
Python buildpack with:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips=* --no-access-log --log-level warning`
- Health check path: `/health`

Set `ADMIN_TOKEN`, `SECRET_KEY` and (if relevant) `ENV=production` as platform
secrets. Expose the assigned port via the platform's routing — do not hard-code
a hostname anywhere in config.

The repo includes a `.python-version` file pinning **Python 3.12**. Run on
Python **3.10–3.13**; Python 3.14 is not yet supported by the pinned
dependency set (no prebuilt `pydantic-core` wheel), so builds on 3.14 fall back
to a source build and fail.

A `render.yaml` blueprint is included so a source-connected platform can
provision the service (build/start/health + generated secrets) without manual
setup.

### Keeping an idle-sleeping instance warm

Many free tiers stop (sleep) an instance after a period of inactivity, which
adds a cold start to the next request. An in-process self-ping cannot wake a
sleeping process, so use an **external** uptime monitor:

- Point an HTTP(S) monitor at `https://<your-host>/health` (or `/healthz`).
- Use a check interval shorter than the platform's idle window (e.g. every
  **5 minutes**).
- Expected response: HTTP 200 with body containing `ok`.

Any external monitor works (for example UptimeRobot or a scheduled job on
another host). This keeps the instance warm; it does not change the tier's
limits.

### Ephemeral filesystems

On platforms where the filesystem is wiped on restart, the SQLite file is not
persistent. Keep configuration portable instead:

```bash
# after exporting config from the console (Settings → Export JSON)
base64 -w0 llm-router-config.json
```

Store the output as `BOOTSTRAP_CONFIG`. On every cold boot the router rebuilds
itself from that value. For durable storage, mount a volume, use a managed
disk, or point `DB_PATH` at a persistent location.

Also set `GATEWAY_KEY` to a fixed value so your applications keep the same key
across restarts (otherwise a fresh one is generated on each cold boot).

---
## Operations

- **Health**: `GET /health` for liveness, `GET /readyz` for readiness — wire
  these into your platform's health checks and your uptime monitor.
- **Logs**: the console's *Live traffic* view streams routing events over SSE.
  The service also logs to stdout (safe for container log collectors).
- **Backups**: the whole state is `DB_PATH`. Copy the file, or export config
  JSON from the console.
- **Key rotation**: add the new key, verify traffic, then remove the old one;
  cooldowns are per key so rotation does not disturb sibling keys.
- **Reset**: `POST /admin/api/keys/{id}/reset` clears a key's cooldown and
  windows after you have fixed the underlying problem.

### Static / allowlisted egress IP

Some providers restrict a key to specific source IPs. The gateway's outbound
identity is whatever the host's network gives it, so you make it **stable and
allowlisted** rather than rotating it:

1. **Discover the IP.** `GET /admin/api/egress` returns the local outbound IP
   and the active proxy configuration. With `EGRESS_CHECK_URL` set (a URL that
   echoes your public IP), `GET /admin/api/egress?check=1` also reports the
   observed public IP. Allowlist that at the provider.
2. **Pin it globally.** Set `EGRESS_PROXY` to a proxy with a fixed IP; every
   upstream call leaves through it. If unset, standard `HTTP_PROXY` /
   `HTTPS_PROXY` / `NO_PROXY` env vars are honoured. On cloud platforms this is
   typically a NAT gateway / static-IP add-on — the app itself cannot change the
   datacentre's egress IP.
3. **Pin it per provider.** Give a provider its own `proxy` (console field:
   *Egress proxy*, or `POST /admin/api/providers` with `proxy`). Use this when
   different providers require different allowlisted IPs.
4. **Cap the single IP.** Set `EGRESS_IP_RPM` to a value **below** your
   provider's per-IP limit. The gateway then throttles requests on each egress
   identity (skipping to a wait/429) so you *stay under* the provider's per-IP
   limit instead of tripping a ban. `GET /admin/api/egress` reports the current
   per-egress usage for the last minute.
5. Per-provider proxies are cached per proxy URL, so calls reuse connections.

> **Note.** This is for allowlisting, compliance and *staying within* limits.
> Do **not** rotate source IPs to sidestep a provider's per-IP rate limits —
> that is evasion, breaches provider terms, and rarely works because providers
> correlate abuse by account/key, not only by IP. The gateway pins one egress
> identity and throttles it; it does not cycle IPs.

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

## Project layout

```
app/
  main.py        FastAPI app, /v1 surface, admin API, static hosting
  router.py      candidate resolution, ordering, failover execution
  adapters.py    OpenAI / Anthropic / Gemini request + SSE translation
  limiter.py     per-key RPM/TPM/concurrency governor and cooldowns
  db.py          SQLite store, column allow-list, encryption hooks
  security.py    constant-time compare, lockout, rate limit, URL validation
  crypto.py      optional AES-256-GCM secret box
  config.py      environment configuration
  providers.py   provider presets
  util.py        token estimation, SSE helpers, header parsing
  static/        admin console (HTML/CSS/JS)
tests/           pytest suite + mock upstream
deploy/          example reverse-proxy / tunnel configuration
Dockerfile, docker-compose.yml, .env.example, pyproject.toml
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
