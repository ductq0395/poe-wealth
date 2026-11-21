"""Rate limiter driven by GGG's X-Rate-Limit-* response headers.

Each bucket (e.g. "stash", "trade-search") learns its rules from the first
response, then keeps a rolling log of request times and waits before it
would exceed (max - safety) hits in any window.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

import httpx


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

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                w = self._wait_time(time.monotonic())
                if w <= 0:
                    break
                await asyncio.sleep(w)
            self.hits.append(time.monotonic())

    def update(self, resp: httpx.Response) -> None:
        rules_hdr = resp.headers.get("x-rate-limit-rules", "")
        new_rules: list[tuple[int, int]] = []
        for rule in [r.strip() for r in rules_hdr.split(",") if r.strip()]:
            spec = resp.headers.get(f"x-rate-limit-{rule.lower()}", "")
            for part in spec.split(","):
                bits = part.split(":")
                if len(bits) == 3:
                    new_rules.append((int(bits[0]), int(bits[1])))
            state = resp.headers.get(f"x-rate-limit-{rule.lower()}-state", "")
            for part in state.split(","):
                bits = part.split(":")
                if len(bits) == 3 and int(bits[2]) > 0:  # currently restricted
                    self.blocked_until = max(self.blocked_until, time.monotonic() + int(bits[2]))
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

    def get(self, name: str) -> Bucket:
        if name not in self.buckets:
            self.buckets[name] = Bucket(name, self.DEFAULTS.get(name, [(10, 10)]))
        return self.buckets[name]

    async def request(self, client: httpx.AsyncClient, bucket: str, method: str, url: str, **kw) -> httpx.Response:
        b = self.get(bucket)
        for attempt in range(3):
            await b.acquire()
            resp = await client.request(method, url, **kw)
            b.update(resp)
            if resp.status_code == 429 and attempt < 2:
                continue
            return resp
        return resp
