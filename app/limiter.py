import time
from collections import deque

WINDOW = 60.0


class State:
    __slots__ = ("rpm", "tpm", "cooldown", "inflight", "last_used", "ok", "err",
                 "latency_ema", "last_error", "last_status", "fails")

    def __init__(self):
        self.rpm = deque()
        self.tpm = deque()
        self.cooldown = 0.0
        self.inflight = 0
        self.last_used = 0.0
        self.ok = 0
        self.err = 0
        self.fails = 0
        self.latency_ema = 0.0
        self.last_error = ""
        self.last_status = 0

    def snapshot(self, now):
        self._prune(now)
        return {
            "cooldown": round(max(0.0, self.cooldown - now), 1),
            "inflight": self.inflight,
            "rpm_used": len(self.rpm),
            "tpm_used": sum(t for _, t in self.tpm),
            "ok": self.ok,
            "err": self.err,
            "latency_ema": round(self.latency_ema),
            "last_error": self.last_error,
            "last_status": self.last_status,
            "available": now >= self.cooldown,
        }

    def _prune(self, now):
        cutoff = now - WINDOW
        while self.rpm and self.rpm[0] < cutoff:
            self.rpm.popleft()
        while self.tpm and self.tpm[0][0] < cutoff:
            self.tpm.popleft()


class Limiter:
    def __init__(self, settings):
        self.s = settings
        self.keys = {}
        self.targets = {}

    def kstate(self, cid) -> State:
        return self.keys.setdefault(cid, State())

    def tstate(self, tid) -> State:
        return self.targets.setdefault(tid, State())

    # ---------- gate ----------
    def _check(self, st: State, now, max_rpm, max_tpm, max_conc) -> bool:
        st._prune(now)
        if now < st.cooldown:
            return False
        if max_conc and st.inflight >= max_conc:
            return False
        if max_rpm and len(st.rpm) >= max_rpm:
            return False
        if max_tpm and sum(t for _, t in st.tpm) >= max_tpm:
            return False
        return True

    def available(self, c, now=None) -> bool:
        now = now or time.time()
        return (self._check(self.kstate(c["cid"]), now, c["key_rpm"], c["key_tpm"], c["key_conc"])
                and self._check(self.tstate(c["tid"]), now, c["t_rpm"], c["t_tpm"], 0))

    def acquire(self, c) -> bool:
        now = time.time()
        if not self.available(c, now):
            return False
        ks, ts = self.kstate(c["cid"]), self.tstate(c["tid"])
        ks.rpm.append(now)
        ts.rpm.append(now)
        ks.inflight += 1
        ks.last_used = now
        ts.last_used = now
        return True

    # ---------- bookkeeping ----------
    def release(self, c, latency_ms, status):
        ks, ts = self.kstate(c["cid"]), self.tstate(c["tid"])
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

    def add_tokens(self, c, total):
        if total <= 0:
            return
        now = time.time()
        self.kstate(c["cid"]).tpm.append((now, total))
        self.tstate(c["tid"]).tpm.append((now, total))

    def cool(self, c, seconds, reason, key_only=False):
        until = time.time() + max(0.5, seconds)
        ks = self.kstate(c["cid"])
        ks.cooldown = max(ks.cooldown, until)
        ks.fails += 1
        ks.last_error = reason[:200]
        if not key_only:
            ts = self.tstate(c["tid"])
            ts.cooldown = max(ts.cooldown, until)
            ts.last_error = reason[:200]

    def health(self):
        now = time.time()
        return {
            "keys": {cid: st.snapshot(now) for cid, st in self.keys.items()},
            "targets": {tid: st.snapshot(now) for tid, st in self.targets.items()},
        }
