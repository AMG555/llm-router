"""Deterministic failover tests using a local mock upstream.

Run from the project root (venv active):

    python tests/test_failover.py

No external network or real API keys required.
"""
import json
import os
import sys
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.getcwd())
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "router_test.db")

BEHAVIOR = {}  # api_key -> ("ok"|"429"|"500"|"401"|"404", label)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        h = self.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else h

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "mock-model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        key = self._auth()
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) if n else b"{}")
        except ValueError:
            body = {}
        mode, label = BEHAVIOR.get(key, ("ok", key))
        if mode == "429":
            self.send_response(429)
            self.send_header("retry-after", "20")
            self.end_headers()
            return
        if mode == "500":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "401":
            self.send_response(401)
            self.end_headers()
            return
        if mode == "404":
            self.send_response(404)
            self.end_headers()
            return
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
        self._json(200, {"id": "c", "object": "chat.completion", "created": int(time.time()),
                         "model": "mock",
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": f"OK-{label}"}}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

from fastapi.testclient import TestClient  # noqa: E402
import app.main as m  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}/v1"
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def new_model(c, ah, name, keys, strategy="balanced"):
    pid = c.post("/admin/api/providers", headers=ah, json={
        "preset": "custom", "name": f"p-{name}", "adapter": "openai",
        "base_url": BASE}).json()["id"]
    kids = []
    for label, mode, pr in keys:
        BEHAVIOR[label] = (mode, label)
        kids.append(c.post("/admin/api/keys", headers=ah, json={
            "provider_id": pid, "label": label, "api_key": label, "priority": pr}).json()["id"])
    vid = c.post("/admin/api/vmodels", headers=ah, json={
        "name": name, "strategy": strategy, "sticky": False}).json()["id"]
    c.post("/admin/api/targets", headers=ah, json={
        "vmodel_id": vid, "provider_id": pid, "key_id": None, "upstream_model": "mock-model"})
    return vid, kids


with TestClient(m.app) as c:
    gk = list(m.store.gateway)[0]
    h = {"Authorization": f"Bearer {gk}"}
    ah = {"x-admin-token": "test-admin"}

    vid, kids = new_model(c, ah, "failover", [("bad", "429", 1), ("good", "ok", 100)])
    r = c.post("/v1/chat/completions", headers=h, json={
        "model": "failover", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    check("429 -> failover to next key", r.status_code == 200
          and "OK-good" in r.json()["choices"][0]["message"]["content"]
          and r.headers.get("x-router-attempts") == "2", str(r.json())[:120])
    check("bad key cooled, good key not", m.limiter.kstate(f"k{kids[0]}").snapshot(time.time())["cooldown"] > 0
          and m.limiter.kstate(f"k{kids[1]}").snapshot(time.time())["cooldown"] == 0)

    r = c.post("/v1/chat/completions", headers=h, json={
        "model": "failover", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    check("subsequent request uses healthy key in 1 attempt",
          r.status_code == 200 and r.headers.get("x-router-attempts") == "1")

    new_model(c, ah, "allbad", [("b1", "429", 1), ("b2", "429", 2)])
    r = c.post("/v1/chat/completions", headers=h, json={
        "model": "allbad", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    check("all keys 429 -> gateway 429 no_capacity",
          r.status_code == 429 and r.json()["error"]["code"] == "no_capacity")

    for nm, mode in [("fivethenok", "500"), ("auththenok", "401"), ("notfoundthenok", "404")]:
        new_model(c, ah, nm, [("x1", mode, 1), ("x2", "ok", 2)])
        r = c.post("/v1/chat/completions", headers=h, json={
            "model": nm, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        check(f"{mode} -> failover to healthy key",
              r.status_code == 200 and "OK-x2" in r.json()["choices"][0]["message"]["content"])

    new_model(c, ah, "streamfail", [("s1", "429", 1), ("s2", "ok", 2)])
    with c.stream("POST", "/v1/chat/completions", headers=h, json={
            "model": "streamfail", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5, "stream": True}) as resp:
        text, done = "", False
        for line in resp.iter_lines():
            if line.startswith("data:"):
                p = line[5:].strip()
                if p == "[DONE]":
                    done = True
                else:
                    try:
                        text += (json.loads(p).get("choices") or [{}])[0].get("delta", {}).get("content", "") or ""
                    except Exception:
                        pass
        check("streaming fails over before first byte", resp.status_code == 200 and done and "stream ok" in text)

    BEHAVIOR["p_bad"] = ("429", "p_bad")
    BEHAVIOR["p_good"] = ("ok", "p_good")
    pid = c.post("/admin/api/providers", headers=ah, json={"preset": "custom", "name": "p-prio",
                 "adapter": "openai", "base_url": BASE}).json()["id"]
    c.post("/admin/api/keys", headers=ah, json={"provider_id": pid, "label": "p_bad",
           "api_key": "p_bad", "priority": 1, "weight": 0.1})
    c.post("/admin/api/keys", headers=ah, json={"provider_id": pid, "label": "p_good",
           "api_key": "p_good", "priority": 2, "weight": 99})
    vid = c.post("/admin/api/vmodels", headers=ah, json={"name": "prio", "strategy": "priority",
                 "sticky": False}).json()["id"]
    c.post("/admin/api/targets", headers=ah, json={"vmodel_id": vid, "provider_id": pid,
           "key_id": None, "upstream_model": "mock-model"})
    r = c.post("/v1/chat/completions", headers=h, json={"model": "prio",
               "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    check("priority strategy enforces strict order",
          r.status_code == 200 and r.headers.get("x-router-key") == "p_good"
          and r.headers.get("x-router-attempts") == "2")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
srv.shutdown()
sys.exit(1 if fails else 0)
