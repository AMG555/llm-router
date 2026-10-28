# Cloudflare Worker (optional edge in front)

A tiny Worker that proxies to your deployment and caches the public liveness
endpoints at the edge. Deploy with:

```bash
cd deploy/cloudflare-worker
cp wrangler.toml.example wrangler.toml     # set ORIGIN
npx wrangler deploy
```

## Does this reduce free-tier burn? Mostly no — read this first

The thing that burns a free Render instance is **instance-hours**: the service
counts as running whenever it is awake. An always-on probe (UptimeRobot, a
Worker cron, etc.) keeps it **awake 24/7**, which uses ~730 of the 750 free
hours a month — the *opposite* of reducing burn. A Worker cannot make a sleeping
instance wake faster, and adding one hop to `/v1/*` increases latency.

So a Worker in front is **not** the lever for burn. What actually reduces burn:

- **Let it sleep.** Don't keep it warm if you don't need sub-second first
  responses; accept the ~50 s cold start. A 5-minute monitor wakes it often
  enough for real traffic while idle time still counts as zero hours.
- **Move static assets off the origin.** Serve the console (`app/static/*`)
  from Cloudflare Pages and point the API at the Worker/origin, so browsing the
  UI never wakes Render.
- **Add a persistent disk / paid tier** if you need always-on.

## When the Worker *is* worth it

- You want Cloudflare's **WAF / bot protection / rate limiting** in front of
  the public `/v1` surface.
- You want to **cache** the public `/health` probe at the edge so monitor hits
  don't reach the origin (small saving).
- You want a stable **custom hostname** or edge TLS in front of the origin.

## What it must never do

- **Do not cache `/v1/*`.** Those responses are per-caller (auth by gateway
  key); caching by URL would leak one caller's data to another. The example
  caches only `/health` and `/healthz`.
- **Do not move secrets to the Worker.** Auth, scopes and rate limiting stay in
  the gateway; the Worker is a dumb proxy.

## Notes

- Streaming works: the Worker returns the upstream body as a readable stream.
- Every API call gains one round trip; measure before routing production
  `/v1/*` through it.
