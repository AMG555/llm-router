import json
import time
from typing import Any


def now() -> float:
    return time.time()


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 12:
        return secret[:3] + "***"
    return f"{secret[:7]}...{secret[-4:]}"


def est_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    parts.append("[image]")
        return " ".join(parts)
    return str(content)


def estimate_payload_tokens(payload: dict) -> int:
    n = 0
    msgs = payload.get("messages") or []
    for m in msgs:
        n += est_tokens(content_text(m.get("content"))) + 4
        for tc in (m.get("tool_calls") or []):
            n += est_tokens(json.dumps(tc.get("function") or {}))
    for t in (payload.get("tools") or []):
        n += est_tokens(json.dumps(t))
    if isinstance(payload.get("prompt"), str):
        n += est_tokens(payload["prompt"])
    return n


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def sse(obj) -> str:
    return "data: " + json.dumps(obj, separators=(",", ":")) + "\n\n"


def parse_retry_after(headers, default: float) -> float:
    """Read Retry-After plus provider-specific rate-limit reset headers."""
    for h in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset",
              "x-ratelimit-reset-tokens", "x-ratelimit-reset-tokens-minute"):
        raw = headers.get(h)
        if not raw:
            continue
        raw = raw.strip()
        try:
            if raw.endswith("ms"):
                return max(0.5, float(raw[:-2]) / 1000.0)
            if raw.endswith("s"):
                return max(0.5, float(raw[:-1]))
            if raw.endswith("m"):
                return max(0.5, float(raw[:-1]) * 60)
            return max(0.5, float(raw))
        except ValueError:
            continue
    return default


def chunk_delta(vmodel: str, created: int, delta: dict, finish=None, index=0) -> str:
    return sse({
        "id": f"chatcmpl-{created}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": vmodel,
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
    })
