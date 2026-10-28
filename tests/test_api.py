"""API surface, auth, hardening headers and input validation."""


def test_health_endpoints(client):
    for path in ("/health", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200 and r.json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"


def test_openapi_disabled_by_default(client):
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in {k.lower() for k in r.headers}
    assert r.headers["referrer-policy"] == "no-referrer"


def test_gateway_requires_key(client):
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_gateway_accepts_valid_key(client, gw_h):
    assert client.get("/v1/models", headers=gw_h).status_code == 200


def test_env_gateway_key_registration(client, app_module):
    from app.main import register_gateway_key
    k = "sk-router-envkey-test-123456"
    assert register_gateway_key(k, "env") is True
    assert k in app_module.store.gateway
    assert register_gateway_key(k, "env") is False  # idempotent
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {k}"}).status_code == 200


def test_admin_requires_token(client):
    assert client.get("/admin/api/state").status_code == 401
    assert client.get("/admin/api/state", headers={"x-admin-token": "nope"}).status_code == 401


def test_stream_token_checked(client):
    assert client.get("/admin/api/stream?token=bad").status_code == 401


def test_provider_validation(client, admin_h):
    assert client.post("/admin/api/providers", headers=admin_h,
                       json={"name": "x", "base_url": "ftp://nope"}).status_code == 400
    assert client.post("/admin/api/providers", headers=admin_h,
                       json={"base_url": "https://ok.example/v1"}).status_code == 400
    r = client.post("/admin/api/providers", headers=admin_h,
                    json={"name": "dup", "base_url": "https://ok.example/v1"})
    assert r.status_code == 200
    assert client.post("/admin/api/providers", headers=admin_h,
                       json={"name": "dup", "base_url": "https://ok.example/v1"}).status_code == 400


def test_vmodel_strategy_validated(client, admin_h):
    r = client.post("/admin/api/vmodels", headers=admin_h,
                    json={"name": "badstrat", "strategy": "explode"})
    assert r.status_code == 400


def test_body_size_limit(client, gw_h):
    big = {"model": "x", "messages": [{"role": "user", "content": "a" * (3 * 1024 * 1024)}]}
    r = client.post("/v1/chat/completions", headers=gw_h, json=big)
    assert r.status_code == 413


def test_logs_clear(client, admin_h):
    assert client.delete("/admin/api/logs", headers=admin_h).status_code == 200


def test_key_requires_existing_provider(client, admin_h):
    r = client.post("/admin/api/keys", headers=admin_h,
                    json={"provider_id": 999999, "api_key": "sk-x"})
    assert r.status_code == 400


def test_target_requires_existing_model_and_provider(client, admin_h, make_model):
    m = make_model([("ref-check", "ok", 100)])
    assert client.post("/admin/api/targets", headers=admin_h, json={
        "vmodel_id": 999999, "provider_id": m["pid"], "upstream_model": "x"}).status_code == 400
    assert client.post("/admin/api/targets", headers=admin_h, json={
        "vmodel_id": m["vid"], "provider_id": 999999, "upstream_model": "x"}).status_code == 400


def test_target_key_must_belong_to_provider(client, admin_h, make_model):
    a = make_model([("owner-a", "ok", 100)])
    b = make_model([("owner-b", "ok", 100)])
    r = client.post("/admin/api/targets", headers=admin_h, json={
        "vmodel_id": b["vid"], "provider_id": b["pid"], "key_id": a["keys"][0],
        "upstream_model": "x"})
    assert r.status_code == 400
