"""State backends: in-memory and Redis (shared across instances)."""
import asyncio
import time

import fakeredis
import fakeredis.aioredis

from app.statebackend import MemoryBackend, RedisBackend, build_backend
from app.config import Settings


def run(coro):
    return asyncio.run(coro)


def test_memory_backend_windows_and_cooldown():
    b = MemoryBackend()

    async def scenario():
        assert await b.allow("s", "k", 2)
        assert await b.allow("s", "k", 2)
        assert not await b.allow("s", "k", 2)
        assert await b.rpm_usage("s", "k") == 2
        await b.cooldown("s", "k", 30)
        assert await b.cooldown_until("s", "k") > time.time()
        await b.clear("s", "k")
        assert await b.cooldown_until("s", "k") == 0

    run(scenario())


def test_redis_backends_share_state():
    server = fakeredis.FakeServer()
    c1 = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    c2 = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    b1 = RedisBackend("", client=c1)
    b2 = RedisBackend("", client=c2)

    async def scenario():
        # window shared across "instances"
        assert await b1.allow("k", "x", 2)
        assert await b1.allow("k", "x", 2)
        assert not await b2.allow("k", "x", 2)          # second instance sees the limit
        assert await b2.rpm_usage("k", "x") == 2
        # cooldown shared
        await b1.cooldown("k", "y", 30)
        assert await b2.cooldown_until("k", "y") > time.time()
        # tokens shared
        await b1.add_tokens("k", "y", 15)
        assert await b2.tpm_usage("k", "y") == 15
        # clear shared
        await b1.clear("k", "y")
        assert await b2.tpm_usage("k", "y") == 0

    run(scenario())


def test_build_backend_defaults_to_memory():
    s = Settings()
    s.redis_url = ""
    assert build_backend(s).name == "memory"
