"""A/B tests: weighted traffic split and strategy comparison."""


def _build(client, admin_h, upstream, weights, name):
    pid = client.post("/admin/api/providers", headers=admin_h, json={
        "preset": "custom", "name": f"p-{name}", "adapter": "openai",
        "base_url": upstream["openai"]}).json()["id"]
    labels = []
    for i, w in enumerate(weights):
        lbl = f"{name}-k{i}"
        client.post("/admin/api/keys", headers=admin_h, json={
            "provider_id": pid, "label": lbl, "api_key": lbl, "weight": w})
        labels.append(lbl)
    vid = client.post("/admin/api/vmodels", headers=admin_h, json={
        "name": name, "strategy": "balanced", "sticky": False}).json()["id"]
    client.post("/admin/api/targets", headers=admin_h, json={
        "vmodel_id": vid, "provider_id": pid, "key_id": None, "upstream_model": "mock-model"})
    return name, labels


def _run(client, gw_h, model, n):
    counts = {}
    for _ in range(n):
        r = client.post("/v1/chat/completions", headers=gw_h, json={
            "model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        counts[r.headers["x-router-key"]] = counts.get(r.headers["x-router-key"], 0) + 1
    return counts


def test_weighted_split_favours_higher_weight(client, admin_h, gw_h, upstream):
    name, labels = _build(client, admin_h, upstream, [1, 3], "ab-split")
    counts = _run(client, gw_h, name, 60)
    low, high = counts.get(labels[0], 0), counts.get(labels[1], 0)
    assert low + high == 60
    # the heavier key must receive materially more traffic
    assert high > low
    assert 1.4 < (high / max(low, 1)) < 6.0


def test_equal_weights_are_balanced(client, admin_h, gw_h, upstream):
    name, labels = _build(client, admin_h, upstream, [1, 1], "ab-even")
    counts = _run(client, gw_h, name, 40)
    low, high = counts.get(labels[0], 0), counts.get(labels[1], 0)
    assert low + high == 40
    assert 0.6 < (high / max(low, 1)) < 1.7


def test_strategy_variants_all_serve(client, admin_h, gw_h, upstream, make_model):
    for strat in ("balanced", "round_robin", "least_latency", "random", "priority"):
        m = make_model([(f"{strat}-a", "ok", 100), (f"{strat}-b", "ok", 100)], strategy=strat)
        r = client.post("/v1/chat/completions", headers=gw_h, json={
            "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        assert r.status_code == 200, strat
