"""Rate governor and cooldown tracking.

Sliding RPM/TPM windows and cooldowns live in a pluggable backend
(in-memory by default, Redis for multi-instance). Per-candidate ephemeral
state (in-flight count, latency EMA) stays in-process.
"""
import time

from .statebackend import WINDOW, build_backend


class State:
    __slots__ = ("inflight", "last_used", "ok", "err", "fails",
                 "latency_ema", "last_error", "last_status")

    def __init__(self):
        self.inflight = 0
        self.last_used = 0.0
        self.ok = 0
        self.err = 0
        self.fails = 0
        self.latency_ema = 0.0
        self.last_error = ""
        self.last_status = 0


class Limiter:
    def __init__(self, settings, backend=None):
        self.s = settings
        self.backend = backend or build_backend(settings)
        self.keys = {}
        self.targets = {}

    def kstate(self, cid) -> State:
        return self.keys.setdefault(cid, State())

    def tstate(self, tid) -> State:
        return self.targets.setdefault(tid, State())

    # ---------- gate ----------
    async def available(self, c, now=None) -> bool:
        now = now or time.time()
        if await self.backend.cooldown_until("k", c["cid"]) > now:
            return False
        ks = self.kstate(c["cid"])
        if c["key_conc"] and ks.inflight >= c["key_conc"]:
            return False
        if c["key_rpm"] and await self.backend.rpm_usage("k", c["cid"]) >= c["key_rpm"]:
            return False
        if c["key_tpm"] and await self.backend.tpm_usage("k", c["cid"]) >= c["key_tpm"]:
            return False
        if await self.backend.cooldown_until("t", c["tid"]) > now:
            return False
        if c["t_rpm"] and await self.backend.rpm_usage("t", c["tid"]) >= c["t_rpm"]:
            return False
        if c["t_tpm"] and await self.backend.tpm_usage("t", c["tid"]) >= c["t_tpm"]:
            return False
        return True

    async def acquire(self, c) -> bool:
        if not await self.available(c):
            return False
        await self.backend.record("k", c["cid"])
        await self.backend.record("t", c["tid"])
        now = time.time()
        ks = self.kstate(c["cid"])
        ks.inflight += 1
        ks.last_used = now
        self.tstate(c["tid"]).last_used = now
        return True

    # ---------- bookkeeping ----------
    def release(self, c, latency_ms, status):
        ks = self.kstate(c["cid"])
        ts = self.tstate(c["tid"])
        ks.inflight = max(0, ks.inflight - 1)
        ks.last_status = status
        ts.last_status = status
        if status < 400:
            ks.ok += 1
            ks.fails = 0
            ts.ok += 1
            ks.latency_ema = latency_ms if not ks.latency_ema else 0.75 * ks.latency_ema + 0.25 * latency_ms
        else:
            ks.err += 1
            ts.err += 1

    async def add_tokens(self, c, total):
        if total <= 0:
            return
        await self.backend.add_tokens("k", c["cid"], total)
        await self.backend.add_tokens("t", c["tid"], total)

    async def cool(self, c, seconds, reason, key_only=False):
        await self.backend.cooldown("k", c["cid"], seconds)
        ks = self.kstate(c["cid"])
        ks.fails += 1
        ks.last_error = reason[:200]
        if not key_only:
            await self.backend.cooldown("t", c["tid"], seconds)
            self.tstate(c["tid"]).last_error = reason[:200]

    # ---------- egress (per outbound IP) ----------
    async def egress_allow(self, key) -> bool:
        return await self.backend.allow("egress", key, self.s.egress_ip_rpm)

    async def egress_usage(self, key) -> int:
        return await self.backend.rpm_usage("egress", key)

    # ---------- admin ----------
    async def reset(self, cid, tid=None):
        await self.backend.clear("k", cid)
        st = self.kstate(cid)
        st.fails = 0
        st.last_error = ""
        st.last_status = 0
        if tid:
            await self.backend.clear("t", tid)
            self.tstate(tid).last_error = ""

    async def health(self):
        now = time.time()
        keys = {cid: await self._snapshot("k", cid, st, now) for cid, st in self.keys.items()}
        targets = {tid: await self._snapshot("t", tid, st, now, flight=False)
                   for tid, st in self.targets.items()}
        return {"keys": keys, "targets": targets, "backend": self.backend.name}

    async def _snapshot(self, scope, key, st, now, flight=True):
        cd = await self.backend.cooldown_until(scope, key)
        return {
            "cooldown": round(max(0.0, cd - now), 1),
            "inflight": st.inflight if flight else 0,
            "rpm_used": await self.backend.rpm_usage(scope, key),
            "tpm_used": await self.backend.tpm_usage(scope, key),
            "ok": st.ok, "err": st.err,
            "latency_ema": round(st.latency_ema),
            "last_error": st.last_error, "last_status": st.last_status,
            "available": now >= cd,
        }
