"""Shared pytest fixtures: isolated config, a local mock upstream that speaks
OpenAI, Anthropic and Gemini wire formats, and helpers to build models."""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Environment must be set before app.main is imported anywhere.
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-do-not-use-in-prod")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="llmrouter-test-"), "router.db")
os.environ["ENV"] = "test"
os.environ["CORS_ORIGINS"] = ""
os.environ["ENABLE_DOCS"] = "false"
os.environ["EGRESS_CHECK_URL"] = ""  # disable the egress public-IP watch in tests

sys.path.insert(0, os.getcwd())

# api_key -> (mode, label). Modes: ok, 429, 500, 401, 404, anthropic, anthropic-tool, gemini
BEHAVIOR = {}
PERSISTED = []


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _key(self):
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h):
                return self.headers[h]
        a = self.headers.get("authorization", "")
        return a[7:].strip() if a.lower().startswith("bearer ") else a

    def do_GET(self):
        if self.path.endswith("/bootstrap.json"):
            self._json({"providers": [], "gateway_keys": []})
        elif self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "mock-model"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        n = int(self.headers.get("content-length", 0))
        try:
            PERSISTED.append(json.loads(self.rfile.read(n) if n else b"{}"))
        except ValueError:
            PERSISTED.append({})
        self._json({"ok": True})

    def do_POST(self):
        key = self._key()
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) if n else b"{}")
        except ValueError:
            body = {}
        mode, label = BEHAVIOR.get(key, ("ok", key))

        if mode == "429":
            return self._empty(429, {"retry-after": "20"})
        if mode == "500":
            return self._empty(500)
        if mode == "401":
            return self._empty(401)
        if mode == "404":
            return self._empty(404)

        if self.path.endswith("/embeddings"):
            return self._json({"object": "list", "model": label,
                               "data": [{"object": "embedding", "index": 0,
                                         "embedding": [0.1, 0.2, 0.3]}],
                               "usage": {"prompt_tokens": 1, "total_tokens": 1}})

        if self.path.endswith("/messages") or mode.startswith("anthropic"):
            return self._anthropic(body, label, mode)
        if ":generateContent" in self.path or ":streamGenerateContent" in self.path or mode == "gemini":
            return self._gemini(body, label)

        return self._openai(body, label)

    # ---- wire formats ----
    def _openai(self, body, label):
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            created = int(time.time())

            def chunk(d, f=None):
                return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk",
                    "created": created, "model": "mock",
                    "choices": [{"index": 0, "delta": d, "finish_reason": f}]}) + "\n\n"

            self.wfile.write(chunk({"role": "assistant", "content": ""}).encode())
            for w in [f"[{label}]", " stream", " ok"]:
                self.wfile.write(chunk({"content": w}).encode())
                self.wfile.flush()
            self.wfile.write(chunk({}, "stop").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json({"id": "cmpl", "object": "chat.completion", "created": int(time.time()),
                    "model": "mock", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": f"OK-{label}"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    def _anthropic(self, body, label, mode):
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for ev in [
                {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
                {"type": "content_block_start", "content_block": {"type": "text"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi "}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "claude"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 4}},
                {"type": "message_stop"},
            ]:
                self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                self.wfile.flush()
            return
        content = [{"type": "text", "text": "hi from claude"}]
        stop = "end_turn"
        if mode == "anthropic-tool":
            content = [{"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                        "input": {"city": "Paris"}}]
            stop = "tool_use"
        self._json({"id": "msg_1", "type": "message", "role": "assistant", "content": content,
                    "stop_reason": stop, "usage": {"input_tokens": 7, "output_tokens": 4}})

    def _gemini(self, body, label):
        if ":streamGenerateContent" in self.path:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for obj in [
                {"candidates": [{"content": {"parts": [{"text": "hi "}], "role": "model"}}]},
                {"candidates": [{"content": {"parts": [{"text": "gemini"}], "role": "model"}}]},
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4,
                                   "totalTokenCount": 11}},
            ]:
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                self.wfile.flush()
            return
        self._json({"candidates": [{"content": {"parts": [{"text": "hi from gemini"}],
                                                "role": "model"}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4,
                                      "totalTokenCount": 11}})

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, code, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()


@pytest.fixture(scope="session")
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"openai": f"http://127.0.0.1:{port}/v1",
           "anthropic": f"http://127.0.0.1:{port}/v1",
           "gemini": f"http://127.0.0.1:{port}/v1beta",
           "base": f"http://127.0.0.1:{port}",
           "behavior": BEHAVIOR,
           "persisted": PERSISTED}
    srv.shutdown()


@pytest.fixture(scope="session")
def app_module():
    import app.main as m
    return m


@pytest.fixture(scope="session")
def client(app_module):
    from fastapi.testclient import TestClient
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def admin_h():
    return {"x-admin-token": os.environ["ADMIN_TOKEN"]}


@pytest.fixture(scope="session")
def gw_h(app_module):
    return {"Authorization": f"Bearer {list(app_module.store.gateway)[0]}"}


@pytest.fixture
def make_model(client, admin_h, upstream):
    """Factory: create a provider + N keys + a virtual model + a fan-out target."""
    import uuid

    def _make(keys, strategy="balanced", sticky=False, adapter="openai", name=None):
        name = name or f"m{uuid.uuid4().hex[:8]}"
        base = upstream[adapter]
        pid = client.post("/admin/api/providers", headers=admin_h, json={
            "preset": "custom", "name": f"p-{name}", "adapter": adapter,
            "base_url": base}).json()["id"]
        kids = []
        for label, mode, pr in keys:
            BEHAVIOR[label] = (mode, label)
            kids.append(client.post("/admin/api/keys", headers=admin_h, json={
                "provider_id": pid, "label": label, "api_key": label,
                "priority": pr}).json()["id"])
        vid = client.post("/admin/api/vmodels", headers=admin_h, json={
            "name": name, "strategy": strategy, "sticky": sticky}).json()["id"]
        client.post("/admin/api/targets", headers=admin_h, json={
            "vmodel_id": vid, "provider_id": pid, "key_id": None,
            "upstream_model": "mock-model"})
        return {"name": name, "vid": vid, "pid": pid, "keys": kids}

    return _make


def chat(client, gw_h, model, stream=False, **extra):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    body.update(extra)
    if stream:
        body["stream"] = True
    return client.post("/v1/chat/completions", headers=gw_h, json=body)
