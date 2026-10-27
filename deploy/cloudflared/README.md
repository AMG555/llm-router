# Cloudflare Tunnel

Expose the LLM Router through Cloudflare **without opening any inbound port**.
`cloudflared` dials out to Cloudflare's edge and proxies `https://your-host`
traffic to the router over an encrypted tunnel.

## When you need it

Use a tunnel when you **self-host** and one of these applies:

- the machine has no public IP or sits behind NAT/firewall,
- you don't want to open/publish ports,
- you want Cloudflare's WAF, DDoS protection or Zero Trust Access in front.

You **do not** need it when your platform already terminates TLS and gives you a
public HTTPS URL — a tunnel there just adds a hop. (It is always safe to run,
just not necessary.)

---

## Method A — Dashboard-managed tunnel (recommended)

A token-driven tunnel you manage entirely from the Cloudflare dashboard. No
local config file.

1. **Have a domain on Cloudflare.** Add your domain to Cloudflare and point its
   nameservers at Cloudflare.
2. **Create the tunnel.** Zero Trust dashboard → **Networks → Tunnels →
   Create a tunnel** → *Cloudflared* → name it (e.g. `llm-router`) → **Save**.
3. **Get the token.** On the connector step, choose **Docker** and copy the
   token (the long string after `--token`).
4. **Give it to compose.** Put it in `.env` (next to the router's own vars):

   ```dotenv
   TUNNEL_TOKEN=eyJhIjoi..........
   ```

5. **Start both services:**

   ```bash
   docker compose --profile tunnel up -d --build
   ```

6. **Add a public hostname.** Back in the tunnel's dashboard → **Public Hostname
   → Add**. Set:
   - Subdomain/domain: e.g. `router.example.com`
   - Service: **HTTP** → `router:7860`  *(this is the compose service name and port)*
7. **Verify:**

   ```bash
   curl -fsS https://router.example.com/health
   # {"status":"ok", ...}
   ```

Because the tunnel provides TLS, **remove the `ports:` mapping** from the
`router` service if you want the container reachable only through Cloudflare.

---

## Method B — Locally-managed named tunnel (config file)

A tunnel whose config and credentials you keep on disk.

```bash
cloudflared tunnel login
cloudflared tunnel create llm-router          # note the tunnel ID
cloudflared tunnel route dns llm-router router.example.com
cp deploy/cloudflared/config.example.yml deploy/cloudflared/config.yml
$EDITOR deploy/cloudflared/config.yml         # set hostname + credentials-file
```

Run it alongside the router (adds a `tunnel-file` style service, or run
`cloudflared` directly on the host):

```bash
docker run -d --name cf-tunnel --restart unless-stopped \
  --network llm-router_default \
  -v "$PWD/deploy/cloudflared:/etc/cloudflared" \
  cloudflare/cloudflared:latest \
  tunnel --no-autoupdate --config /etc/cloudflared/config.yml run
```

The `credentials-file` in `config.yml` must point at the JSON you downloaded
(`/etc/cloudflared/<TUNNEL-ID>.json`). `docker compose network ls` shows the
actual network name if it isn't `llm-router_default`.

---

## Method C — Quick token run (no compose)

```bash
docker run -d --name cf-tunnel --restart unless-stopped \
  --network llm-router_default \
  cloudflare/cloudflared:latest \
  tunnel --no-autoupdate run --token <TUNNEL_TOKEN>
```

Use this when you already run the router some other way and only need the
connector.

---

## Recommendation: gate the admin console

The tunnel makes the console reachable from the internet. The admin API is
already token-protected, but you can add a second layer in Cloudflare:

- **Zero Trust → Access → Applications** → add a self-hosted app for
  `router.example.com/admin/*` (and `/*` if you like) with an email/SSO policy.
- Or add a **WAF** rule to block everything except your IPs on `/admin/*`.

Keep `ADMIN_TOKEN` long and random regardless. `/v1/*` is protected by gateway
keys, so clients keep using Bearer auth through the tunnel unchanged.

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `502` / `connection refused` from Cloudflare | Wrong service target — must be `http://router:7860` and both containers on the same Docker network. |
| Tunnel connects but hostname unreachable | Public hostname not added to the tunnel, or DNS route missing. |
| Works locally, not through the tunnel | The `router` container isn't listening on `0.0.0.0:7860`, or `PORT` was changed. |
| HSTS not emitted | Start uvicorn with `--proxy-headers --forwarded-allow-ips=*` (already the image default). |
| Token rejected | Re-copy the connector token; it differs per tunnel. |

---

## Security notes

- Real config/credentials are **git-ignored** — never commit them.
- The tunnel uses outbound-only connections; no inbound firewall ports required.
- TLS/HSTS is handled at the edge; the router must still be run with
  `--proxy-headers` so it sees `X-Forwarded-Proto: https`.
