"""Frontend serving, CSP compatibility and XSS-safe patterns."""


def test_index_served_without_inline_handlers(client):
    html = client.get("/").text
    assert "LLM" in html
    for pat in ("onclick=", "onchange=", "onsubmit=", "onload=", "<script>"):
        assert pat not in html, f"found {pat}"


def test_static_assets_served(client):
    for path in ("/static/app.js", "/static/style.css", "/static/favicon.svg"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_csp_is_strict(client):
    csp = client.get("/").headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src" not in csp or "object-src 'none'" in csp


def test_appjs_is_escaped_and_has_no_blocking_dialogs(client):
    js = client.get("/static/app.js").text
    assert "function esc(" in js
    assert "&lt;" in js  # esc() implementation
    for bad in ("alert(", "confirm(", "prompt("):
        assert bad not in js, f"blocking dialog {bad} present"
