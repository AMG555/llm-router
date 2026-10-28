"""RBAC: admin roles and audit trail."""


def _mk_admin(client, admin_h, role, label):
    r = client.post("/admin/api/admin-tokens", headers=admin_h, json={"label": label, "role": role})
    assert r.status_code == 200, r.text
    return r.json()


def test_viewer_can_read_but_not_write(client, admin_h):
    v = _mk_admin(client, admin_h, "viewer", "viewer-1")
    h = {"x-admin-token": v["token"]}
    r = client.get("/admin/api/state", headers=h)
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    assert client.post("/admin/api/providers", headers=h,
                       json={"name": "nope", "base_url": "https://x/v1"}).status_code == 403


def test_admin_role_can_write(client, admin_h):
    a = _mk_admin(client, admin_h, "admin", "helper")
    h = {"x-admin-token": a["token"]}
    r = client.post("/admin/api/providers", headers=h,
                    json={"name": "rbac-prov", "base_url": "https://api.example.com/v1"})
    assert r.status_code == 200
    audit = client.get("/admin/api/audit", headers=admin_h).json()["audit"]
    assert any(e["actor"] == "helper" and e["action"] == "POST"
               and "providers" in e["target"] for e in audit)


def test_revoked_token_is_rejected(client, admin_h):
    v = _mk_admin(client, admin_h, "viewer", "temp")
    h = {"x-admin-token": v["token"]}
    assert client.get("/admin/api/state", headers=h).status_code == 200
    client.delete(f"/admin/api/admin-tokens/{v['id']}", headers=admin_h)
    assert client.get("/admin/api/state", headers=h).status_code == 401


def test_super_admin_token_bypasses_rbac(client, admin_h):
    assert client.get("/admin/api/state", headers=admin_h).json()["role"] == "admin"


def test_invalid_role_rejected(client, admin_h):
    assert client.post("/admin/api/admin-tokens", headers=admin_h,
                       json={"label": "x", "role": "wizard"}).status_code in (400, 422)
