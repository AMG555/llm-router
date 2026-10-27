"""Routing strategies, sticky sessions, escape hatch and config portability."""
import base64
import json


def _chat(client, gw_h, model, headers_extra=None, **extra):
    h = dict(gw_h)
    if headers_extra:
        h.update(headers_extra)
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    body.update(extra)
    return client.post("/v1/chat/completions", headers=h, json=body)


def test_priority_strategy_is_strict(client, gw_h, make_model):
    m = make_model([("pbad", "429", 1), ("pgood", "ok", 2)], strategy="priority")
    r = _chat(client, gw_h, m["name"])
    assert r.status_code == 200
    assert r.headers["x-router-key"] == "pgood"
    assert r.headers["x-router-attempts"] == "2"


def test_sticky_session_pins_key(client, gw_h, make_model):
    m = make_model([("stk1", "ok", 100), ("stk2", "ok", 100)], sticky=True)
    r1 = _chat(client, gw_h, m["name"], headers_extra={"x-session-id": "conv-42"})
    r2 = _chat(client, gw_h, m["name"], headers_extra={"x-session-id": "conv-42"})
    assert r1.headers["x-router-key"] == r2.headers["x-router-key"]


def test_balanced_spreads_across_keys(client, gw_h, make_model):
    m = make_model([("sp1", "ok", 100), ("sp2", "ok", 100)], sticky=False)
    seen = {_chat(client, gw_h, m["name"]).headers["x-router-key"] for _ in range(10)}
    assert seen == {"sp1", "sp2"}


def test_provider_model_escape_hatch(client, gw_h, make_model):
    m = make_model([("esc", "ok", 100)])
    r = _chat(client, gw_h, f"{m['pid'] and 'p-' + m['name']}:mock-model")
    assert r.status_code == 200
    assert r.headers["x-router-provider"] == f"p-{m['name']}"


def test_import_export_round_trip(client, admin_h, gw_h, make_model):
    make_model([("ie1", "ok", 100)])
    cfg = client.get("/admin/api/export?reveal=1", headers=admin_h).json()
    client.post("/admin/api/import?wipe=1", headers=admin_h, json=cfg)
    st = client.get("/admin/api/state", headers=admin_h).json()
    assert len(st["providers"]) == len(cfg["providers"])
    assert len(st["vmodels"]) == len(cfg["vmodels"])


def test_bootstrap_rebuilds_config(client, admin_h, gw_h, make_model, app_module):
    make_model([("bs1", "ok", 100)])
    cfg = client.get("/admin/api/export?reveal=1", headers=admin_h).json()
    boot = json.loads(base64.b64decode(base64.b64encode(json.dumps(cfg).encode())))
    for t in ("targets", "vmodels", "api_keys", "providers"):
        app_module.store.conn.execute(f"DELETE FROM {t}")
    app_module.store.conn.commit()
    app_module.store.load()
    app_module.import_config(boot)
    assert len(app_module.store.vmodels) == len(cfg["vmodels"])


def test_postcompletion_prompt_shim(client, gw_h, make_model):
    m = make_model([("cmp", "ok", 100)])
    r = client.post("/v1/completions", headers=gw_h,
                    json={"model": m["name"], "prompt": "hello", "max_tokens": 5})
    assert r.status_code == 200


def test_embeddings_endpoint(client, gw_h, make_model):
    m = make_model([("emb", "ok", 100)])
    r = client.post("/v1/embeddings", headers=gw_h,
                    json={"model": m["name"], "input": "hello"})
    assert r.status_code == 200 and r.json()["data"][0]["embedding"]


def test_unknown_model_shape(client, gw_h):
    r = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": "nope", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
