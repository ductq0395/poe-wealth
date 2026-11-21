"""Prices from poe.ninja (public, no login, no cookie sent)."""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict

import httpx

from .config import USER_AGENT
from .ratelimit import Limiter

NINJA = "https://poe.ninja/poe1/api/economy"
POECDN = "https://web.poecdn.com"

# poe.ninja (from 2025) splits data in two APIs. Verified 19/11/2026 (league Covens).
EXCHANGE_TYPES = ["Currency", "Fragment", "Oil", "Scarab", "Fossil", "Resonator", "Essence", "DivinationCard",
                  "DeliriumOrb", "Omen", "Tattoo", "Artifact", "AllflameEmber", "Runegraft", "Astrolabe", "Thread",
                  "Ducat", "EnshroudingCrystal"]
STASH_STACK_TYPES = ["Invitation", "Memory", "Incubator", "Wombgift", "Beast", "IncursionTemple", "Vial",
                     "KalguuranRune", "Coffin", "Corpse", "Viscera", "ScryingOrb"]
UNIQUE_TYPES = ["UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel",
                "UniqueMap", "UniqueRelic", "UniqueTincture", "ForbiddenJewel"]
MAP_TYPES = ["Map", "BlightedMap", "BlightRavagedMap"]

TIMELESS_NAMES = {"Lethal Pride", "Brutal Restraint", "Glorious Vanity", "Elegant Hubris",
                  "Militant Faith", "Heroic Tragedy"}

_NUM = re.compile(r"(\d+)")


def _prop(item: dict, name: str) -> str | None:
    for p in item.get("properties", []) or []:
        if p.get("name") == name and p.get("values"):
            v = p["values"][0]
            v = v[0] if isinstance(v, (list, tuple)) and v else v
            return v if isinstance(v, str) else str(v)
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
        self.maps: dict[str, tuple[float, int]] = {}
        self.bases: dict[tuple[str, int, str], float] = {}
        self.divine = 0.0
        self.by_type: dict[str, list[dict]] = defaultdict(list)   # poe.ninja type -> [{name, chaos, icon}]
        self.errors: list[str] = []
        self._lock = asyncio.Lock()

    async def _get(self, client: httpx.AsyncClient, api: str, typ: str) -> dict:
        path = "exchange/current/overview" if api == "exchange" else "stash/current/item/overview"
        url = f"{NINJA}/{path}"
        try:
            r = await self.limiter.request(client, "ninja", "GET", url, params={"league": self.league, "type": typ})
            if r.status_code != 200:
                self.errors.append(f"{typ}: HTTP {r.status_code}")
                return {}
            return r.json()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{typ}: {e}")
            return {}

    async def refresh(self, league: str, max_age: float = 1800) -> None:
        async with self._lock:
            if league == self.league and time.time() - self.loaded_at < max_age:
                return
            self.__init__(self.limiter)  # reset tables
            self.league = league
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
                for t in EXCHANGE_TYPES:
                    data = await self._get(c, "exchange", t)
                    meta = {i["id"]: i for i in data.get("items", [])}
                    for ln in data.get("lines", []):
                        it = meta.get(ln.get("id"))
                        if not it:
                            continue
                        self.stack.setdefault(it["name"], float(ln.get("primaryValue") or 0))
                        if it.get("image"):
                            self.icons.setdefault(it["name"], POECDN + it["image"])
                        self.by_type[t].append({"name": it["name"], "chaos": float(ln.get("primaryValue") or 0),
                                                "icon": POECDN + it["image"] if it.get("image") else ""})
                for t in STASH_STACK_TYPES:
                    for ln in (await self._get(c, "stash", t)).get("lines", []):
                        self.stack.setdefault(ln["name"], float(ln.get("chaosValue") or 0))
                        if ln.get("icon"):
                            self.icons.setdefault(ln["name"], ln["icon"])
                        self.by_type[t].append({"name": ln["name"], "chaos": float(ln.get("chaosValue") or 0),
                                                "icon": ln.get("icon") or ""})
                for t in UNIQUE_TYPES:
                    for ln in (await self._get(c, "stash", t)).get("lines", []):
                        self.uniques[(ln["name"], ln.get("baseType") or "")].append(ln)
                for ln in (await self._get(c, "stash", "SkillGem")).get("lines", []):
                    self.gems[ln["name"]].append(ln)
                for t in MAP_TYPES:
                    for ln in (await self._get(c, "stash", t)).get("lines", []):
                        name, cnt = ln["name"], int(ln.get("count") or 0)
                        old = self.maps.get(name)
                        if not old or cnt > old[1]:      # several generations: keep the most traded
                            self.maps[name] = (float(ln.get("chaosValue") or 0), cnt)
                for ln in (await self._get(c, "stash", "BaseType")).get("lines", []):
                    key = (ln.get("baseType") or ln["name"], int(ln.get("levelRequired") or 0), ln.get("variant") or "")
                    self.bases[key] = min(self.bases.get(key, 1e12), float(ln.get("chaosValue") or 0))
            self.stack["Chaos Orb"] = 1.0
            self.divine = self.stack.get("Divine Orb", 0.0)
            self.loaded_at = time.time()
            if not self.divine:
                self.errors.insert(0, f"không có giá Divine cho league '{league}' – sai tên league?")

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
        if tier or type_line.endswith("Map") or base.startswith("Map (Tier"):
            cands = [type_line, base, f"{type_line} (Tier {tier})", f"{name} {base}".strip(), f"{name} {type_line}".strip()]
            if base.startswith("Map (Tier") or type_line.startswith("Map (Tier"):
                cands.insert(0, f"{type_line.split(' Map (Tier')[0]} {base}")
            for cand in cands:
                if cand in self.maps:
                    return self.maps[cand][0], "map"
            return 0.0, "map"

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
