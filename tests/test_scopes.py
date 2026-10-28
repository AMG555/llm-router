"""Per-gateway-key scopes and quotas."""


def _new_key(client, admin_h, **body):
    r = client.post("/admin/api/gateway-keys", headers=admin_h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _chat(client, key, model):
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 5})


def test_model_allowlist_enforced(client, admin_h, make_model):
    m = make_model([("scope-ok", "ok", 100)])
    key = _new_key(client, admin_h, label="scoped", allowed_models=[m["name"]])
    assert _chat(client, key, m["name"]).status_code == 200
    r = _chat(client, key, "some-other-model")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "model_forbidden"


def test_empty_allowlist_permits_all(client, admin_h, make_model):
    m = make_model([("scope-any", "ok", 100)])
    key = _new_key(client, admin_h, label="open")
    assert _chat(client, key, m["name"]).status_code == 200


def test_rpm_quota_enforced(client, admin_h, make_model):
    m = make_model([("scope-rpm", "ok", 100)])
    key = _new_key(client, admin_h, label="quota", max_rpm=1)
    assert _chat(client, key, m["name"]).status_code == 200
    assert _chat(client, key, m["name"]).status_code == 429


def test_state_exposes_scopes(client, admin_h):
    _new_key(client, admin_h, label="shown", allowed_models="a,b", max_rpm=7)
    rows = client.get("/admin/api/state", headers=admin_h).json()["gateway_keys"]
    row = next(g for g in rows if g["label"] == "shown")
    assert row["allowed_models"] == "a,b"
    assert row["max_rpm"] == 7


def test_scopes_editable(client, admin_h):
    gid = client.post("/admin/api/gateway-keys", headers=admin_h,
                      json={"label": "edit-me"}).json()["id"]
    r = client.patch(f"/admin/api/gateway-keys/{gid}", headers=admin_h,
                     json={"allowed_models": ["x", "y"], "max_rpm": 3})
    assert r.status_code == 200
    row = next(g for g in client.get("/admin/api/state", headers=admin_h).json()["gateway_keys"]
               if g["id"] == gid)
    assert row["allowed_models"] == "x,y"
    assert row["max_rpm"] == 3
