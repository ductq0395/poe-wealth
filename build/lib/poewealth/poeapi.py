"""Talks directly to pathofexile.com with the user's POESESSID.

The cookie is only ever sent to www.pathofexile.com — never to poe.ninja
or anywhere else.
"""
from __future__ import annotations

import httpx

from .config import USER_AGENT, Config
from .ratelimit import Limiter

POE = "https://www.pathofexile.com"


class PoeError(RuntimeError):
    pass


class PoeClient:
    def __init__(self, cfg: Config, limiter: Limiter):
        self.cfg = cfg
        self.limiter = limiter
        cookies = {"POESESSID": cfg.poesessid} if cfg.poesessid else {}
        self.client = httpx.AsyncClient(
            base_url=POE,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            cookies=cookies,
            timeout=30,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _json(self, bucket: str, method: str, url: str, **kw):
        resp = await self.limiter.request(self.client, bucket, method, url, **kw)
        if resp.status_code in (401, 403):
            raise PoeError(f"{url}: {resp.status_code} – POESESSID sai/hết hạn hoặc profile đang private?")
        if resp.status_code == 429:
            raise PoeError(f"{url}: bị rate limit, thử lại sau")
        if resp.status_code >= 400:
            raise PoeError(f"{url}: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            raise PoeError(f"{url}: {data['error']}")
        return data

    # ---------- account / stash ----------
    def _acc(self) -> dict:
        if not self.cfg.account_name:
            raise PoeError("Chưa nhập account name (dạng ten#1234)")
        return {"accountName": self.cfg.account_name, "realm": self.cfg.realm}

    async def leagues(self) -> list[str]:
        data = await self._json("misc", "GET", "/api/trade/data/leagues")
        return [l["id"] for l in data.get("result", []) if l.get("realm", "pc") == self.cfg.realm]

    async def stash_tabs(self) -> list[dict]:
        params = {**self._acc(), "league": self.cfg.league, "tabs": 1, "tabIndex": 0}
        data = await self._json("stash", "GET", "/character-window/get-stash-items", params=params)
        return data.get("tabs", [])

    async def stash_items(self, tab_index: int) -> list[dict]:
        params = {**self._acc(), "league": self.cfg.league, "tabs": 0, "tabIndex": tab_index}
        data = await self._json("stash", "GET", "/character-window/get-stash-items", params=params)
        return data.get("items", [])

    async def characters(self) -> list[dict]:
        data = await self._json("chars", "POST", "/character-window/get-characters", data=self._acc())
        return [c for c in data if c.get("league") == self.cfg.league]

    async def character_items(self, name: str) -> list[dict]:
        data = await self._json("chars", "POST", "/character-window/get-items",
                                data={**self._acc(), "character": name})
        return data.get("items", [])

    # ---------- trade (used for timeless jewels) ----------
    async def trade_stats(self) -> dict:
        return await self._json("misc", "GET", "/api/trade/data/stats")

    async def trade_search(self, query: dict) -> dict:
        return await self._json("trade-search", "POST", f"/api/trade/search/{self.cfg.league}", json=query)

    async def trade_fetch(self, ids: list[str], query_id: str) -> list[dict]:
        data = await self._json("trade-fetch", "GET", f"/api/trade/fetch/{','.join(ids[:10])}",
                                params={"query": query_id})
        return data.get("result", [])
