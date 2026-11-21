"""Prices from poe.ninja (public, no login, no cookie sent)."""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict

import httpx

from .config import USER_AGENT
from .ratelimit import Limiter

NINJA = "https://poe.ninja/api/data"

CURRENCY_TYPES = ["Currency", "Fragment"]
STACK_TYPES = ["Oil", "Incubator", "Scarab", "Fossil", "Resonator", "Essence", "DivinationCard",
               "DeliriumOrb", "Omen", "Tattoo", "Artifact", "Invitation", "Memory", "AllflameEmber",
               "KalguuranRune", "Vial", "Runegraft", "Coffin"]
UNIQUE_TYPES = ["UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel",
                "UniqueMap", "UniqueRelic", "UniqueTincture"]
OTHER_TYPES = ["SkillGem", "Map", "BlightedMap", "BlightRavagedMap", "BaseType"]

TIMELESS_NAMES = {"Lethal Pride", "Brutal Restraint", "Glorious Vanity", "Elegant Hubris",
                  "Militant Faith", "Heroic Tragedy"}

_NUM = re.compile(r"(\d+)")


def _prop(item: dict, name: str) -> str | None:
    for p in item.get("properties", []) or []:
        if p.get("name") == name and p.get("values"):
            return p["values"][0][0]
    return None


def _prop_int(item: dict, name: str, default: int = 0) -> int:
    v = _prop(item, name)
    m = _NUM.search(v or "")
    return int(m.group(1)) if m else default


def max_links(item: dict) -> int:
    groups: dict[int, int] = defaultdict(int)
    for s in item.get("sockets", []) or []:
        groups[s.get("group", 0)] += 1
    return max(groups.values(), default=0)


class Prices:
    def __init__(self, limiter: Limiter):
        self.limiter = limiter
        self.league = ""
        self.loaded_at = 0.0
        self.stack: dict[str, float] = {"Chaos Orb": 1.0}
        self.icons: dict[str, str] = {}
        self.uniques: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.gems: dict[str, list[dict]] = defaultdict(list)
        self.maps: dict[tuple[str, int], float] = {}
        self.bases: dict[tuple[str, int, str], float] = {}
        self.divine = 0.0
        self.errors: list[str] = []
        self._lock = asyncio.Lock()

    async def _get(self, client: httpx.AsyncClient, kind: str, typ: str) -> list[dict]:
        url = f"{NINJA}/{kind}?league={self.league}&type={typ}"
        try:
            r = await self.limiter.request(client, "ninja", "GET", url)
            if r.status_code != 200:
                self.errors.append(f"{typ}: HTTP {r.status_code}")
                return []
            data = r.json()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{typ}: {e}")
            return []
        if kind == "currencyoverview":
            for d in data.get("currencyDetails", []):
                if d.get("icon"):
                    self.icons[d["name"]] = d["icon"]
        return data.get("lines", [])

    async def refresh(self, league: str, max_age: float = 1800) -> None:
        async with self._lock:
            if league == self.league and time.time() - self.loaded_at < max_age:
                return
            self.__init__(self.limiter)  # reset tables
            self.league = league
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
                for t in CURRENCY_TYPES:
                    for ln in await self._get(c, "currencyoverview", t):
                        self.stack[ln["currencyTypeName"]] = float(ln.get("chaosEquivalent") or 0)
                for t in STACK_TYPES:
                    for ln in await self._get(c, "itemoverview", t):
                        self.stack.setdefault(ln["name"], float(ln.get("chaosValue") or 0))
                        if ln.get("icon"):
                            self.icons.setdefault(ln["name"], ln["icon"])
                for t in UNIQUE_TYPES:
                    for ln in await self._get(c, "itemoverview", t):
                        self.uniques[(ln["name"], ln.get("baseType", ""))].append(ln)
                for ln in await self._get(c, "itemoverview", "SkillGem"):
                    self.gems[ln["name"]].append(ln)
                for t in ("Map", "BlightedMap", "BlightRavagedMap"):
                    for ln in await self._get(c, "itemoverview", t):
                        self.maps[(ln["name"], int(ln.get("mapTier") or 0))] = float(ln.get("chaosValue") or 0)
                for ln in await self._get(c, "itemoverview", "BaseType"):
                    key = (ln.get("baseType") or ln["name"], int(ln.get("levelRequired") or 0), ln.get("variant") or "")
                    self.bases[key] = min(self.bases.get(key, 1e12), float(ln.get("chaosValue") or 0))
            self.divine = self.stack.get("Divine Orb", 0.0)
            self.loaded_at = time.time()

    # ------------------------------------------------------------------
    def price(self, item: dict) -> tuple[float, str]:
        """Return (unit chaos value, category). 0 when unknown."""
        ft = item.get("frameType", 0)
        name = item.get("name") or ""
        type_line = item.get("typeLine") or ""
        base = item.get("baseType") or type_line

        if ft == 5 or (ft == 0 and item.get("stackSize")) or ft == 6:
            v = self.stack.get(type_line) or self.stack.get(base) or 0.0
            return v, "divination" if ft == 6 else "currency"

        if ft in (3, 9, 10):  # unique / relic / foil
            lines = self.uniques.get((name, base), [])
            want_relic = ft in (9, 10)
            links = max_links(item)
            want_links = 6 if links >= 6 else 5 if links == 5 else 0
            cands = [l for l in lines if ("relic" in (l.get("detailsId") or "")) == want_relic] or lines
            exact = [l for l in cands if int(l.get("links") or 0) == want_links] or \
                    [l for l in cands if not l.get("links")] or cands
            if not exact:
                return 0.0, "unique"
            return min(float(l.get("chaosValue") or 0) for l in exact), "unique"

        if ft == 4:  # gem
            lvl = _prop_int(item, "Level", 1)
            q = _prop_int(item, "Quality", 0)
            corrupted = bool(item.get("corrupted"))
            lines = [l for l in self.gems.get(type_line, []) if bool(l.get("corrupted")) == corrupted]
            lines = [l for l in lines if int(l.get("gemLevel") or 1) <= lvl and int(l.get("gemQuality") or 0) <= q]
            if not lines:
                return 0.0, "gem"
            best = max(lines, key=lambda l: (int(l.get("gemLevel") or 1), int(l.get("gemQuality") or 0)))
            return float(best.get("chaosValue") or 0), "gem"

        tier = _prop_int(item, "Map Tier", 0)
        if tier:
            prefix = "Blight-ravaged " if "Blight-ravaged" in type_line else "Blighted " if "Blighted" in type_line else ""
            return self.maps.get((prefix + base, tier), 0.0), "map"

        if ft in (0, 1, 2) and not item.get("corrupted") and item.get("ilvl", 0) >= 82:
            infl = sorted(k.capitalize() for k in (item.get("influences") or {}))
            variant = "/".join(infl)
            if item.get("synthesised"):
                variant = "Synthesised"
            lvl = min(int(item["ilvl"]), 86)
            v = self.bases.get((base, lvl, variant), 0.0)
            return v, "base"

        return 0.0, "other"

    def icon_for(self, item: dict) -> str:
        return item.get("icon") or self.icons.get(item.get("typeLine", ""), "")


def iter_items(items: list[dict]):
    """Yield items plus anything socketed in them (gems, jewels in gear)."""
    for it in items:
        yield it
        yield from it.get("socketedItems", []) or []


def is_timeless(item: dict) -> bool:
    return item.get("frameType") in (3, 9, 10) and item.get("name") in TIMELESS_NAMES
