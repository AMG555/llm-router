"""Egress / static-outbound-IP features: per-provider proxy, client pool,
schema migration and the egress inspector."""
import os
import sqlite3
import tempfile


def test_provider_proxy_stored_and_masked(client, admin_h):
    r = client.post("/admin/api/providers", headers=admin_h, json={
        "name": "egr-1", "adapter": "openai", "base_url": "https://api.example.com/v1",
        "proxy": "http://user:pass@proxy.example:3128"})
    assert r.status_code == 200, r.text
    st = client.get("/admin/api/state", headers=admin_h).json()
    p = next(x for x in st["providers"] if x["name"] == "egr-1")
    assert p["proxy_set"] is True
    assert "user:pass" not in p["proxy"]
    assert "proxy.example:3128" in p["proxy"]


def test_invalid_proxy_rejected(client, admin_h):
    r = client.post("/admin/api/providers", headers=admin_h, json={
        "name": "egr-bad", "adapter": "openai", "base_url": "https://api.example.com/v1",
        "proxy": "not-a-url"})
    assert r.status_code == 400


def test_edit_provider_proxy(client, admin_h):
    pid = client.post("/admin/api/providers", headers=admin_h, json={
        "name": "egr-2", "adapter": "openai", "base_url": "https://api.example.com/v1"}).json()["id"]
    r = client.patch(f"/admin/api/providers/{pid}", headers=admin_h,
                     json={"proxy": "http://10.0.0.9:8080"})
    assert r.status_code == 200
    assert client.patch(f"/admin/api/providers/{pid}", headers=admin_h,
                        json={"proxy": "ftp://bad"}).status_code == 400


def test_client_pool_reuses_by_proxy(app_module):
    router = app_module.router
    a1 = router.client_for("http://a.example:1")
    a2 = router.client_for("http://a.example:1")
    b = router.client_for("http://b.example:2")
    default = router.client_for(None)
    assert a1 is a2
    assert a1 is not b
    assert default is not a1


def test_migration_adds_proxy_column_to_legacy_db():
    path = os.path.join(tempfile.mkdtemp(prefix="legacy-"), "router.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE providers(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,"
        " adapter TEXT, base_url TEXT, headers TEXT, notes TEXT, enabled INTEGER, created_at REAL)")
    con.execute("INSERT INTO providers(name,adapter,base_url,headers,notes,enabled,created_at)"
                " VALUES('legacy','openai','https://x/v1','{}','',1,0)")
    con.commit()
    con.close()

    from app.db import Store
    store = Store(path)
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(providers)")}
    assert "proxy" in cols
    store.update("providers", 1, {"proxy": "http://proxy.example:3128"})
    assert store.providers[1]["proxy"] == "http://proxy.example:3128"


def test_egress_endpoint_requires_admin_and_reports(client, admin_h):
    assert client.get("/admin/api/egress").status_code == 401
    r = client.get("/admin/api/egress", headers=admin_h)
    assert r.status_code == 200
    body = r.json()
    assert "local_ip" in body and "providers" in body
    assert body["global_proxy_set"] is False
