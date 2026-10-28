"""Observability: request IDs, structured logs, Prometheus metrics."""
import json
import re

from app import observability
from app.config import settings
from app.observability import log_event, route_label


def test_route_label_is_bounded():
    assert route_label("/v1/chat/completions") == "/v1/chat/completions"
    assert route_label("/admin/api/keys/5") == "/admin/api/*"
    assert route_label("/static/app.js") == "/static/*"
    assert route_label("/whatever/123") == "other"


def test_request_id_echoed(client):
    r = client.get("/health", headers={"x-request-id": "trace-abc"})
    assert r.headers.get("x-request-id") == "trace-abc"


def test_request_id_generated_when_absent(client):
    r = client.get("/health")
    rid = r.headers.get("x-request-id")
    assert rid and re.fullmatch(r"[0-9a-f]{32}", rid)


def test_log_event_json(monkeypatch, capsys):
    monkeypatch.setattr(settings, "log_format", "json")
    log_event("info", "unit_test", foo="bar", n=3)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "unit_test" and rec["foo"] == "bar" and rec["level"] == "info"


def test_metrics_require_admin(client, admin_h):
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=admin_h).status_code == 200


def test_metrics_expose_red(client, admin_h, gw_h, make_model):
    make_model([("obs-ok", "ok", 100)])
    client.get("/health")
    client.post("/v1/chat/completions", headers=gw_h, json={
        "model": "does-not-exist", "messages": [{"role": "user", "content": "x"}]})
    text = client.get("/metrics", headers=admin_h).text
    assert "http_requests_total" in text
    assert "http_request_duration_seconds" in text
    assert 'route="/health"' in text
    assert "auth_failures_total" in text


def test_auth_failure_is_counted(client, admin_h):
    client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    text = client.get("/metrics", headers=admin_h).text
    assert 'auth_failures_total{surface="gateway"}' in text
