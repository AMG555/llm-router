import asyncio
import base64
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import util
from .config import settings
from .db import Store
from .limiter import Limiter
from .providers import PRESETS, preset_headers
from .router import NoCapacity, Router, UnknownModel
from .security import (SECURITY_HEADERS, LockoutGuard, SlidingRateLimiter,
                       const_eq, validate_upstream_url)

BASE = Path(__file__).parent
store = Store(settings.db_path)
limiter = Limiter(settings)
admin_guard = LockoutGuard(settings.admin_max_fails, settings.admin_lock_seconds)
gw_limiter = SlidingRateLimiter(settings.gateway_rpm)

@asynccontextmanager
async def lifespan(app):
    bus.bind(asyncio.get_running_loop())
    if settings.bootstrap and not store.providers:
        try:
            cfg = json.loads(base64.b64decode(settings.bootstrap).decode())
            import_config(cfg)
        except Exception as e:
            print("bootstrap failed:", e)
    if settings.gateway_key:
        if register_gateway_key(settings.gateway_key, "env"):
            print("[gateway] registered GATEWAY_KEY from environment")
    if not store.gateway:
        key = "sk-router-" + secrets.token_urlsafe(24)
        store.insert("gateway_keys", {"label": "default", "key": key,
                                      "enabled": 1, "created_at": time.time()})
        print(f"\n=== NEW GATEWAY KEY: {key} ===\n")
    if settings.admin_token_generated:
        print(f"\n=== GENERATED ADMIN TOKEN: {settings.admin_token} ===")
        print("Set ADMIN_TOKEN to a fixed value to keep it stable across restarts.\n")
    if not settings.secret_key:
        print("[warn] SECRET_KEY not set - stored upstream keys are NOT encrypted at rest.")
    print(f"[security] env={settings.env} docs={'on' if settings.enable_docs else 'off'} "
          f"cors={'on' if settings.cors else 'same-origin'} gateway_rpm={settings.gateway_rpm or 'unlimited'}")
    store.prune_logs(settings.max_log_rows)
    try:
        yield
    finally:
        store.flush_logs()
        await router.aclose()


app = FastAPI(
    title="LLM Router",
    version="1.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts or ["*"])
if settings.cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-admin-token", "x-api-key",
                       "x-session-id", "x-route-sticky"],
    )


class HardenMiddleware:
    """Raw ASGI middleware for security headers + body-size cap.

    Uses a pure ASGI wrapper instead of Starlette's BaseHTTPMiddleware, which
    adds latency and can buffer streaming responses.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        cl = headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_body_bytes:
            resp = JSONResponse({"error": {"message": "Request body too large",
                                           "type": "invalid_request_error"}}, status_code=413)
            return await resp(scope, receive, send)

        https = (scope.get("scheme") == "https"
                 or headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")
        hsts = settings.force_https or https

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                raw = message.setdefault("headers", [])
                have = {k.decode("latin-1").lower() for k, _ in raw}
                def add(k, v):
                    if k.lower() not in have:
                        raw.append((k.lower().encode("latin-1"), v.encode("latin-1")))
                        have.add(k.lower())
                for k, v in SECURITY_HEADERS.items():
                    add(k, v)
                if hsts:
                    add("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                add("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(HardenMiddleware)


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


def register_gateway_key(key: str, label: str = "env") -> bool:
    """Add a gateway key if it is not already present. Returns True if added."""
    key = (key or "").strip()
    if not key or key in store.gateway:
        return False
    store.insert("gateway_keys", {"label": label, "key": key, "enabled": 1,
                                  "created_at": time.time()})
    return True


# ---------------- helpers ----------------
def openai_error(message, code="invalid_request_error", status=400, type_="invalid_request_error"):
    return JSONResponse({"error": {"message": message, "type": type_, "code": code}}, status_code=status)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _local_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent for UDP connect
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def gateway_auth(request: Request):
    if not settings.require_auth:
        return "open"
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token = token or request.headers.get("x-api-key", "")
    if not token:
        raise HTTPException(401, "Missing API key")
    match = False
    for gk in store.gateway:
        if const_eq(token, gk):
            match = True
    if const_eq(token, settings.admin_token):
        match = True
    if not match:
        raise HTTPException(401, "Invalid API key")
    if not gw_limiter.allow(token):
        raise HTTPException(429, "Gateway rate limit exceeded")
    return token


def admin_auth(request: Request):
    ident = _client_ip(request)
    lock = admin_guard.locked_for(ident)
    if lock:
        raise HTTPException(429, f"Too many attempts. Try again in {lock}s")
    token = (request.headers.get("x-admin-token")
             or request.headers.get("authorization", "").replace("Bearer ", ""))
    if const_eq(token, settings.admin_token):
        admin_guard.reset(ident)
        return True
    admin_guard.fail(ident)
    raise HTTPException(401, "Invalid admin token")


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1"):
        type_ = "rate_limit_error" if exc.status_code == 429 else (
            "authentication_error" if exc.status_code in (401, 403) else "invalid_request_error")
        return openai_error(str(exc.detail), code=str(exc.status_code),
                            status=exc.status_code, type_=type_)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(ValueError)
async def value_exc(request: Request, exc: ValueError):
    return JSONResponse({"detail": f"invalid input: {exc}"}, status_code=400)


# ---------------- health ----------------
@app.get("/health")
@app.get("/healthz")
async def health():
    return {"status": "ok", "models": len(store.vmodels), "providers": len(store.providers),
            "keys": len(store.keys), "time": time.time()}


@app.get("/readyz")
async def readyz():
    try:
        store.conn.execute("SELECT 1")
        return {"status": "ready"}
    except Exception:
        return JSONResponse({"status": "not-ready"}, status_code=503)


# ---------------- OpenAI-compatible surface ----------------
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
        return JSONResponse(res.data, status_code=res.status, headers=res.headers)
    return StreamingResponse(res.stream, media_type="text/event-stream",
                             headers={**res.headers, "Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.post("/v1/chat/completions")
async def chat(request: Request, _=Depends(gateway_auth)):
    return await _run(request)


@app.post("/v1/completions")
async def completions(request: Request, _=Depends(gateway_auth)):
    body = await request.json()
    if body.get("prompt") is not None and not body.get("messages"):
        body["messages"] = [{"role": "user", "content": body["prompt"]}]
    # drop the legacy field so upstreams that reject unknown params don't 4xx
    body.pop("prompt", None)
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
            r = await router.client_for(cand.get("proxy")).post(url, json=payload, headers=headers)
        except Exception:
            limiter.cool(cand, 5, "network", key_only=True)
            continue
        limiter.release(cand, 0, r.status_code)
        if r.status_code == 429:
            limiter.cool(cand, util.parse_retry_after(r.headers, 20), "429", key_only=True)
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
        "providers": [{**p, "proxy": util.mask_proxy(p.get("proxy") or ""),
                       "proxy_set": bool(p.get("proxy")),
                       "key_count": len(store.keys_by_provider.get(p["id"], []))}
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
        "meta": {"env": settings.env, "encryption": store.box.enabled,
                 "gateway_rpm": settings.gateway_rpm},
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


@app.get("/admin/api/egress")
async def admin_egress(check: int = 0, _=Depends(admin_auth)):
    """Report the outbound identity used for upstream calls.

    Use this to discover the IP to allowlist at your provider. ``check=1``
    additionally calls ``EGRESS_CHECK_URL`` to observe the public IP.
    """
    info = {
        "global_proxy": util.mask_proxy(settings.egress_proxy),
        "global_proxy_set": bool(settings.egress_proxy),
        "trust_env": not bool(settings.egress_proxy),
        "egress_rpm": settings.egress_ip_rpm,
        "local_ip": _local_ip(),
        "providers": {p["name"]: util.mask_proxy(p.get("proxy") or "")
                      for p in store.providers.values() if p.get("proxy")},
    }
    egress_ids = {"default"}
    if settings.egress_proxy:
        egress_ids.add(settings.egress_proxy)
    for p in store.providers.values():
        if p.get("proxy"):
            egress_ids.add(p["proxy"])
    info["usage_1m"] = {(util.mask_proxy(e) if e != "default" else "default"):
                        router.egress.usage(e) for e in egress_ids}
    if check:
        if not settings.egress_check_url:
            info["check_error"] = "EGRESS_CHECK_URL is not configured"
        else:
            try:
                r = await router.client_for(None).get(settings.egress_check_url, timeout=10)
                info["check_url"] = settings.egress_check_url
                info["observed"] = r.text.strip()[:200]
            except Exception as e:
                info["check_error"] = str(e)
    return info


@app.post("/admin/api/providers")
async def add_provider(request: Request, _=Depends(admin_auth)):
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if name in {p["name"] for p in store.providers.values()}:
        raise HTTPException(400, "provider name already exists")
    preset = b.get("preset") or "custom"
    raw_url = (b.get("base_url") or PRESETS.get(preset, {}).get("base_url") or "").strip()
    try:
        base_url = validate_upstream_url(raw_url, settings.block_private_upstreams)
    except ValueError as e:
        raise HTTPException(400, str(e))
    headers = b.get("headers") or preset_headers(preset)
    proxy = (b.get("proxy") or "").strip()
    if proxy:
        try:
            proxy = validate_upstream_url(proxy)
        except ValueError as e:
            raise HTTPException(400, f"invalid proxy: {e}")
    pid = store.insert("providers", {
        "name": name,
        "adapter": b.get("adapter") or PRESETS.get(preset, {}).get("adapter", "openai"),
        "base_url": base_url,
        "headers": json.dumps(headers),
        "proxy": proxy,
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
    if "base_url" in b:
        try:
            b["base_url"] = validate_upstream_url(b["base_url"], settings.block_private_upstreams)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "proxy" in b:
        b["proxy"] = (b["proxy"] or "").strip()
        if b["proxy"]:
            try:
                b["proxy"] = validate_upstream_url(b["proxy"])
            except ValueError as e:
                raise HTTPException(400, f"invalid proxy: {e}")
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
    if int(b["provider_id"]) not in store.providers:
        raise HTTPException(400, "unknown provider")
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
    if "enabled" in b:
        b["enabled"] = 1 if b["enabled"] else 0
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
    strategy = b.get("strategy") or "balanced"
    if strategy not in ("balanced", "priority", "round_robin", "least_latency", "random"):
        raise HTTPException(400, "invalid strategy")
    vid = store.insert("vmodels", {
        "name": name, "description": b.get("description", ""),
        "strategy": strategy,
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
    vid, pid = int(b["vmodel_id"]), int(b["provider_id"])
    if vid not in store.vmodels_by_id:
        raise HTTPException(400, "unknown model")
    if pid not in store.providers:
        raise HTTPException(400, "unknown provider")
    kid = int(b["key_id"]) if b.get("key_id") else None
    if kid is not None:
        k = store.keys.get(kid)
        if not k or k["provider_id"] != pid:
            raise HTTPException(400, "key does not belong to that provider")
    tid = store.insert("targets", {
        "vmodel_id": vid,
        "provider_id": pid,
        "key_id": kid,
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
    if "enabled" in b:
        b["enabled"] = 1 if b["enabled"] else 0
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
    return {"logs": store.rows("logs", "1=1 ORDER BY id DESC LIMIT ?", (max(1, min(limit, 2000)),))}


@app.delete("/admin/api/logs")
async def clear_logs(_=Depends(admin_auth)):
    store.clear_logs()
    return {"ok": True}


@app.get("/admin/api/stream")
async def stream(request: Request, token: str = ""):
    if not const_eq(token, settings.admin_token):
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
async def export(reveal: int = 0, _=Depends(admin_auth)):
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
        for g in data["gateway_keys"]:
            g["key"] = util.mask(g["key"])
        for p in data["providers"]:
            p["headers"] = "{}"
    return data


def _reject_masked(value: str, field: str):
    if isinstance(value, str) and ("***" in value or "..." in value):
        raise ValueError(
            f"refusing to import a masked {field}; export with reveal=1")


def import_config(cfg: dict):
    idmap = {}
    for p in cfg.get("providers", []):
        old = p.pop("id", None)
        p = {k: v for k, v in p.items()}
        if p.get("proxy"):
            _reject_masked(p["proxy"], "proxy")
        p.setdefault("created_at", time.time())
        idmap[("providers", old)] = store.insert("providers", p)
    for k in cfg.get("api_keys", []):
        old = k.pop("id", None)
        _reject_masked(k.get("api_key", ""), "API key")
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
        _reject_masked(g.get("key", ""), "gateway key")
        g.setdefault("created_at", time.time())
        try:
            store.insert("gateway_keys", g)
        except Exception:
            pass


@app.post("/admin/api/import")
async def do_import(request: Request, wipe: int = 0, _=Depends(admin_auth)):
    cfg = await request.json()
    if wipe:
        with store.lock:
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
