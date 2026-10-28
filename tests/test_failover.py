"""Failover, cooldown isolation and error-path behaviour (mock upstream)."""
import asyncio
import time


def _content(r):
    return r.json()["choices"][0]["message"]["content"]


def test_429_fails_over_and_only_cools_bad_key(client, gw_h, make_model, app_module):
    m = make_model([("bad", "429", 1), ("good", "ok", 100)])
    r = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    assert r.status_code == 200
    assert "OK-good" in _content(r)
    assert r.headers["x-router-attempts"] == "2"
    assert r.headers["x-router-key"] == "good"
    be = app_module.limiter.backend
    bad_cd = asyncio.run(be.cooldown_until("k", f"k{m['keys'][0]}"))
    good_cd = asyncio.run(be.cooldown_until("k", f"k{m['keys'][1]}"))
    assert bad_cd > time.time()
    assert good_cd == 0


def test_target_state_not_cooled_by_key_rate_limit(client, gw_h, make_model, app_module):
    make_model([("bad2", "429", 1), ("good2", "ok", 100)])
    t = app_module.store.rows("targets")[-1]
    cd = asyncio.run(app_module.limiter.backend.cooldown_until("t", f"t{t['id']}"))
    assert cd == 0


def test_second_request_uses_healthy_key(client, gw_h, make_model):
    m = make_model([("bad3", "429", 1), ("good3", "ok", 100)])
    r1 = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    r2 = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    assert r1.headers["x-router-attempts"] == "2"
    assert r2.headers["x-router-attempts"] == "1"
    assert r2.headers["x-router-key"] == "good3"


def test_all_keys_limited_returns_429(client, gw_h, make_model):
    m = make_model([("b1", "429", 1), ("b2", "429", 2)])
    r = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "no_capacity"


def test_5xx_auth_404_all_fail_over(client, gw_h, make_model):
    for mode in ("500", "401", "404"):
        m = make_model([("x1", mode, 1), ("x2", "ok", 2)])
        r = client.post("/v1/chat/completions", headers=gw_h, json={
            "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        assert r.status_code == 200, mode
        assert "OK-x2" in _content(r), mode


def test_streaming_fails_over_before_first_byte(client, gw_h, make_model):
    m = make_model([("s1", "429", 1), ("s2", "ok", 2)])
    text, done = "", False
    with client.stream("POST", "/v1/chat/completions", headers=gw_h, json={
            "model": m["name"], "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5, "stream": True}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data:"):
                p = line[5:].strip()
                if p == "[DONE]":
                    done = True
                else:
                    import json
                    try:
                        text += (json.loads(p).get("choices") or [{}])[0].get("delta", {}).get("content", "") or ""
                    except Exception:
                        pass
    assert done and "stream ok" in text
