import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import util
from .config import settings
from .db import Store
from .limiter import Limiter
from .providers import PRESETS, preset_headers
from .router import NoCapacity, Router, UnknownModel

BASE = Path(__file__).parent
store = Store(settings.db_path)
limiter = Limiter(settings)
app = FastAPI(title="LLM Router", version="1.0.0")


class Bus:
    def __init__(self):
        self.subs = set()
        self.loop = None

    def bind(self, loop):
        self.loop = loop

    def subscribe(self):
        q = asyncio.Queue(maxsize=1000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def publish(self, item):
        item["ts"] = time.time()
        for q in list(self.subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass


bus = Bus()
router = Router(store, limiter, settings, bus)

app.add_middleware(CORSMiddleware, allow_origins=settings.cors.split(","),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _startup():
    bus.bind(asyncio.get_running_loop())
    if settings.bootstrap and not store.providers:
        try:
            cfg = json.loads(base64.b64decode(settings.bootstrap).decode())
            import_config(cfg)
        except Exception as e:
            print("bootstrap failed:", e)
    if not store.gateway:
        key = "sk-router-" + secrets.token_urlsafe(24)
        store.insert("gateway_keys", {"label": "default", "key": key,
                                      "enabled": 1, "created_at": time.time()})
        print(f"\n=== NEW GATEWAY KEY: {key} ===\n")
    store.prune_logs(settings.max_log_rows)


@app.on_event("shutdown")
async def _shutdown():
    await router.aclose()


# ---------------- auth ----------------
def openai_error(message, code="invalid_request_error", status=400, type_="invalid_request_error"):
    return JSONResponse({"error": {"message": message, "type": type_, "code": code}}, status_code=status)


def gateway_auth(request: Request):
    if not settings.require_auth:
        return "open"
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token = token or request.headers.get("x-api-key", "")
    if not token:
        raise HTTPException(401, "Missing API key")
    if token in store.gateway or token == settings.admin_token:
        return token
    raise HTTPException(401, "Invalid API key")


def admin_auth(request: Request):
    if not settings.admin_token:
        return True
    token = (request.headers.get("x-admin-token")
             or request.headers.get("authorization", "").replace("Bearer ", ""))
    if token != settings.admin_token:
        raise HTTPException(401, "Invalid admin token")
    return True


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1"):
        return openai_error(str(exc.detail), code=str(exc.status_code), status=exc.status_code,
                            type_="authentication_error" if exc.status_code == 401 else "invalid_request_error")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------- OpenAI-compatible surface ----------------
@app.get("/health")
async def health():
    return {"status": "ok", "models": len(store.vmodels), "providers": len(store.providers),
            "keys": len(store.keys), "time": time.time()}


@app.get("/v1/models")
async def models(_=Depends(gateway_auth)):
    return {"object": "list", "data": [
        {"id": v["name"], "object": "model", "created": int(v["created_at"] or 0),
         "owned_by": "llm-router", "description": v["description"]}
        for v in store.vmodels.values() if v["enabled"]]}


async def _run(request: Request, stream_forced=False):
    body = await request.json()
    stream = bool(body.get("stream")) or stream_forced
    session = (request.headers.get("x-session-id")
               or request.headers.get("x-route-sticky")
               or body.get("user"))
    if session is not None and not isinstance(session, str):
        session = json.dumps(session, sort_keys=True)
    try:
        res = await router.execute(body.get("model"), body, stream=stream, session=session)
    except UnknownModel as e:
        return openai_error(str(e), code="model_not_found", status=404)
    except NoCapacity as e:
        return openai_error(str(e), code="no_capacity", status=429, type_="rate_limit_error")

    if res.stream is None:
        return JSONResponse(res.data, status_code=res.status,
                            headers=res.headers)
    return StreamingResponse(res.stream, media_type="text/event-stream",
                             headers={**res.headers, "Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.post("/v1/chat/completions")
async def chat(request: Request, _=Depends(gateway_auth)):
    return await _run(request)


@app.post("/v1/completions")
async def completions(request: Request, _=Depends(gateway_auth)):
    body = await request.json()
    if body.get("prompt") is not None and not body.get("messages"):
        body["messages"] = [{"role": "user", "content": body["prompt"]}]
    return await _run(request)


@app.post("/v1/embeddings")
async def embeddings(request: Request, _=Depends(gateway_auth)):
    body = await request.json()
    name = body.get("model")
    try:
        vm, cands = router.resolve(name)
    except UnknownModel as e:
        return openai_error(str(e), code="model_not_found", status=404)
    for cand in router.order(cands, "balanced", 0, None):
        if not limiter.acquire(cand):
            continue
        from .adapters import get_adapter
        adapter = get_adapter(cand["adapter"])
        if not adapter.supports_embeddings:
            limiter.release(cand, 0, 599)
            continue
        try:
            url, headers, payload, _ = adapter.build_embeddings(cand, body)
            r = await router.client.post(url, json=payload, headers=headers)
        except Exception:
            limiter.cool(cand, 5, "network")
            continue
        limiter.release(cand, 0, r.status_code)
        if r.status_code == 429:
            limiter.cool(cand, util.parse_retry_after(r.headers, 20), "429")
            continue
        if r.status_code >= 400:
            continue
        data = r.json()
        data["model"] = name
        return JSONResponse(data)
    return openai_error("No embedding-capable target available", status=503)


# ---------------- admin API ----------------
@app.get("/admin/api/state")
async def admin_state(_=Depends(admin_auth)):
    health = limiter.health()
    logs = store.rows("logs", "1=1 ORDER BY id DESC LIMIT 50")
    return {
        "providers": [{**p, "key_count": len(store.keys_by_provider.get(p["id"], []))}
                      for p in store.providers.values()],
        "keys": [{**{k: v for k, v in key.items() if k != "api_key"},
                  "masked": util.mask(key["api_key"]),
                  "health": health["keys"].get(f"k{key['id']}", {})}
                 for key in store.keys.values()],
        "vmodels": [{**v, "targets": [t for t in store.targets if t["vmodel_id"] == v["id"]]}
                    for v in store.vmodels.values()],
        "gateway_keys": [{**g, "masked": util.mask(g["key"])} for g in store.rows("gateway_keys")],
        "presets": PRESETS,
        "logs": logs,
        "stats": _stats(),
        "health": health,
    }


def _stats():
    rows = store.rows("logs", "ts > ?", (time.time() - 300,))
    ok = sum(1 for r in rows if (r["status"] or 500) < 400)
    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    return {
        "requests_5m": len(rows),
        "success_rate": round(100 * ok / len(rows), 2) if rows else 100.0,
        "avg_latency": int(sum(lat) / len(lat)) if lat else 0,
        "keys_total": len(store.keys),
        "keys_enabled": sum(1 for k in store.keys.values() if k["enabled"]),
        "models": len(store.vmodels),
    }


@app.get("/admin/api/health")
async def admin_health(_=Depends(admin_auth)):
    return limiter.health()


@app.post("/admin/api/providers")
async def add_provider(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if name in {p["name"] for p in store.providers.values()}:
        raise HTTPException(400, "provider name already exists")
    preset = b.get("preset") or "custom"
    base_url = (b.get("base_url") or PRESETS.get(preset, {}).get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(400, "base_url required")
    headers = b.get("headers") or preset_headers(preset)
    pid = store.insert("providers", {
        "name": name,
        "adapter": b.get("adapter") or PRESETS.get(preset, {}).get("adapter", "openai"),
        "base_url": base_url,
        "headers": json.dumps(headers),
        "notes": b.get("notes", ""),
        "enabled": 1 if b.get("enabled", True) else 0,
        "created_at": time.time(),
    })
    return {"id": pid}


@app.patch("/admin/api/providers/{pid}")
async def edit_provider(pid: int, request: Request, _=Depends(admin_auth)):
    b = await request.json()
    if "headers" in b and isinstance(b["headers"], dict):
        b["headers"] = json.dumps(b["headers"])
    for k in ("enabled", "sticky"):
        if k in b:
            b[k] = 1 if b[k] else 0
    store.update("providers", pid, b)
    return {"ok": True}


@app.delete("/admin/api/providers/{pid}")
async def del_provider(pid: int, _=Depends(admin_auth)):
    store.delete("providers", pid)
    return {"ok": True}


@app.get("/admin/api/providers/{pid}/models")
async def provider_models(pid: int, _=Depends(admin_auth)):
    return {"models": await router.list_upstream_models(pid)}


@app.post("/admin/api/keys")
async def add_key(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    if not b.get("provider_id") or not b.get("api_key"):
        raise HTTPException(400, "provider_id and api_key required")
    kid = store.insert("api_keys", {
        "provider_id": int(b["provider_id"]),
        "label": b.get("label") or "",
        "api_key": b["api_key"].strip(),
        "enabled": 1 if b.get("enabled", True) else 0,
        "weight": float(b.get("weight") or 1),
        "priority": int(b.get("priority") or 100),
        "max_rpm": int(b.get("max_rpm") or 0),
        "max_tpm": int(b.get("max_tpm") or 0),
        "max_concurrency": int(b.get("max_concurrency") or 0),
        "created_at": time.time(),
    })
    return {"id": kid}


@app.patch("/admin/api/keys/{kid}")
async def edit_key(kid: int, request: Request, _=Depends(admin_auth)):
    b = await request.json()
    for k in ("enabled",):
        if k in b:
            b[k] = 1 if b[k] else 0
    for k in ("max_rpm", "max_tpm", "max_concurrency", "priority"):
        if k in b:
            b[k] = int(b[k] or 0)
    if "weight" in b:
        b["weight"] = float(b["weight"] or 1)
    store.update("api_keys", kid, b)
    return {"ok": True}


@app.delete("/admin/api/keys/{kid}")
async def del_key(kid: int, _=Depends(admin_auth)):
    store.delete("api_keys", kid)
    return {"ok": True}


@app.post("/admin/api/keys/{kid}/reset")
async def reset_key(kid: int, _=Depends(admin_auth)):
    st = limiter.kstate(f"k{kid}")
    st.cooldown = 0.0
    st.rpm.clear()
    st.tpm.clear()
    st.fails = 0
    st.last_error = ""
    return {"ok": True}


@app.post("/admin/api/vmodels")
async def add_vmodel(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    vid = store.insert("vmodels", {
        "name": name, "description": b.get("description", ""),
        "strategy": b.get("strategy") or "balanced",
        "sticky": 1 if b.get("sticky", True) else 0,
        "enabled": 1, "created_at": time.time(),
    })
    return {"id": vid}


@app.patch("/admin/api/vmodels/{vid}")
async def edit_vmodel(vid: int, request: Request, _=Depends(admin_auth)):
    b = await request.json()
    for k in ("enabled", "sticky"):
        if k in b:
            b[k] = 1 if b[k] else 0
    store.update("vmodels", vid, b)
    return {"ok": True}


@app.delete("/admin/api/vmodels/{vid}")
async def del_vmodel(vid: int, _=Depends(admin_auth)):
    store.delete("vmodels", vid)
    return {"ok": True}


@app.post("/admin/api/targets")
async def add_target(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    if not b.get("vmodel_id") or not b.get("provider_id") or not b.get("upstream_model"):
        raise HTTPException(400, "vmodel_id, provider_id, upstream_model required")
    tid = store.insert("targets", {
        "vmodel_id": int(b["vmodel_id"]),
        "provider_id": int(b["provider_id"]),
        "key_id": int(b["key_id"]) if b.get("key_id") else None,
        "upstream_model": b["upstream_model"].strip(),
        "weight": float(b.get("weight") or 1),
        "priority": int(b.get("priority") or 100),
        "max_rpm": int(b.get("max_rpm") or 0),
        "max_tpm": int(b.get("max_tpm") or 0),
        "enabled": 1, "created_at": time.time(),
    })
    return {"id": tid}


@app.patch("/admin/api/targets/{tid}")
async def edit_target(tid: int, request: Request, _=Depends(admin_auth)):
    b = await request.json()
    for k in ("enabled",):
        if k in b:
            b[k] = 1 if b[k] else 0
    for k in ("max_rpm", "max_tpm", "priority"):
        if k in b:
            b[k] = int(b[k] or 0)
    if "weight" in b:
        b["weight"] = float(b["weight"] or 1)
    if "key_id" in b:
        b["key_id"] = int(b["key_id"]) if b["key_id"] else None
    store.update("targets", tid, b)
    return {"ok": True}


@app.delete("/admin/api/targets/{tid}")
async def del_target(tid: int, _=Depends(admin_auth)):
    store.delete("targets", tid)
    return {"ok": True}


@app.post("/admin/api/gateway-keys")
async def add_gateway_key(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    key = b.get("key") or ("sk-router-" + secrets.token_urlsafe(24))
    gid = store.insert("gateway_keys", {"label": b.get("label") or "client",
                                        "key": key, "enabled": 1, "created_at": time.time()})
    return {"id": gid, "key": key}


@app.patch("/admin/api/gateway-keys/{gid}")
async def edit_gateway_key(gid: int, request: Request, _=Depends(admin_auth)):
    b = await request.json()
    if "enabled" in b:
        b["enabled"] = 1 if b["enabled"] else 0
    store.update("gateway_keys", gid, b)
    return {"ok": True}


@app.delete("/admin/api/gateway-keys/{gid}")
async def del_gateway_key(gid: int, _=Depends(admin_auth)):
    store.delete("gateway_keys", gid)
    return {"ok": True}


@app.post("/admin/api/test")
async def test_route(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    model = b.get("model")
    if not model:
        vms = list(store.vmodels)
        if not vms:
            raise HTTPException(400, "no virtual models configured")
        model = vms[0]
    body = {"model": model, "messages": [{"role": "user",
            "content": b.get("prompt") or "Reply with exactly: pong"}], "max_tokens": 32}
    t0 = time.time()
    try:
        res = await router.execute(model, body, stream=False)
    except (NoCapacity, UnknownModel) as e:
        return {"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)}
    text = ""
    try:
        text = res.data["choices"][0]["message"]["content"]
    except Exception:
        text = json.dumps(res.data)[:300]
    return {"ok": True, "provider": res.provider, "key": res.key_label,
            "attempts": res.attempts, "ms": int((time.time() - t0) * 1000),
            "reply": text}


@app.get("/admin/api/logs")
async def get_logs(limit: int = 200, _=Depends(admin_auth)):
    return {"logs": store.rows("logs", "1=1 ORDER BY id DESC LIMIT ?", (min(limit, 2000),))}


@app.delete("/admin/api/logs")
async def clear_logs(_=Depends(admin_auth)):
    store.conn.execute("DELETE FROM logs")
    store.conn.commit()
    return {"ok": True}


@app.get("/admin/api/stream")
async def stream(request: Request, token: str = ""):
    if settings.admin_token and token != settings.admin_token:
        raise HTTPException(401, "bad token")
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield "data: " + json.dumps(item) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/admin/api/export")
async def export(reveal: int = 1, _=Depends(admin_auth)):
    data = {
        "providers": store.rows("providers"),
        "api_keys": store.rows("api_keys"),
        "vmodels": store.rows("vmodels"),
        "targets": store.rows("targets"),
        "gateway_keys": store.rows("gateway_keys"),
    }
    if not reveal:
        for k in data["api_keys"]:
            k["api_key"] = util.mask(k["api_key"])
    return data


def import_config(cfg: dict):
    idmap = {}
    for p in cfg.get("providers", []):
        old = p.pop("id", None)
        p.setdefault("created_at", time.time())
        idmap[("providers", old)] = store.insert("providers", p)
    for k in cfg.get("api_keys", []):
        old = k.pop("id", None)
        k["provider_id"] = idmap.get(("providers", k["provider_id"]), k["provider_id"])
        k.setdefault("created_at", time.time())
        idmap[("api_keys", old)] = store.insert("api_keys", k)
    for v in cfg.get("vmodels", []):
        old = v.pop("id", None)
        v.setdefault("created_at", time.time())
        idmap[("vmodels", old)] = store.insert("vmodels", v)
    for t in cfg.get("targets", []):
        t.pop("id", None)
        t["vmodel_id"] = idmap.get(("vmodels", t["vmodel_id"]), t["vmodel_id"])
        t["provider_id"] = idmap.get(("providers", t["provider_id"]), t["provider_id"])
        if t.get("key_id"):
            t["key_id"] = idmap.get(("api_keys", t["key_id"]), t["key_id"])
        t.setdefault("created_at", time.time())
        store.insert("targets", t)
    for g in cfg.get("gateway_keys", []):
        g.pop("id", None)
        g.setdefault("created_at", time.time())
        try:
            store.insert("gateway_keys", g)
        except Exception:
            pass


@app.post("/admin/api/import")
async def do_import(request: Request, wipe: int = 0, _=Depends(admin_auth)):
    cfg = await request.json()
    if wipe:
        for t in ("targets", "vmodels", "api_keys", "providers"):
            store.conn.execute(f"DELETE FROM {t}")
        store.conn.commit()
        store.load()
    import_config(cfg)
    return {"ok": True, "providers": len(store.providers), "models": len(store.vmodels)}


# ---------------- static UI ----------------
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(str(BASE / "static" / "index.html"))
