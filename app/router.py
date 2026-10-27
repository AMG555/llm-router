import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field

import httpx

from . import util
from .adapters import get_adapter


class NoCapacity(Exception):
    pass


class UnknownModel(Exception):
    pass


@dataclass
class RouteResult:
    status: int
    data: dict | None
    stream: object | None
    provider: str
    key_label: str
    attempts: int
    headers: dict = field(default_factory=dict)


class Router:
    def __init__(self, store, limiter, settings, bus):
        self.store = store
        self.limiter = limiter
        self.s = settings
        self.bus = bus
        self.swrr = {}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
            follow_redirects=True,
        )

    async def aclose(self):
        await self.client.aclose()

    # ---------------- candidate resolution ----------------
    def _cand(self, target, provider, key):
        def pick(a, b):
            a, b = int(a or 0), int(b or 0)
            if a and b:
                return min(a, b)
            return a or b
        return {
            "cid": f"k{key['id']}",
            "tid": f"t{target['id']}",
            "target_id": target["id"],
            "key_id": key["id"],
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "key_label": key["label"] or f"key#{key['id']}",
            "adapter": provider["adapter"],
            "base_url": provider["base_url"],
            "api_key": key["api_key"],
            "upstream_model": target["upstream_model"],
            "weight": max(0.01, float(target.get("weight") or 1) * float(key.get("weight") or 1)),
            "priority": min(int(target.get("priority") or 100), int(key.get("priority") or 100)),
            "key_rpm": int(key.get("max_rpm") or 0),
            "key_tpm": int(key.get("max_tpm") or 0),
            "key_conc": int(key.get("max_concurrency") or 0),
            "t_rpm": pick(target.get("max_rpm"), 0),
            "t_tpm": pick(target.get("max_tpm"), 0),
            "extra_headers": json.loads(provider.get("headers") or "{}"),
        }

    def candidates(self, vmodel: dict):
        out = []
        for t in self.store.targets:
            if t["vmodel_id"] != vmodel["id"] or not t["enabled"]:
                continue
            p = self.store.providers.get(t["provider_id"])
            if not p or not p["enabled"]:
                continue
            if t["key_id"]:
                k = self.store.keys.get(t["key_id"])
                if k and k["enabled"] and k["provider_id"] == p["id"]:
                    out.append(self._cand(t, p, k))
            else:
                for k in self.store.keys_by_provider.get(p["id"], []):
                    if k["enabled"]:
                        out.append(self._cand(t, p, k))
        return out

    def resolve(self, name: str):
        vm = self.store.vmodels.get(name)
        if vm and vm["enabled"]:
            return vm, self.candidates(vm)
        # direct "provider:model" escape hatch
        if name and ":" in name:
            pname, _, model = name.partition(":")
            prov = next((p for p in self.store.providers.values() if p["name"] == pname), None)
            if prov:
                fake = {"id": -1, "name": name, "strategy": "balanced", "sticky": 0}
                cands = []
                for k in self.store.keys_by_provider.get(prov["id"], []):
                    if k["enabled"]:
                        cands.append(self._cand({
                            "id": f"direct-{k['id']}", "upstream_model": model, "weight": 1,
                            "priority": 100, "max_rpm": 0, "max_tpm": 0, "enabled": 1}, prov, k))
                return fake, cands
        if vm:
            return vm, []
        raise UnknownModel(f"Model '{name}' is not configured on this router")

    # ---------------- ordering ----------------
    def _swrr(self, cands):
        remaining = list(cands)
        cur = {c["cid"] + c["tid"]: self.swrr.get(c["cid"] + c["tid"], 0.0) for c in remaining}
        ordered = []
        while remaining:
            total = sum(c["weight"] for c in remaining) or 1.0
            best = max(remaining, key=lambda c: cur[c["cid"] + c["tid"]] + c["weight"])
            key = best["cid"] + best["tid"]
            cur[key] = cur[key] + best["weight"] - total
            ordered.append(best)
            remaining.remove(best)
        for k, v in cur.items():
            self.swrr[k] = v
        return ordered

    def order(self, cands, strategy, sticky, session):
        if not cands:
            return []
        if strategy == "random":
            out = list(cands)
            random.shuffle(out)
            return out
        if strategy == "least_latency":
            return sorted(cands, key=lambda c: (c["priority"],
                                                self.limiter.kstate(c["cid"]).latency_ema))
        if strategy == "round_robin":
            out = sorted(cands, key=lambda c: c["priority"])
            return self._rotate(out, session)
        if strategy == "priority":
            return sorted(cands, key=lambda c: c["priority"])
        groups = {}
        for c in cands:
            groups.setdefault(c["priority"], []).append(c)
        out = []
        for p in sorted(groups):
            out.extend(self._swrr(groups[p]))
        if sticky and session and len(out) > 1:
            top = groups[min(groups)]
            head = self._rotate(top, session)
            out = head + [c for c in out if c not in top]
        return out

    @staticmethod
    def _rotate(cands, session):
        if not session or len(cands) < 2:
            return list(cands)
        h = int(hashlib.blake2b(session.encode(), digest_size=4).hexdigest(), 16)
        i = h % len(cands)
        return list(cands[i:]) + list(cands[:i])

    # ---------------- execution ----------------
    async def execute(self, model_name, body, stream=False, session=None, endpoint="chat"):
        started = time.time()
        vm, cands = self.resolve(model_name)
        if not cands:
            raise NoCapacity(f"No enabled targets for model '{model_name}'")

        strategy = vm.get("strategy") or "balanced"
        ordered = self.order(cands, strategy, vm.get("sticky", 1), session)
        ready = [c for c in ordered if self.limiter.available(c)]
        pool = ready or ordered
        attempts = 0
        errors = []

        for cand in pool[: self.s.max_attempts]:
            if not self.limiter.acquire(cand):
                continue
            attempts += 1
            adapter = get_adapter(cand["adapter"])
            t0 = time.time()
            try:
                url, headers, payload, _ = adapter.build(cand, body, stream)
                req = self.client.build_request("POST", url, json=payload, headers=headers)
                resp = await self.client.send(req, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                self.limiter.release(cand, int((time.time() - t0) * 1000), 599)
                self.limiter.cool(cand, self.s.short_cooldown,
                                  f"network: {type(e).__name__}", key_only=True)
                errors.append(f"{cand['provider_name']}/{cand['key_label']} net {type(e).__name__}")
                self.bus.publish({"kind": "retry", "provider": cand["provider_name"],
                                  "key": cand["key_label"], "reason": "network"})
                continue

            status = resp.status_code

            # ---- rate limited ----
            if status == 429:
                wait = util.parse_retry_after(resp.headers, self.s.default_cooldown)
                body_txt = await resp.aread()
                await resp.aclose()
                self.limiter.release(cand, int((time.time() - t0) * 1000), 429)
                self.limiter.cool(cand, wait, "429 rate limited", key_only=True)
                self.limiter.add_tokens(cand, util.estimate_payload_tokens(body))
                errors.append(f"{cand['provider_name']} 429 (cool {wait:.0f}s)")
                self.bus.publish({"kind": "cooldown", "provider": cand["provider_name"],
                                  "key": cand["key_label"], "seconds": round(wait, 1)})
                continue

            # ---- bad / expired key ----
            if status in (401, 403):
                await resp.aread()
                await resp.aclose()
                self.limiter.release(cand, int((time.time() - t0) * 1000), status)
                self.limiter.cool(cand, 120, f"auth {status}", key_only=True)
                errors.append(f"{cand['provider_name']} auth {status}")
                continue

            # ---- upstream broken ----
            if status >= 500 or status == 408:
                txt = await resp.aread()
                await resp.aclose()
                self.limiter.release(cand, int((time.time() - t0) * 1000), status)
                self.limiter.cool(cand, self.s.short_cooldown, f"upstream {status}", key_only=True)
                errors.append(f"{cand['provider_name']} {status}")
                self.bus.publish({"kind": "retry", "provider": cand["provider_name"],
                                  "key": cand["key_label"], "reason": f"http {status}"})
                continue

            # ---- model not found on this provider -> try next ----
            if status == 404:
                txt = await resp.aread()
                await resp.aclose()
                self.limiter.release(cand, int((time.time() - t0) * 1000), 404)
                self.limiter.cool(cand, 60, "404 model", key_only=True)
                errors.append(f"{cand['provider_name']} 404")
                continue

            # ---- client error: pass through, do not rotate ----
            if status >= 400:
                txt = await resp.aread()
                await resp.aclose()
                self.limiter.release(cand, int((time.time() - t0) * 1000), status)
                try:
                    data = json.loads(txt)
                except ValueError:
                    data = {"error": {"message": txt.decode(errors="replace")}}
                self._log(vm, cand, status, started, attempts, errors, session, 0, 0)
                return RouteResult(status, data, None, cand["provider_name"],
                                   cand["key_label"], attempts)

            # ---- success ----
            created = int(time.time())
            headers_out = {
                "x-router-provider": cand["provider_name"],
                "x-router-key": cand["key_label"],
                "x-router-model": cand["upstream_model"],
                "x-router-attempts": str(attempts),
            }
            prompt_tokens = util.estimate_payload_tokens(body)

            if stream:
                stats = {"prompt_tokens": 0, "completion_tokens": 0, "text_len": 0}
                gen = self._wrap_stream(resp, adapter, vm, cand, created, stats,
                                        prompt_tokens, started, attempts, errors, session)
                return RouteResult(200, None, gen, cand["provider_name"],
                                   cand["key_label"], attempts, headers_out)

            try:
                raw = await resp.aread()
                await resp.aclose()
            finally:
                pass
            data = json.loads(raw) if raw else {}
            pt, ct = adapter.usage(data)
            self.limiter.release(cand, int((time.time() - t0) * 1000), 200)
            self.limiter.add_tokens(cand, pt or prompt_tokens)
            self.limiter.add_tokens(cand, ct)
            self._log(vm, cand, 200, started, attempts, errors, session, pt, ct)
            self.bus.publish({"kind": "ok", "provider": cand["provider_name"],
                              "key": cand["key_label"], "ms": int((time.time() - started) * 1000),
                              "model": vm["name"]})
            return RouteResult(200, adapter.parse(data, vm["name"], created), None,
                               cand["provider_name"], cand["key_label"], attempts, headers_out)

        raise NoCapacity(
            f"All {len(pool)} candidates exhausted for '{model_name}'. Tried: " + "; ".join(errors[:6]))

    async def _wrap_stream(self, resp, adapter, vm, cand, created, stats,
                           prompt_tokens, started, attempts, errors, session):
        try:
            async for piece in adapter.stream(resp, vm["name"], created, stats):
                yield piece
        finally:
            await resp.aclose()
            pt = stats.get("prompt_tokens") or prompt_tokens
            text_len = stats.get("text_len", 0)
            ct = stats.get("completion_tokens") or (max(1, text_len // 4) if text_len else 0)
            self.limiter.release(cand, int((time.time() - started) * 1000), 200)
            self.limiter.add_tokens(cand, pt + ct)
            self._log(vm, cand, 200, started, attempts, errors, session, pt, ct)
            self.bus.publish({"kind": "ok", "provider": cand["provider_name"],
                              "key": cand["key_label"], "ms": int((time.time() - started) * 1000),
                              "model": vm["name"], "stream": True})

    def _log(self, vm, cand, status, started, attempts, errors, session, pt, ct):
        self.store.log(ts=time.time(), vmodel=vm.get("name"), provider=cand["provider_name"],
                       key_label=cand["key_label"], status=status,
                       latency_ms=int((time.time() - started) * 1000), attempts=attempts,
                       prompt_tokens=pt, completion_tokens=ct,
                       error="; ".join(errors[:3]) or None, session=session)

    # ---------------- utility ----------------
    async def list_upstream_models(self, provider_id):
        p = self.store.providers.get(provider_id)
        if not p:
            return []
        keys = [k for k in self.store.keys_by_provider.get(provider_id, []) if k["enabled"]]
        if not keys:
            return []
        fake = {"base_url": p["base_url"], "api_key": keys[0]["api_key"],
                "upstream_model": "", "extra_headers": json.loads(p.get("headers") or "{}")}
        adapter = get_adapter(p["adapter"])
        url, headers = adapter.list_models_url(fake)
        if p["adapter"] == "gemini":
            url = p["base_url"].rstrip("/") + "/models"
            headers = {"x-goog-api-key": keys[0]["api_key"]}
        try:
            r = await self.client.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            if "data" in data:
                return sorted(m.get("id", "") for m in data["data"])
            if "models" in data:
                return sorted(m.get("name", "").split("/")[-1] for m in data["models"])
        except Exception:
            return []
        return []
