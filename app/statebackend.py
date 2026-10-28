"""Pluggable state backend for rate windows and cooldowns.

Two implementations:
  * MemoryBackend  - in-process (default; single instance).
  * RedisBackend   - shared across instances (optional; REDIS_URL).

Both expose the same async interface so the limiter can run either way.
RPM/TPM windows and cooldowns live here; per-candidate ephemeral data
(in-flight count, latency EMA) stays in-process by design.
"""
import threading
import time
import uuid
from collections import deque

WINDOW = 60.0


class MemoryBackend:
    name = "memory"

    def __init__(self):
        self._rpm = {}
        self._tpm = {}
        self._cd = {}
        self._lock = threading.Lock()

    async def allow(self, scope, key, limit, window=WINDOW) -> bool:
        if not limit or limit <= 0:
            return True
        now = time.time()
        with self._lock:
            dq = self._rpm.setdefault((scope, key), deque())
            cutoff = now - window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    async def rpm_usage(self, scope, key, window=WINDOW) -> int:
        now = time.time()
        with self._lock:
            dq = self._rpm.get((scope, key))
            if not dq:
                return 0
            cutoff = now - window
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq)

    async def record(self, scope, key, window=WINDOW):
        now = time.time()
        with self._lock:
            dq = self._rpm.setdefault((scope, key), deque())
            dq.append(now)
            cutoff = now - window
            while dq and dq[0] < cutoff:
                dq.popleft()

    async def add_tokens(self, scope, key, amount, window=WINDOW):
        if amount <= 0:
            return
        now = time.time()
        with self._lock:
            dq = self._tpm.setdefault((scope, key), deque())
            dq.append((now, amount))
            cutoff = now - window
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    async def tpm_usage(self, scope, key, window=WINDOW) -> int:
        now = time.time()
        with self._lock:
            dq = self._tpm.get((scope, key))
            if not dq:
                return 0
            cutoff = now - window
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            return sum(a for _, a in dq)

    async def cooldown(self, scope, key, seconds):
        now = time.time()
        with self._lock:
            self._cd[(scope, key)] = max(self._cd.get((scope, key), 0.0),
                                         now + max(0.5, seconds))

    async def cooldown_until(self, scope, key) -> float:
        with self._lock:
            return self._cd.get((scope, key), 0.0)

    async def clear(self, scope, key):
        with self._lock:
            self._rpm.pop((scope, key), None)
            self._tpm.pop((scope, key), None)
            self._cd.pop((scope, key), None)

    async def healthy(self) -> bool:
        return True


class RedisBackend:
    """Redis-backed windows and cooldowns for multi-instance deployments.

    RPM uses a sorted-set sliding window; TPM uses a fixed-window counter
    (an approximation); cooldowns use a key with a TTL.
    """

    name = "redis"

    def __init__(self, url: str, client=None):
        if client is not None:
            self._redis = client
            return
        import redis.asyncio as aioredis  # imported lazily
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def allow(self, scope, key, limit, window=WINDOW) -> bool:
        if not limit or limit <= 0:
            return True
        now = time.time()
        k = f"llmr:rl:{scope}:{key}"
        await self._redis.zremrangebyscore(k, 0, now - window)
        if int(await self._redis.zcard(k)) >= limit:
            return False
        await self._redis.zadd(k, {f"{now}:{uuid.uuid4().hex}": now})
        await self._redis.expire(k, int(window) + 2)
        return True

    async def rpm_usage(self, scope, key, window=WINDOW) -> int:
        now = time.time()
        k = f"llmr:rl:{scope}:{key}"
        await self._redis.zremrangebyscore(k, 0, now - window)
        return int(await self._redis.zcard(k))

    async def record(self, scope, key, window=WINDOW):
        now = time.time()
        k = f"llmr:rl:{scope}:{key}"
        await self._redis.zadd(k, {f"{now}:{uuid.uuid4().hex}": now})
        await self._redis.expire(k, int(window) + 2)

    async def add_tokens(self, scope, key, amount, window=WINDOW):
        if amount <= 0:
            return
        k = f"llmr:tp:{scope}:{key}"
        await self._redis.incrby(k, int(amount))
        await self._redis.expire(k, int(window) + 2)

    async def tpm_usage(self, scope, key, window=WINDOW) -> int:
        v = await self._redis.get(f"llmr:tp:{scope}:{key}")
        return int(v) if v else 0

    async def cooldown(self, scope, key, seconds):
        await self._redis.set(f"llmr:cd:{scope}:{key}", "1",
                              ex=max(1, int(seconds + 0.999)))

    async def cooldown_until(self, scope, key) -> float:
        ttl = await self._redis.ttl(f"llmr:cd:{scope}:{key}")
        return time.time() + ttl if isinstance(ttl, int) and ttl > 0 else 0.0

    async def clear(self, scope, key):
        await self._redis.delete(f"llmr:rl:{scope}:{key}",
                                 f"llmr:tp:{scope}:{key}",
                                 f"llmr:cd:{scope}:{key}")

    async def healthy(self) -> bool:
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False


def build_backend(settings):
    """Return the configured backend, falling back to memory on failure."""
    url = getattr(settings, "redis_url", "")
    if url:
        try:
            return RedisBackend(url)
        except Exception as e:  # pragma: no cover - depends on environment
            print(f"[warn] Redis backend unavailable ({e}); using in-memory state")
    return MemoryBackend()
