import sqlite3
import threading
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
"""

# Columns a caller is allowed to write, per table. Anything else is rejected,
# which closes SQL injection through attacker-controlled column names.
COLUMNS = {
    "providers": {"name", "adapter", "base_url", "headers", "notes", "enabled", "created_at"},
    "api_keys": {"provider_id", "label", "api_key", "enabled", "weight", "priority",
                 "max_rpm", "max_tpm", "max_concurrency", "created_at"},
    "vmodels": {"name", "description", "strategy", "sticky", "enabled", "created_at"},
    "targets": {"vmodel_id", "provider_id", "key_id", "upstream_model", "weight",
                "priority", "max_rpm", "max_tpm", "enabled", "created_at"},
    "gateway_keys": {"label", "key", "enabled", "created_at"},
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
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.providers = {}
        self.keys = {}
        self.keys_by_provider = {}
        self.vmodels = {}
        self.vmodels_by_id = {}
        self.targets = []
        self.gateway = set()
        self.load()

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
            self.gateway = {self.box.decrypt(r["key"]) for r in self.conn.execute(
                "SELECT key FROM gateway_keys WHERE enabled=1")}

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
        with self.lock:
            self.conn.execute(
                "INSERT INTO logs(ts,vmodel,provider,key_label,status,latency_ms,attempts,"
                "prompt_tokens,completion_tokens,error,session) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kw.get("ts"), kw.get("vmodel"), kw.get("provider"), kw.get("key_label"),
                 kw.get("status"), kw.get("latency_ms"), kw.get("attempts"),
                 kw.get("prompt_tokens"), kw.get("completion_tokens"),
                 kw.get("error"), kw.get("session")))
            self.conn.commit()

    def prune_logs(self, keep: int):
        with self.lock:
            self.conn.execute(
                "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (keep,))
            self.conn.commit()
