import os
import secrets
from pathlib import Path


def _bool(v, d=False):
    if v is None:
        return d
    return str(v).lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        # --- admin auth (never leave the admin surface unauthenticated) ---
        self.admin_token = os.getenv("ADMIN_TOKEN", "").strip()
        self.admin_token_generated = False
        if not self.admin_token:
            self.admin_token = "admin-" + secrets.token_urlsafe(24)
            self.admin_token_generated = True

        # --- runtime ---
        self.env = (os.getenv("ENV", "production") or "production").lower()
        self.db_path = Path(os.getenv("DB_PATH", "./data/router.db"))
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "600"))
        self.connect_timeout = float(os.getenv("CONNECT_TIMEOUT", "15"))
        self.max_attempts = int(os.getenv("MAX_ATTEMPTS", "8"))
        self.default_cooldown = float(os.getenv("DEFAULT_COOLDOWN", "20"))
        self.short_cooldown = float(os.getenv("SHORT_COOLDOWN", "5"))

        # --- gateway surface ---
        self.require_auth = _bool(os.getenv("REQUIRE_GATEWAY_AUTH"), True)
        self.gateway_rpm = int(os.getenv("GATEWAY_RPM", "0"))  # 0 = unlimited
        self.max_body_bytes = int(os.getenv("MAX_BODY_BYTES", "2097152"))

        # --- web hardening ---
        self.cors = os.getenv("CORS_ORIGINS", "").strip()  # empty => same-origin only
        self.trusted_hosts = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "*").split(",") if h.strip()]
        self.enable_docs = _bool(os.getenv("ENABLE_DOCS"), False)
        self.force_https = _bool(os.getenv("FORCE_HTTPS"), False)
        self.admin_max_fails = int(os.getenv("ADMIN_MAX_FAILS", "10"))
        self.admin_lock_seconds = int(os.getenv("ADMIN_LOCK_SECONDS", "300"))

        # --- secrets ---
        self.secret_key = os.getenv("SECRET_KEY", "").strip()  # enables at-rest encryption
        self.block_private_upstreams = _bool(os.getenv("BLOCK_PRIVATE_UPSTREAMS"), False)

        # --- egress / static outbound IP ---
        # If set, all upstream calls leave through this proxy (must be a fixed,
        # allowlisted egress IP). Standard HTTP_PROXY/HTTPS_PROXY are also
        # honoured when this is unset.
        self.egress_proxy = os.getenv("EGRESS_PROXY", "").strip()
        # Cap upstream requests per minute per egress identity (proxy IP, or
        # the default egress). Set this BELOW the provider's per-IP limit so
        # the gateway throttles instead of tripping the limit. 0 = unlimited.
        self.egress_ip_rpm = int(os.getenv("EGRESS_IP_RPM", "0"))
        # Optional URL returning the caller's public IP (e.g. an IP echo
        # service); used by GET /admin/api/egress?check=1.
        self.egress_check_url = os.getenv("EGRESS_CHECK_URL", "").strip()

        # --- misc ---
        self.bootstrap = os.getenv("BOOTSTRAP_CONFIG", "").strip()
        self.log_requests = _bool(os.getenv("LOG_REQUESTS"), True)
        self.max_log_rows = int(os.getenv("MAX_LOG_ROWS", "5000"))

    @property
    def is_production(self) -> bool:
        return self.env not in ("dev", "development", "local", "test")


settings = Settings()
