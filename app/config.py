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
        # Optional fixed client-facing key. Recommended on hosts with an
        # ephemeral filesystem so the gateway key survives restarts.
        self.gateway_key = os.getenv("GATEWAY_KEY", "").strip()

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
        # Optional Redis URL: shares rate windows and cooldowns across
        # instances. Empty = in-process state (single instance).
        self.redis_url = os.getenv("REDIS_URL", "").strip()
        # Optional URL returning the caller's public IP (an IP echo service).
        # Used to watch the egress IP. Defaults to api.ipify.org; set to an
        # empty string to disable the watch.
        self.egress_check_url = os.getenv("EGRESS_CHECK_URL", "https://api.ipify.org").strip()
        # How often (seconds) to re-check the public egress IP.
        self.egress_check_interval = int(os.getenv("EGRESS_CHECK_INTERVAL", "900"))

        # --- misc ---
        self.bootstrap = os.getenv("BOOTSTRAP_CONFIG", "").strip()
        # Optional URL to a JSON config restored on boot when the DB is empty
        # (survives ephemeral filesystems without a paid disk).
        self.bootstrap_url = os.getenv("BOOTSTRAP_URL", "").strip()
        # Optional URL to auto-PUT the full config to after every change
        # (paired with BOOTSTRAP_URL for durable free-tier persistence).
        self.persist_url = os.getenv("PERSIST_URL", "").strip()
        self.persist_token = os.getenv("PERSIST_TOKEN", "").strip()
        self.log_requests = _bool(os.getenv("LOG_REQUESTS"), True)
        self.max_log_rows = int(os.getenv("MAX_LOG_ROWS", "5000"))

        # --- observability ---
        self.log_format = (os.getenv("LOG_FORMAT", "text") or "text").lower()  # text|json
        self.metrics_enabled = _bool(os.getenv("METRICS_ENABLED"), True)
        self.metrics_public = _bool(os.getenv("METRICS_PUBLIC"), False)

    @property
    def is_production(self) -> bool:
        return self.env not in ("dev", "development", "local", "test")


settings = Settings()
