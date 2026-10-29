"""Config bootstrap from base64 or a URL (free-tier persistence)."""
import asyncio
import base64
import json

from app.config import settings


def test_load_bootstrap_from_base64(app_module):
    old_c, old_u = settings.bootstrap, settings.bootstrap_url
    settings.bootstrap_url = ""
    settings.bootstrap = base64.b64encode(
        json.dumps({"providers": [], "gateway_keys": []}).encode()).decode()
    try:
        cfg = asyncio.run(app_module._load_bootstrap())
        assert cfg == {"providers": [], "gateway_keys": []}
    finally:
        settings.bootstrap, settings.bootstrap_url = old_c, old_u


def test_load_bootstrap_from_url(app_module, upstream):
    base = upstream["openai"].rsplit("/v1", 1)[0]
    old_c, old_u = settings.bootstrap, settings.bootstrap_url
    settings.bootstrap = ""
    settings.bootstrap_url = base + "/bootstrap.json"
    try:
        cfg = asyncio.run(app_module._load_bootstrap())
        assert isinstance(cfg, dict) and "providers" in cfg
    finally:
        settings.bootstrap, settings.bootstrap_url = old_c, old_u


def test_bootstrap_b64_endpoint(client, admin_h):
    r = client.get("/admin/api/bootstrap-b64", headers=admin_h)
    assert r.status_code == 200
    data = json.loads(base64.b64decode(r.json()["b64"]).decode())
    assert "providers" in data and "vmodels" in data


def test_persist_puts_config(app_module, upstream):
    old_u, old_t = settings.persist_url, settings.persist_token
    settings.persist_url = upstream["base"] + "/persist"
    settings.persist_token = "tok"
    before = len(upstream["persisted"])
    try:
        asyncio.run(app_module.persist_config())
    finally:
        settings.persist_url, settings.persist_token = old_u, old_t
    assert len(upstream["persisted"]) == before + 1
    assert "providers" in upstream["persisted"][-1]


def test_mutation_triggers_persist(client, admin_h, app_module, upstream):
    import time
    old_u = settings.persist_url
    settings.persist_url = upstream["base"] + "/persist"
    before = len(upstream["persisted"])
    try:
        client.post("/admin/api/providers", headers=admin_h,
                    json={"name": "persist-prov", "base_url": "https://x.example/v1"})
        for _ in range(50):  # wait out the 2s debounce (up to 5s)
            if len(upstream["persisted"]) > before:
                break
            time.sleep(0.1)
    finally:
        settings.persist_url = old_u
    assert len(upstream["persisted"]) > before

