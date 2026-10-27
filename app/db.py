import sqlite3
import threading
from pathlib import Path

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


class Store:
    """SQLite persistence + hot in-memory registry used by the router."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    # ---------- cache ----------
    def load(self):
        with self.lock:
            self.providers = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM providers")}
            self.keys = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM api_keys")}
            self.keys_by_provider = {}
            for k in self.keys.values():
                self.keys_by_provider.setdefault(k["provider_id"], []).append(k)
            self.vmodels = {r["name"]: dict(r) for r in self.conn.execute("SELECT * FROM vmodels")}
            self.vmodels_by_id = {v["id"]: v for v in self.vmodels.values()}
            self.targets = [dict(r) for r in self.conn.execute("SELECT * FROM targets")]
            self.gateway = {r["key"] for r in self.conn.execute(
                "SELECT key FROM gateway_keys WHERE enabled=1")}

    # ---------- crud ----------
    def insert(self, table, data: dict):
        cols = ",".join(data.keys())
        ph = ",".join("?" * len(data))
        with self.lock:
            cur = self.conn.execute(
                f"INSERT INTO {table}({cols}) VALUES({ph})", list(data.values()))
            self.conn.commit()
            self.load()
            return cur.lastrowid

    def update(self, table, rid, data: dict):
        if not data:
            return
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

    def rows(self, table, where="", args=()):
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def log(self, **kw):
        if not settings_log_enabled():
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


def settings_log_enabled():
    from .config import settings
    return settings.log_requests
