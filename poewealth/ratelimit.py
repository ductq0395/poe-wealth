"""Rate limiter driven by GGG's X-Rate-Limit-* response headers.

Each bucket (e.g. "stash", "trade-search") learns its rules from the first
response, then keeps a rolling log of request times and waits before it
would exceed (max - safety) hits in any window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

log = logging.getLogger("poewealth.http")
MAX_WAIT = 300  # seconds; longer waits fail loudly instead of hanging


class RateLimited(RuntimeError):
    pass


class Bucket:
    def __init__(self, name: str, default_rules: list[tuple[int, int]]):
        self.name = name
        self.rules = list(default_rules)      # [(max_hits, period_seconds)]
        self.hits: deque[float] = deque()
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    def _wait_time(self, now: float) -> float:
        wait = max(0.0, self.blocked_until - now)
        longest = max((p for _, p in self.rules), default=0)
        while self.hits and now - self.hits[0] > longest:
            self.hits.popleft()
        for max_hits, period in self.rules:
            allowed = max(1, max_hits - 1)  # keep 1 hit of headroom
            recent = [t for t in self.hits if now - t < period]
            if len(recent) >= allowed:
                wait = max(wait, period - (now - recent[-allowed]) + 0.25)
        return wait

    async def acquire(self, on_wait=None) -> None:
        async with self.lock:
            while True:
                w = self._wait_time(time.monotonic())
                if w <= 0:
                    break
                if w > MAX_WAIT:
                    raise RateLimited(f"GGG rate limit ({self.name}): phải chờ {w/60:.0f} phút. "
                                      "Có máy/tool khác đang dùng cùng tài khoản?")
                log.info("bucket %s: chờ %.1fs (rules=%s)", self.name, w, self.rules)
                if on_wait:
                    on_wait(self.name, w)
                await asyncio.sleep(min(w, 5))
            self.hits.append(time.monotonic())

    def update(self, resp: httpx.Response) -> None:
        rules_hdr = resp.headers.get("x-rate-limit-rules", "")
        new_rules: list[tuple[int, int]] = []
        now = time.monotonic()
        for rule in [r.strip() for r in rules_hdr.split(",") if r.strip()]:
            spec = [p.split(":") for p in resp.headers.get(f"x-rate-limit-{rule.lower()}", "").split(",")]
            state = [p.split(":") for p in resp.headers.get(f"x-rate-limit-{rule.lower()}-state", "").split(",")]
            for bits in spec:
                if len(bits) == 3:
                    new_rules.append((int(bits[0]), int(bits[1])))
            for bits in state:
                if len(bits) != 3:
                    continue
                hits, period, restricted = int(bits[0]), int(bits[1]), int(bits[2])
                if restricted > 0:  # server says we are locked out
                    self.blocked_until = max(self.blocked_until, now + restricted)
                # server-side count includes requests from OTHER machines on the same account/IP
                lim = next((int(b[0]) for b in spec if len(b) == 3 and int(b[1]) == period), None)
                if lim and hits >= lim - 1:
                    self.blocked_until = max(self.blocked_until, now + period / 2)
        log.info("%s %s -> %s | rules=%s %s", resp.request.method, resp.request.url.copy_with(query=None),
                 resp.status_code, rules_hdr,
                 " ".join(f"{k}={v}" for k, v in resp.headers.items() if k.startswith("x-rate-limit-") and k != "x-rate-limit-rules"))
        if new_rules:
            self.rules = new_rules
        if resp.status_code == 429:
            retry = int(resp.headers.get("retry-after", "60"))
            self.blocked_until = max(self.blocked_until, time.monotonic() + retry)


class Limiter:
    DEFAULTS = {
        "stash": [(30, 60), (100, 1800)],
        "chars": [(30, 60)],
        "trade-search": [(5, 10), (15, 60), (30, 300)],
        "trade-fetch": [(12, 4), (16, 12)],
        "ninja": [(20, 10)],
        "misc": [(10, 10)],
    }

    def __init__(self) -> None:
        self.buckets: dict[str, Bucket] = {}
        self.on_wait = None  # callback(bucket, seconds) for UI progress

    def get(self, name: str) -> Bucket:
        if name not in self.buckets:
            self.buckets[name] = Bucket(name, self.DEFAULTS.get(name, [(10, 10)]))
        return self.buckets[name]

    def state(self) -> dict:
        now = time.monotonic()
        return {n: {"rules": b.rules, "blocked_for": max(0, round(b.blocked_until - now)),
                    "hits_last_60s": sum(1 for t in b.hits if now - t < 60)} for n, b in self.buckets.items()}

    async def request(self, client: httpx.AsyncClient, bucket: str, method: str, url: str, **kw) -> httpx.Response:
        b = self.get(bucket)
        for attempt in range(3):
            await b.acquire(self.on_wait)
            try:
                resp = await client.request(method, url, **kw)
            except httpx.TimeoutException as e:
                log.warning("%s %s timeout: %s", method, url, e)
                raise RateLimited(f"Hết thời gian chờ khi gọi {url.split('?')[0]} – mạng/DNS/firewall?") from e
            b.update(resp)
            if resp.status_code == 429 and attempt < 2:
                continue
            return resp
        return resp
