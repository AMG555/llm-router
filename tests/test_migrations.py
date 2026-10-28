"""Versioned schema migrations."""
import os
import sqlite3
import tempfile


def test_fresh_db_records_all_migrations(app_module):
    store = app_module.store
    versions = {r["version"] for r in store.conn.execute("SELECT version FROM schema_migrations")}
    assert {1, 2, 3} <= versions


def test_legacy_db_is_upgraded():
    path = os.path.join(tempfile.mkdtemp(prefix="legacy2-"), "router.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE providers(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,"
                " adapter TEXT, base_url TEXT, headers TEXT, notes TEXT, enabled INTEGER, created_at REAL)")
    con.execute("CREATE TABLE gateway_keys(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,"
                " key TEXT UNIQUE, enabled INTEGER, created_at REAL)")
    con.execute("INSERT INTO providers(name,adapter,base_url,headers,notes,enabled,created_at)"
                " VALUES('p','openai','https://x/v1','{}','',1,0)")
    con.execute("INSERT INTO gateway_keys(label,key,enabled,created_at) VALUES('d','sk-old',1,0)")
    con.commit()
    con.close()

    from app.db import Store
    store = Store(path)
    pcols = {r["name"] for r in store.conn.execute("PRAGMA table_info(providers)")}
    gcols = {r["name"] for r in store.conn.execute("PRAGMA table_info(gateway_keys)")}
    assert "proxy" in pcols
    assert {"allowed_models", "max_rpm"} <= gcols
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"admin_tokens", "audit_log", "schema_migrations"} <= tables
    assert "sk-old" in store.gateway
    assert store.gateway_meta["sk-old"]["max_rpm"] == 0


def test_migrations_are_idempotent(app_module):
    from app.db import Store
    path = app_module.store.path
    store = Store(path)  # re-open an already-migrated DB
    versions = [r["version"] for r in store.conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == sorted(set(versions))
