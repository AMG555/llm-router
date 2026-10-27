# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately rather than opening a public
issue. Include a description, reproduction steps, and the affected version or
commit. We aim to acknowledge reports within a few business days.

## Supported deployments

Run the current `main` with:

- `ADMIN_TOKEN` set to a long random value (never left at the auto-generated
  default in production).
- `SECRET_KEY` set, so stored upstream keys are encrypted at rest.
- TLS terminated in front of the service, with `--proxy-headers` enabled.
- `CORS_ORIGINS` left empty unless you genuinely need cross-origin browser
  access; set `TRUSTED_HOSTS` to your hostname(s).

## Built-in controls

| Control | Where |
| --- | --- |
| Constant-time token comparison | `app/security.py` |
| Per-IP admin lockout | `app/security.py` |
| Column allow-list on all writes | `app/db.py` |
| AES-256-GCM at-rest encryption | `app/crypto.py` |
| Security headers + strict CSP | `app/security.py`, `app/main.py` |
| Request body size cap | `app/main.py` |
| Upstream URL / SSRF validation | `app/security.py` |
| Masked secret export by default | `app/main.py` |

## Scope

Out of scope: vulnerabilities in the upstream providers you connect to, in
third-party reverse proxies or tunnels, and any consequence of violating a
provider's terms of service.
