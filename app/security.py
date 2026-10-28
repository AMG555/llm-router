"""Security primitives: constant-time compare, lockout guard, gateway rate
limiting, upstream URL validation and response security headers."""
import hmac
import ipaddress
import threading
import time
from collections import deque
from urllib.parse import urlparse

# Applied to every response. script-src stays strict ('self' only) because the
# admin UI ships no inline <script>/handlers; style-src allows inline styles
# used by the progress bars.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}

WINDOW = 60.0


def const_eq(a: str, b: str) -> bool:
    """Constant-time string comparison that never raises on length mismatch."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_token(token: str) -> str:
    """Stable hash for storing admin API tokens (never store the token itself)."""
    import hashlib
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class LockoutGuard:
    """Per-identity failed-attempt tracker used to slow admin brute force."""

    def __init__(self, max_fails: int = 10, lock_seconds: int = 300):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._fails = {}
        self._lock = threading.Lock()

    def locked_for(self, ident: str) -> int:
        with self._lock:
            fails = self._fails.get(ident)
            if not fails:
                return 0
            cutoff = time.time() - self.lock_seconds
            kept = deque((t for t in fails if t >= cutoff),
                         maxlen=self.max_fails * 2)
            self._fails[ident] = kept
            if len(kept) >= self.max_fails:
                return int(kept[0] + self.lock_seconds - time.time()) + 1
            return 0

    def fail(self, ident: str):
        with self._lock:
            self._fails.setdefault(ident, deque(maxlen=self.max_fails * 2)).append(time.time())

    def reset(self, ident: str):
        with self._lock:
            self._fails.pop(ident, None)


class SlidingRateLimiter:
    """Sliding-window request limiter keyed by an arbitrary string."""

    def __init__(self, limit: int, window: float = WINDOW):
        self.limit = limit
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if not self.limit or self.limit <= 0:
            return True
        now = time.time()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            return True

    def usage(self, key: str) -> int:
        now = time.time()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq)

    def remaining(self, key: str):
        if not self.limit or self.limit <= 0:
            return None
        return max(0, self.limit - self.usage(key))


class KeyedRateLimiter:
    """Sliding-window limiter whose per-key limit is supplied per call.

    Used for per-gateway-key quotas, where each key carries its own rpm.
    """

    def __init__(self, window: float = WINDOW):
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        if not limit or limit <= 0:
            return True
        now = time.time()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def usage(self, key: str) -> int:
        now = time.time()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq)


def validate_upstream_url(url: str, block_private: bool = False) -> str:
    """Return a normalized base URL or raise ValueError."""
    if not url or not isinstance(url, str):
        raise ValueError("base_url is required")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must use http or https")
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    if block_private:
        host = parsed.hostname.lower()
        if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
            raise ValueError("private/loopback upstreams are disabled")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("private/loopback upstreams are disabled")
        except ValueError as e:
            if "private/loopback" in str(e):
                raise
    return url.rstrip("/")
