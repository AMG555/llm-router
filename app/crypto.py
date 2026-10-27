"""Optional authenticated encryption for secrets at rest (AES-256-GCM).

Enabled by setting the ``SECRET_KEY`` environment variable. Without it,
values are stored as-is (suitable only for local/development use).
"""
import base64
import hashlib

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - exercised only when dependency missing
    _HAVE_CRYPTO = False

_PREFIX = "enc:v1:"


class SecretBox:
    def __init__(self, key: str):
        self.aead = None
        if not key:
            return
        if not _HAVE_CRYPTO:
            raise RuntimeError(
                "SECRET_KEY is set but the 'cryptography' package is not installed")
        self.aead = AESGCM(hashlib.sha256(key.encode("utf-8")).digest())

    @property
    def enabled(self) -> bool:
        return self.aead is not None

    def encrypt(self, value: str) -> str:
        if not value or not self.aead:
            return value
        if value.startswith(_PREFIX):
            return value  # already encrypted (idempotent)
        import os
        nonce = os.urandom(12)
        ct = self.aead.encrypt(nonce, value.encode("utf-8"), None)
        return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value or not self.aead or not value.startswith(_PREFIX):
            return value
        raw = base64.b64decode(value[len(_PREFIX):])
        nonce, ct = raw[:12], raw[12:]
        return self.aead.decrypt(nonce, ct, None).decode("utf-8")
