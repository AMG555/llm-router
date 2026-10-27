"""Security unit tests: constant-time compare, lockout, rate limit, URL
validation, SQL column allow-list and encryption at rest."""
import pytest

from app.security import (LockoutGuard, SlidingRateLimiter, const_eq,
                          validate_upstream_url)


def test_const_eq():
    assert const_eq("abc", "abc")
    assert not const_eq("abc", "abd")
    assert not const_eq("abc", "abcd")
    assert not const_eq(None, "x")


def test_lockout_guard():
    g = LockoutGuard(max_fails=3, lock_seconds=300)
    for _ in range(3):
        g.fail("1.2.3.4")
    assert g.locked_for("1.2.3.4") > 0
    assert g.locked_for("9.9.9.9") == 0
    g.reset("1.2.3.4")
    assert g.locked_for("1.2.3.4") == 0


def test_sliding_rate_limiter():
    lim = SlidingRateLimiter(2, window=60)
    assert lim.allow("k") and lim.allow("k")
    assert not lim.allow("k")
    assert lim.allow("other")


def test_rate_limiter_unlimited():
    lim = SlidingRateLimiter(0)
    assert all(lim.allow("k") for _ in range(100))


def test_validate_upstream_url():
    assert validate_upstream_url("https://api.example.com/v1") == "https://api.example.com/v1"
    assert validate_upstream_url("http://host:8080/v1/") == "http://host:8080/v1"
    for bad in ["ftp://x", "example.com", "", "javascript:alert(1)", "https://"]:
        with pytest.raises(ValueError):
            validate_upstream_url(bad)


def test_validate_blocks_private_when_enabled():
    for bad in ["http://localhost/v1", "http://127.0.0.1:11434/v1", "http://10.0.0.5/v1"]:
        with pytest.raises(ValueError):
            validate_upstream_url(bad, block_private=True)
    assert validate_upstream_url("https://api.example.com", block_private=True)


def test_store_rejects_unknown_columns(app_module):
    with pytest.raises(ValueError):
        app_module.store.insert("api_keys", {"provider_id": 1, "api_key": "k",
                                             "evil) VALUES(1);--": "x"})


def test_api_rejects_bad_column_with_400(client, admin_h, make_model):
    m = make_model([("col", "ok", 100)])
    kid = m["keys"][0]
    r = client.patch(f"/admin/api/keys/{kid}", headers=admin_h, json={"id": 999})
    assert r.status_code == 400


def test_encryption_at_rest(app_module):
    raw = app_module.store.conn.execute(
        "SELECT api_key FROM api_keys ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert raw.startswith("enc:v1:")
    decrypted = list(app_module.store.keys.values())[0]["api_key"]
    assert not decrypted.startswith("enc:v1:")


def test_export_masks_by_default(client, admin_h, make_model):
    make_model([("maskme", "ok", 100)])
    data = client.get("/admin/api/export", headers=admin_h).json()
    assert all(("***" in k["api_key"]) or ("..." in k["api_key"]) for k in data["api_keys"])
