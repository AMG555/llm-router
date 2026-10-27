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

One OpenAI-compatible endpoint. Unlimited providers, unlimited keys, zero rate-limit pain.

## Endpoint

- Base URL: `https://<your-space>.hf.space/v1`
- Auth: `Authorization: Bearer <gateway key from the admin UI>`
- Model: any name you defined as a virtual model

## Admin UI

Open the Space root URL. Paste your `ADMIN_TOKEN` when prompted.

## Free-tier notes

- HF Spaces free: filesystem is ephemeral. Set `BOOTSTRAP_CONFIG` (base64 JSON) so config reloads on restart.
- Render free: spins down after ~15 min idle. Point a free uptime pinger at `/health`.

## Deploy

### Hugging Face Spaces (free, Docker)

1. New Space → **SDK: Docker** → *Blank*.
2. Upload every file above, preserving the `app/` folder.
3. Space Settings → **Variables and secrets** → add `ADMIN_TOKEN` (random string).
4. It builds and serves on port 7860. Open the Space URL, paste the token, start adding providers.

Free Spaces sleep after 48h idle and have ephemeral disk. Keep config portable:

```bash
# locally, after exporting config from the UI
base64 -w0 llm-router-config.json
```

Paste the result into a Space secret named `BOOTSTRAP_CONFIG`. On every cold boot the router rebuilds itself.

### Render free tier

1. Push to GitHub, then Render → **New → Blueprint**, point at the repo (it reads `render.yaml`).
2. Add `ADMIN_TOKEN` as an env var if not auto-generated.
3. Free instances sleep after 15 idle minutes. Add a free cron pinger (cron-job.org / UptimeRobot) hitting `https://yourapp.onrender.com/health` every 10 minutes to keep it warm — a self-ping won't work because the process is already asleep.

### Any VPS / Docker host

```bash
docker build -t llm-router .
docker run -d --name router -p 7860:7860 \
  -e ADMIN_TOKEN=your-secret \
  -v $(pwd)/data:/data \
  llm-router
```

The `-v` mount makes SQLite persistent.

## Wire up your app

You only ever change the base URL and the key. Nothing else.

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://your-space.hf.space/v1",
    api_key="sk-router-xxxxx",      # gateway key from the admin UI
)

r = client.chat.completions.create(
    model="fast-chat",              # your virtual model
    messages=[{"role": "user", "content": "hello"}],
)
print(r.choices[0].message.content)
```

```bash
curl https://your-space.hf.space/v1/chat/completions \
  -H "Authorization: Bearer sk-router-xxxxx" \
  -H "Content-Type: application/json" \
  -H "x-session-id: user-42-conv-7" \
  -d '{"model":"fast-chat","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

Every response carries `x-router-provider`, `x-router-key`, and `x-router-attempts` so you can see exactly which key served it.

## How it dodges limits — the actual mechanics

**Sliding-window governor.** Each key keeps a 60-second deque of request timestamps and a deque of `(timestamp, tokens)` pairs. Before a key is used, the governor prunes anything older than 60s and checks three things: `len(rpm_deque) < max_rpm`, `sum(tpm) < max_tpm`, `inflight < max_concurrency`. A key at budget is *skipped*, not queued — that's why there's no lag.

**Retry-After, honored exactly.** On a 429 the router reads `retry-after` plus Groq/OpenAI/Anthropic's `x-ratelimit-reset-*` headers, parsing `ms`, `s`, and `m` suffixes. That key goes into a cooldown until the provider says it's free, then returns to the pool automatically. Nothing is hard-disabled.

**Failover before first byte.** `httpx.send(req, stream=True)` returns as soon as headers land. The router inspects the status code *before* touching the body. 429 → next key. 5xx/408/timeout → short 5s cooldown, next key. 401/403 → 120s cooldown, next key. 404 → the model doesn't exist there, next key. Only genuine 4xx client errors pass through un-rotated. Streaming clients never see the retry.

**Smooth weighted round-robin.** Nginx-style: each candidate carries a running counter; the router repeatedly picks the max `(counter + weight)` and subtracts total weight. Higher `weight` = proportionally more traffic, with no bursty pattern.

**Sticky sessions.** Send `x-session-id` and the same conversation pins to the same key, so upstream prompt caching actually hits. Without it, requests rotate freely.

**Two-tier caps.** Limits live on the *key* (the real shared provider resource) and separately on the *target* (per-model ceiling). If one key serves three virtual models, its RPM budget is shared correctly across all of them.

**Token accounting.** Prompt tokens estimated from the payload; completion tokens read from `usage` on non-streaming and from the final usage chunk on streaming (the router injects `stream_options.include_usage` for OpenAI-compatible upstreams). This feeds TPM windows so they're real, not decorative.

**Health scoring.** Per-key latency EMA tracks slow keys. Use `least_latency` strategy to route around them, or `priority` for strict failover chains.

## Compliance note

This router pools keys **you control**. Using it to evade a provider's per-account rate limits, or to run many accounts of the same provider to farm free quota, may breach that provider's terms of service and can get accounts/keys banned. Use it with keys you're authorized to aggregate (separate projects, teams, or orgs), and respect each provider's terms.

## Things worth knowing

- **Anthropic and Gemini are first-class.** The router translates OpenAI-format requests into `/v1/messages` and `:generateContent` and translates responses and SSE streams back. Your app stays OpenAI-shaped even when hitting Claude or Gemini.
- **`provider:model` escape hatch.** Request `groq:llama-3.3-70b-versatile` and it routes directly to that provider using its whole key pool — no virtual model needed.
- **Don't commit keys.** Export config, base64 it, and use it as an env secret. Keys are masked in every API response.
- **Render free's 15-minute spin-down is the one real weakness.** No router logic can fix a sleeping process; the external pinger is the answer.
- **Free provider tiers are aggressively rate-limited.** Add 4-6 keys per provider and set `max_rpm` on each to roughly 80% of the documented limit — headroom absorbs clock skew between you and them.
