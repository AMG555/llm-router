"""Observability: request IDs, structured logs and Prometheus metrics.

Signals map to the questions on-call will ask:
  - metrics  -> "how many / how fast" (RED per route, per provider)
  - logs     -> "what happened in this case" (structured, with request_id)
  - request id -> correlate a single request across logs and upstream calls
"""
import contextvars
import json
import time
import uuid

from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Histogram,
                               generate_latest)

from .config import settings

request_id_var = contextvars.ContextVar("request_id", default="")

_KNOWN_PATHS = {
    "/", "/health", "/healthz", "/readyz", "/metrics",
    "/v1/models", "/v1/chat/completions", "/v1/completions", "/v1/embeddings",
}


def route_label(path: str) -> str:
    """Bounded route label (never a raw path with ids)."""
    if path in _KNOWN_PATHS:
        return path
    if path.startswith("/admin/api/"):
        return "/admin/api/*"
    if path.startswith("/static/"):
        return "/static/*"
    return "other"


def new_request_id() -> str:
    return uuid.uuid4().hex


def log_event(level: str, event: str, **fields):
    """Emit a structured log line. Never pass secrets or PII in fields."""
    rec = {"ts": round(time.time(), 3), "level": level, "event": event}
    rid = request_id_var.get()
    if rid:
        rec["request_id"] = rid
    rec.update({k: v for k, v in fields.items() if v is not None})
    if settings.log_format == "json":
        print(json.dumps(rec, separators=(",", ":")), flush=True)
    else:
        extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        print(f"{level.upper():5} {event} {extra}".rstrip(), flush=True)


# ---- Prometheus metrics (bounded label sets only) ----
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request duration", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
ROUTER_ATTEMPTS = Counter("router_attempts_total", "Upstream attempts", ["provider", "outcome"])
ROUTER_FAILOVERS = Counter("router_failovers_total", "Failovers to a later candidate", ["provider"])
ROUTER_COOLDOWNS = Counter("router_cooldowns_total", "Key cooldowns opened", ["provider"])
AUTH_FAILURES = Counter("auth_failures_total", "Failed authentication attempts", ["surface"])


class ObservabilityMiddleware:
    """Assign/propagate a request id and record RED metrics. Raw ASGI."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id") or new_request_id()
        token = request_id_var.set(rid)
        start = time.perf_counter()
        state = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur = time.perf_counter() - start
            route = route_label(scope.get("path", ""))
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(method, route, f"{state['status'] // 100}xx").inc()
            HTTP_DURATION.labels(method, route).observe(dur)
            request_id_var.reset(token)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
