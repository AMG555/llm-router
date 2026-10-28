import sqlite3
import threading
import time
from pathlib import Path

from .config import settings
from .crypto import SecretBox

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  adapter TEXT NOT NULL DEFAULT 'openai',
  base_url TEXT NOT NULL,
  headers TEXT DEFAULT '{}',
  proxy TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id INTEGER NOT NULL,
  label TEXT DEFAULT '',
  api_key TEXT NOT NULL,
  enabled INTEGER DEFAULT 1,
  weight REAL DEFAULT 1,
  priority INTEGER DEFAULT 100,
  max_rpm INTEGER DEFAULT 0,
  max_tpm INTEGER DEFAULT 0,
  max_concurrency INTEGER DEFAULT 0,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS vmodels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT DEFAULT '',
  strategy TEXT DEFAULT 'balanced',
  sticky INTEGER DEFAULT 1,
  enabled INTEGER DEFAULT 1,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vmodel_id INTEGER NOT NULL,
  provider_id INTEGER NOT NULL,
  key_id INTEGER,
  upstream_model TEXT NOT NULL,
  weight REAL DEFAULT 1,
  priority INTEGER DEFAULT 100,
  max_rpm INTEGER DEFAULT 0,
  max_tpm INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS gateway_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT DEFAULT '',
  key TEXT UNIQUE NOT NULL,
  enabled INTEGER DEFAULT 1,
  allowed_models TEXT DEFAULT '',
  max_rpm INTEGER DEFAULT 0,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, vmodel TEXT, provider TEXT, key_label TEXT,
  status INTEGER, latency_ms INTEGER, attempts INTEGER,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  error TEXT, session TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
CREATE TABLE IF NOT EXISTS admin_tokens(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT DEFAULT '',
  token_hash TEXT UNIQUE NOT NULL,
  role TEXT DEFAULT 'admin',
  enabled INTEGER DEFAULT 1,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, actor TEXT, role TEXT, action TEXT, target TEXT, detail TEXT, ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY, name TEXT, applied_at REAL
);
"""

# Columns a caller is allowed to write, per table. Anything else is rejected,
# which closes SQL injection through attacker-controlled column names.
COLUMNS = {
    "providers": {"name", "adapter", "base_url", "headers", "proxy", "notes", "enabled", "created_at"},
    "api_keys": {"provider_id", "label", "api_key", "enabled", "weight", "priority",
                 "max_rpm", "max_tpm", "max_concurrency", "created_at"},
    "vmodels": {"name", "description", "strategy", "sticky", "enabled", "created_at"},
    "targets": {"vmodel_id", "provider_id", "key_id", "upstream_model", "weight",
                "priority", "max_rpm", "max_tpm", "enabled", "created_at"},
    "gateway_keys": {"label", "key", "enabled", "allowed_models", "max_rpm", "created_at"},
    "admin_tokens": {"label", "token_hash", "role", "enabled", "created_at"},
    "audit_log": {"ts", "actor", "role", "action", "target", "detail", "ip"},
}

# Fields encrypted at rest when SECRET_KEY is configured.
SENSITIVE = {"api_keys": {"api_key"}, "gateway_keys": {"key"}, "providers": {"headers"}}


class Store:
    """SQLite persistence + hot in-memory registry used by the router."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.box = SecretBox(settings.secret_key)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._run_migrations()
        self.conn.commit()
        self.providers = {}
        self.keys = {}
        self.keys_by_provider = {}
        self.vmodels = {}
        self.vmodels_by_id = {}
        self.targets = []
        self.gateway = set()
        self.gateway_meta = {}
        self.admin_tokens = {}
        self._logbuf = []
        self._last_flush = 0.0
        self.load()

    # ---------- migrations ----------
    MIGRATIONS = [
        (1, "baseline"),
        (2, "gateway_key_scopes"),
        (3, "admin_tokens_and_audit"),
    ]

    def _has_column(self, table, col) -> bool:
        return col in {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _run_migrations(self):
        """Apply ordered, idempotent migrations and record them."""
        applied = {r["version"] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        for version, name in self.MIGRATIONS:
            if version in applied:
                continue
            getattr(self, f"_m{version}")(name)
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (version, name, time.time()))
            self.conn.commit()

    def _m1(self, _name):
        pass  # baseline schema already created above

    def _m2(self, _name):
        if not self._has_column("providers", "proxy"):
            self.conn.execute("ALTER TABLE providers ADD COLUMN proxy TEXT DEFAULT ''")
        if not self._has_column("gateway_keys", "allowed_models"):
            self.conn.execute("ALTER TABLE gateway_keys ADD COLUMN allowed_models TEXT DEFAULT ''")
        if not self._has_column("gateway_keys", "max_rpm"):
            self.conn.execute("ALTER TABLE gateway_keys ADD COLUMN max_rpm INTEGER DEFAULT 0")

    def _m3(self, _name):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS admin_tokens(
              id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT DEFAULT '',
              token_hash TEXT UNIQUE NOT NULL, role TEXT DEFAULT 'admin',
              enabled INTEGER DEFAULT 1, created_at REAL);
            CREATE TABLE IF NOT EXISTS audit_log(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, role TEXT,
              action TEXT, target TEXT, detail TEXT, ip TEXT);
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
        """)

    # ---------- validation / crypto ----------
    @staticmethod
    def _sanitize(table, data: dict, allow_id=False) -> dict:
        if table not in COLUMNS:
            raise ValueError(f"unknown table: {table}")
        allowed = COLUMNS[table] | ({"id"} if allow_id else set())
        clean = {}
        for k, v in data.items():
            if k not in allowed:
                raise ValueError(f"unknown column '{k}' for table '{table}'")
            clean[k] = v
        return clean

    def _encrypt_row(self, table, data: dict) -> dict:
        fields = SENSITIVE.get(table, set())
        for f in fields:
            if f in data and isinstance(data[f], str):
                data[f] = self.box.encrypt(data[f])
        return data

    def _decrypt_row(self, table, data: dict) -> dict:
        fields = SENSITIVE.get(table, set())
        for f in fields:
            if f in data and isinstance(data[f], str):
                data[f] = self.box.decrypt(data[f])
        return data

    # ---------- cache ----------
    def load(self):
        with self.lock:
            self.providers = {r["id"]: self._decrypt_row("providers", dict(r))
                              for r in self.conn.execute("SELECT * FROM providers")}
            self.keys = {r["id"]: self._decrypt_row("api_keys", dict(r))
                         for r in self.conn.execute("SELECT * FROM api_keys")}
            self.keys_by_provider = {}
            for k in self.keys.values():
                self.keys_by_provider.setdefault(k["provider_id"], []).append(k)
            self.vmodels = {r["name"]: dict(r) for r in self.conn.execute("SELECT * FROM vmodels")}
            self.vmodels_by_id = {v["id"]: v for v in self.vmodels.values()}
            self.targets = [dict(r) for r in self.conn.execute("SELECT * FROM targets")]
            self.gateway = set()
            self.gateway_meta = {}
            for r in self.conn.execute("SELECT * FROM gateway_keys"):
                if not r["enabled"]:
                    continue
                k = self.box.decrypt(r["key"])
                self.gateway.add(k)
                self.gateway_meta[k] = {"id": r["id"], "label": r["label"],
                                        "allowed_models": (r["allowed_models"] or "") if "allowed_models" in r.keys() else "",
                                        "max_rpm": (r["max_rpm"] or 0) if "max_rpm" in r.keys() else 0}
            self.admin_tokens = {}
            for r in self.conn.execute("SELECT id,token_hash,role,label FROM admin_tokens WHERE enabled=1"):
                self.admin_tokens[r["token_hash"]] = {"id": r["id"], "role": r["role"], "label": r["label"]}

    # ---------- crud ----------
    def insert(self, table, data: dict):
        data = self._sanitize(table, data)
        data = self._encrypt_row(table, dict(data))
        cols = ",".join(data.keys())
        ph = ",".join("?" * len(data))
        with self.lock:
            cur = self.conn.execute(
                f"INSERT INTO {table}({cols}) VALUES({ph})", list(data.values()))
            self.conn.commit()
            self.load()
            return cur.lastrowid

    def update(self, table, rid, data: dict):
        data = self._sanitize(table, data)
        if not data:
            return
        data = self._encrypt_row(table, dict(data))
        sets = ",".join(f"{k}=?" for k in data)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*data.values(), rid])
            self.conn.commit()
            self.load()

    def delete(self, table, rid):
        with self.lock:
            self.conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            if table == "providers":
                self.conn.execute("DELETE FROM api_keys WHERE provider_id=?", (rid,))
                self.conn.execute("DELETE FROM targets WHERE provider_id=?", (rid,))
            if table == "vmodels":
                self.conn.execute("DELETE FROM targets WHERE vmodel_id=?", (rid,))
            self.conn.commit()
            self.load()

    def rows(self, table, where="", args=(), reveal=False):
        if table == "logs":
            self.flush_logs()
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        out = [dict(r) for r in self.conn.execute(sql, args)]
        if table in SENSITIVE:
            out = [self._decrypt_row(table, r) for r in out]
        return out

    def log(self, **kw):
        if not settings.log_requests:
            return
        row = (kw.get("ts"), kw.get("vmodel"), kw.get("provider"), kw.get("key_label"),
               kw.get("status"), kw.get("latency_ms"), kw.get("attempts"),
               kw.get("prompt_tokens"), kw.get("completion_tokens"),
               kw.get("error"), kw.get("session"))
        with self.lock:
            self._logbuf.append(row)
            now = time.time()
            if len(self._logbuf) >= 50 or (now - self._last_flush) >= 1.0:
                self._flush_logs_locked(now)

    def _flush_logs_locked(self, now=None):
        if not self._logbuf:
            return
        self.conn.executemany(
            "INSERT INTO logs(ts,vmodel,provider,key_label,status,latency_ms,attempts,"
            "prompt_tokens,completion_tokens,error,session) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            self._logbuf)
        self.conn.commit()
        self._logbuf.clear()
        self._last_flush = now or time.time()

    def flush_logs(self):
        with self.lock:
            self._flush_logs_locked()

    def clear_logs(self):
        with self.lock:
            self._logbuf.clear()
            self.conn.execute("DELETE FROM logs")
            self.conn.commit()

    def audit(self, actor, role, action, target="", detail="", ip=""):
        """Append an audit record (no full cache reload)."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit_log(ts,actor,role,action,target,detail,ip) VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor, role, action, target, detail, ip))
            self.conn.commit()

    def prune_audit(self, keep: int):
        with self.lock:
            self.conn.execute(
                "DELETE FROM audit_log WHERE id NOT IN (SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
                (keep,))
            self.conn.commit()

    def prune_logs(self, keep: int):
        with self.lock:
            self._flush_logs_locked()
            self.conn.execute(
                "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (keep,))
            self.conn.commit()
