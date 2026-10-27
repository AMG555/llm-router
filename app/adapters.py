import json

from . import util

FINISH_ANTHROPIC = {"end_turn": "stop", "stop_sequence": "stop",
                    "max_tokens": "length", "tool_use": "tool_calls", "pause_turn": "stop"}
FINISH_GEMINI = {"STOP": "stop", "MAX_TOKENS": "length", "TOOL_CALLS": "tool_calls"}


class BaseAdapter:
    name = "base"
    supports_embeddings = False

    def build(self, target, body, stream):
        raise NotImplementedError

    def build_embeddings(self, target, body):
        raise NotImplementedError

    def parse(self, data, vmodel, created):
        return data

    def usage(self, data):
        u = data.get("usage") or {}
        return int(u.get("prompt_tokens") or u.get("input_tokens") or 0), \
               int(u.get("completion_tokens") or u.get("output_tokens") or 0)

    async def stream(self, resp, vmodel, created, stats):
        done = False
        async for line in resp.aiter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                yield line + "\n"
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                yield "data: [DONE]\n\n"
                break
            try:
                ev = json.loads(payload)
            except ValueError:
                yield line + "\n\n"
                continue
            if isinstance(ev, dict):
                ev["model"] = vmodel
                u = ev.get("usage")
                if u:
                    stats["prompt_tokens"] = u.get("prompt_tokens") or stats.get("prompt_tokens", 0)
                    stats["completion_tokens"] = u.get("completion_tokens") or stats.get("completion_tokens", 0)
            yield "data: " + json.dumps(ev, separators=(",", ":")) + "\n\n"
        if not done:
            yield "data: [DONE]\n\n"

    def list_models_url(self, target):
        return util.join_url(target["base_url"], "models"), \
               {"authorization": f"Bearer {target['api_key']}"}


class OpenAIAdapter(BaseAdapter):
    name = "openai"
    supports_embeddings = True

    def build(self, target, body, stream):
        payload = dict(body)
        payload["model"] = target["upstream_model"]
        payload.pop("provider", None)
        if stream:
            payload["stream"] = True
            so = dict(payload.get("stream_options") or {})
            so.setdefault("include_usage", True)
            payload["stream_options"] = so
        url = util.join_url(target["base_url"], "chat/completions")
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {target['api_key']}"}
        headers.update(target.get("extra_headers") or {})
        return url, headers, payload, None

    def build_embeddings(self, target, body):
        payload = dict(body)
        payload["model"] = target["upstream_model"]
        url = util.join_url(target["base_url"], "embeddings")
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {target['api_key']}"}
        return url, headers, payload, None


# ------------------------- ANTHROPIC -------------------------
def _to_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            blocks.append({"type": "text", "text": p.get("text", "")})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                head, b64 = url.split(";base64,", 1)
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": head[5:], "data": b64}})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks or [{"type": "text", "text": ""}]


def openai_to_anthropic(body):
    out = {"max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or 4096,
           "messages": []}
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop"):
        out["stop_sequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    system = []
    for m in body.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            system.append(util.content_text(content))
        elif role == "tool":
            out["messages"].append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": util.content_text(content)}]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks = _to_blocks(content) if content else []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "call_0",
                               "name": fn.get("name") or "tool", "input": args})
            out["messages"].append({"role": "assistant", "content": blocks})
        else:
            out["messages"].append({"role": "assistant" if role == "assistant" else "user",
                                    "content": _to_blocks(content)})
    if system:
        out["system"] = "\n\n".join(system)
    if body.get("tools"):
        out["tools"] = [{"name": t["function"]["name"],
                         "description": t["function"].get("description", ""),
                         "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                        for t in body["tools"] if t.get("type", "function") == "function"]
    return out


class AnthropicAdapter(BaseAdapter):
    name = "anthropic"

    def build(self, target, body, stream):
        payload = openai_to_anthropic(body)
        if stream:
            payload["stream"] = True
        headers = {"content-type": "application/json",
                   "x-api-key": target["api_key"],
                   "anthropic-version": "2023-06-01"}
        headers.update(target.get("extra_headers") or {})
        return util.join_url(target["base_url"], "messages"), headers, payload, None

    def parse(self, data, vmodel, created):
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        tool_calls = []
        for b in blocks:
            if b.get("type") == "tool_use":
                tool_calls.append({"id": b.get("id"), "type": "function",
                                   "function": {"name": b.get("name"),
                                                "arguments": json.dumps(b.get("input") or {})}})
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        u = data.get("usage") or {}
        return {"id": data.get("id", f"chatcmpl-{created}"), "object": "chat.completion",
                "created": created, "model": vmodel,
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": FINISH_ANTHROPIC.get(data.get("stop_reason"), "stop")}],
                "usage": {"prompt_tokens": u.get("input_tokens", 0),
                          "completion_tokens": u.get("output_tokens", 0),
                          "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}}

    async def stream(self, resp, vmodel, created, stats):
        first = True
        tool_idx = -1
        finish = "stop"
        yield chunk_first(vmodel, created) if first else ""
        first = False
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            ev = json.loads(payload)
            t = ev.get("type")
            if t == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                stats["prompt_tokens"] = u.get("input_tokens", 0)
            elif t == "content_block_start":
                b = ev.get("content_block") or {}
                if b.get("type") == "tool_use":
                    tool_idx += 1
                    yield util.chunk_delta(vmodel, created, {"tool_calls": [{
                        "index": tool_idx, "id": b.get("id"), "type": "function",
                        "function": {"name": b.get("name"), "arguments": ""}}]})
            elif t == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    stats["text_len"] = stats.get("text_len", 0) + len(d.get("text", ""))
                    yield util.chunk_delta(vmodel, created, {"content": d.get("text", "")})
                elif d.get("type") == "input_json_delta":
                    yield util.chunk_delta(vmodel, created, {"tool_calls": [{
                        "index": tool_idx, "function": {"arguments": d.get("partial_json", "")}}]})
            elif t == "message_delta":
                finish = FINISH_ANTHROPIC.get((ev.get("delta") or {}).get("stop_reason"), "stop")
                u = ev.get("usage") or {}
                if u.get("output_tokens"):
                    stats["completion_tokens"] = u["output_tokens"]
            elif t == "message_stop":
                break
        yield util.chunk_delta(vmodel, created, {}, finish=finish)
        yield "data: [DONE]\n\n"


def chunk_first(vmodel, created):
    return util.chunk_delta(vmodel, created, {"role": "assistant", "content": ""})


# ------------------------- GEMINI -------------------------
def openai_to_gemini(body):
    contents, system = [], []
    for m in body.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            system.append({"text": util.content_text(content)})
            continue
        parts = []
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"text": p.get("text", "")})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        head, b64 = url.split(";base64,", 1)
                        parts.append({"inline_data": {"mime_type": head[5:], "data": b64}})
                    else:
                        parts.append({"file_data": {"file_uri": url}})
        contents.append({"role": "model" if role == "assistant" else "user",
                         "parts": parts or [{"text": ""}]})
    out = {"contents": contents}
    if system:
        out["systemInstruction"] = {"parts": system}
    gen = {}
    if body.get("temperature") is not None:
        gen["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        gen["topP"] = body["top_p"]
    if body.get("max_tokens"):
        gen["maxOutputTokens"] = body["max_tokens"]
    if body.get("stop"):
        gen["stopSequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    if gen:
        out["generationConfig"] = gen
    if body.get("tools"):
        out["tools"] = [{"functionDeclarations": [
            {"name": t["function"]["name"],
             "description": t["function"].get("description", ""),
             "parameters": t["function"].get("parameters") or {"type": "object", "properties": {}}}
            for t in body["tools"]]}]
    return out


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    supports_embeddings = True

    def _url(self, target, method, stream):
        base = target["base_url"].rstrip("/")
        if stream:
            return f"{base}/models/{target['upstream_model']}:{method}?alt=sse"
        return f"{base}/models/{target['upstream_model']}:{method}"

    def build(self, target, body, stream):
        payload = openai_to_gemini(body)
        headers = {"content-type": "application/json", "x-goog-api-key": target["api_key"]}
        headers.update(target.get("extra_headers") or {})
        return self._url(target, "streamGenerateContent" if stream else "generateContent", stream), \
               headers, payload, None

    def build_embeddings(self, target, body):
        payload = {"content": {"parts": [{"text": body.get("input") if isinstance(body.get("input"), str)
                                          else " ".join(body.get("input") or [])}]}}
        headers = {"content-type": "application/json", "x-goog-api-key": target["api_key"]}
        return f"{target['base_url'].rstrip('/')}/models/{target['upstream_model']}:embedContent", \
               headers, payload, None

    def parse(self, data, vmodel, created):
        cand = (data.get("candidates") or [{}])[0]
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts)
        u = data.get("usageMetadata") or {}
        return {"id": f"chatcmpl-{created}", "object": "chat.completion", "created": created,
                "model": vmodel,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": FINISH_GEMINI.get(cand.get("finishReason"), "stop")}],
                "usage": {"prompt_tokens": u.get("promptTokenCount", 0),
                          "completion_tokens": u.get("candidatesTokenCount", 0),
                          "total_tokens": u.get("totalTokenCount", 0)}}

    async def stream(self, resp, vmodel, created, stats):
        yield chunk_first(vmodel, created)
        finish = "stop"
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            ev = json.loads(payload)
            cand = (ev.get("candidates") or [{}])[0]
            for p in ((cand.get("content") or {}).get("parts") or []):
                if p.get("text"):
                    stats["text_len"] = stats.get("text_len", 0) + len(p["text"])
                    yield util.chunk_delta(vmodel, created, {"content": p["text"]})
            if cand.get("finishReason"):
                finish = FINISH_GEMINI.get(cand["finishReason"], "stop")
            u = ev.get("usageMetadata") or {}
            if u:
                stats["prompt_tokens"] = u.get("promptTokenCount", 0)
                stats["completion_tokens"] = u.get("candidatesTokenCount", 0)
        yield util.chunk_delta(vmodel, created, {}, finish=finish)
        yield "data: [DONE]\n\n"


ADAPTERS = {"openai": OpenAIAdapter(), "anthropic": AnthropicAdapter(), "gemini": GeminiAdapter()}


def get_adapter(name):
    return ADAPTERS.get(name) or ADAPTERS["openai"]
