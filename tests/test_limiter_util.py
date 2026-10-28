"""Utility and rate-governor unit tests."""
import time

from app import util
from app.crypto import SecretBox
from app.limiter import Limiter
from app.config import Settings


def test_mask():
    assert util.mask("") == ""
    assert util.mask("short") == "sho***"
    assert util.mask("abcdefghijklmnop") == "abcdefg...mnop"


def test_est_tokens_and_payload():
    assert util.est_tokens("") == 0
    assert util.est_tokens("abcd") == 1
    p = util.estimate_payload_tokens({"messages": [{"role": "user", "content": "hello world"}]})
    assert p >= 4


def test_content_text():
    assert util.content_text("hi") == "hi"
    assert util.content_text(None) == ""
    assert util.content_text([{"type": "text", "text": "a"},
                              {"type": "image_url", "image_url": {"url": "x"}}]) == "a [image]"


def test_join_url_and_sse():
    assert util.join_url("https://x/v1/", "/chat") == "https://x/v1/chat"
    assert util.sse({"a": 1}).startswith("data: ")


def test_parse_retry_after():
    assert util.parse_retry_after({"retry-after": "5"}, 20) == 5
    assert util.parse_retry_after({"retry-after": "1500ms"}, 20) == 1.5
    assert util.parse_retry_after({"retry-after": "2s"}, 20) == 2
    assert util.parse_retry_after({"retry-after": "1m"}, 20) == 60
    assert util.parse_retry_after({}, 20) == 20
    assert util.parse_retry_after({"retry-after": "garbage"}, 20) == 20


def _cand(cid, **kw):
    d = {"cid": cid, "tid": "t" + cid, "key_rpm": 0, "key_tpm": 0, "key_conc": 0,
         "t_rpm": 0, "t_tpm": 0}
    d.update(kw)
    return d


def test_limiter_rpm():
    lim = Limiter(Settings())
    c = _cand("a", key_rpm=2)
    assert lim.acquire(c) and lim.acquire(c)
    assert not lim.acquire(c)


def test_limiter_concurrency():
    lim = Limiter(Settings())
    c = _cand("b", key_conc=1)
    assert lim.acquire(c)
    assert not lim.acquire(c)
    lim.release(c, 5, 200)
    assert lim.acquire(c)


def test_limiter_tpm():
    lim = Limiter(Settings())
    c = _cand("c", key_tpm=10)
    assert lim.acquire(c)
    lim.release(c, 5, 200)
    lim.add_tokens(c, 10)
    assert not lim.acquire(c)


def test_limiter_cooldown():
    lim = Limiter(Settings())
    c = _cand("d")
    lim.cool(c, 60, "429", key_only=True)
    assert not lim.available(c)
    assert lim.kstate("d").cooldown > time.time()
    lim.kstate("d").cooldown = 0
    assert lim.available(c)


def test_limiter_health_shape():
    lim = Limiter(Settings())
    lim.acquire(_cand("e"))
    h = lim.health()
    assert "keys" in h and "targets" in h


def test_secretbox_round_trip():
    box = SecretBox("a-strong-secret")
    assert box.enabled
    token = box.encrypt("sk-secret-value")
    assert token.startswith("enc:v1:")
    assert box.decrypt(token) == "sk-secret-value"
    assert box.encrypt("") == ""


def test_secretbox_disabled_passthrough():
    box = SecretBox("")
    assert not box.enabled
    assert box.encrypt("plain") == "plain"
    assert box.decrypt("plain") == "plain"


def test_secretbox_wrong_key_raises_clear_error():
    import pytest
    token = SecretBox("key-one").encrypt("sk-secret")
    with pytest.raises(RuntimeError) as err:
        SecretBox("key-two").decrypt(token)
    assert "SECRET_KEY" in str(err.value)
