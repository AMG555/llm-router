"""Adapter translation: OpenAI <-> Anthropic / Gemini, including streaming."""

import json


def _stream_text(client, gw_h, model):
    text, done = "", False
    with client.stream("POST", "/v1/chat/completions", headers=gw_h, json={
            "model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10, "stream": True}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
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
    return text, done


def test_anthropic_completion_and_usage(client, gw_h, make_model):
    m = make_model([("anth-ok", "anthropic", 100)], adapter="anthropic")
    r = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    body = r.json()
    assert r.status_code == 200
    assert "claude" in body["choices"][0]["message"]["content"]
    assert body["usage"]["prompt_tokens"] == 7 and body["usage"]["completion_tokens"] == 4


def test_anthropic_stream_translated(client, gw_h, make_model):
    m = make_model([("anth-st", "anthropic", 100)], adapter="anthropic")
    text, done = _stream_text(client, gw_h, m["name"])
    assert done and text == "hi claude"


def test_anthropic_tool_use_maps_to_tool_calls(client, gw_h, make_model):
    m = make_model([("anthropic-tool", "anthropic-tool", 100)], adapter="anthropic")
    r = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "weather?"}],
        "tools": [{"type": "function", "function": {"name": "get_weather",
                   "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
        "max_tokens": 10})
    body = r.json()
    tc = body["choices"][0]["message"].get("tool_calls") or []
    assert len(tc) == 1 and tc[0]["function"]["name"] == "get_weather"
    assert json.loads(tc[0]["function"]["arguments"])["city"] == "Paris"
    assert body["choices"][0]["finish_reason"] == "tool_calls"


def test_gemini_completion_and_usage(client, gw_h, make_model):
    m = make_model([("gem-ok", "gemini", 100)], adapter="gemini")
    r = client.post("/v1/chat/completions", headers=gw_h, json={
        "model": m["name"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    body = r.json()
    assert r.status_code == 200
    assert "gemini" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] == 11


def test_gemini_stream_translated(client, gw_h, make_model):
    m = make_model([("gem-st", "gemini", 100)], adapter="gemini")
    text, done = _stream_text(client, gw_h, m["name"])
    assert done and text == "hi gemini"
