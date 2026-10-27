import os
from pathlib import Path


def _bool(v, d=False):
    if v is None:
        return d
    return str(v).lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        self.admin_token = os.getenv("ADMIN_TOKEN", "").strip()
        self.db_path = Path(os.getenv("DB_PATH", "./data/router.db"))
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "600"))
        self.connect_timeout = float(os.getenv("CONNECT_TIMEOUT", "15"))
        self.max_attempts = int(os.getenv("MAX_ATTEMPTS", "8"))
        self.default_cooldown = float(os.getenv("DEFAULT_COOLDOWN", "20"))
        self.short_cooldown = float(os.getenv("SHORT_COOLDOWN", "5"))
        self.require_auth = _bool(os.getenv("REQUIRE_GATEWAY_AUTH"), True)
        self.allow_unauth = _bool(os.getenv("ALLOW_UNAUTHENTICATED"), True)
        self.cors = os.getenv("CORS_ORIGINS", "*")
        self.bootstrap = os.getenv("BOOTSTRAP_CONFIG", "").strip()
        self.log_requests = _bool(os.getenv("LOG_REQUESTS"), True)
        self.max_log_rows = int(os.getenv("MAX_LOG_ROWS", "5000"))


settings = Settings()
