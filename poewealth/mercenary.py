"""Price Mercenary Warrants (league 3.30) through the official trade API.

Trade site facts (checked 22/11/2026):
- /api/trade/data/stats has a "mercenary" group: mercenary.skill_<hash> (skill name) and
  mercenary.support_<hash> ("Faster Casting (Tier 2)"); each support tier is its own hash.
- Stat group type "mercenary" = "Mercenary Skill Group" on the site: the supports must sit on
  THAT skill (stricter than "and").
- Trade items carry item.mercenarySkills = [{hash, name, supports:[{hash, name, tier}]}] and a
  "Mercenary Level" property. There is no level filter, so level is filtered on fetched listings.
- Queries get "too complex" fast (anonymous: ~1 group of 3 filters); logged in allows more.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from .poeapi import PoeClient, PoeError
from .pricing import Prices
from .timeless import TRADE_CURRENCY

CACHE_SECONDS = 30 * 60
FETCH_MAX = 30           # cheapest listings fetched (3 fetch calls) – level is filtered on these
_TIER = re.compile(r"^(.*?)\s*\(Tier:?\s*(\d+)\)\s*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()


class MercCatalog:
    """skill/support name -> trade stat id, from /api/trade/data/stats."""

    def __init__(self):
        self.skills: dict[str, list[str]] = {}     # "abyssal spray" -> ["mercenary.skill_50122"] (a few names have 2 ids)
        self.supports: dict[str, str] = {}         # "faster casting|2" -> "mercenary.support_38851"
        self.text: dict[str, str] = {}             # id -> display text
        self.loaded = 0.0

    async def ensure(self, client: PoeClient) -> None:
        if self.skills and time.time() - self.loaded < 24 * 3600:
            return
        data = await client.trade_stats()
        skills, sups, text = {}, {}, {}
        for g in data.get("result", []):
            if g.get("id") != "mercenary":
                continue
            for e in g.get("entries", []):
                sid, t = e.get("id", ""), e.get("text", "")
                text[sid] = t
                if sid.startswith("mercenary.skill_"):
                    skills.setdefault(_norm(t), []).append(sid)
                elif sid.startswith("mercenary.support_"):
                    m = _TIER.match(t)
                    if m:
                        sups.setdefault(f"{_norm(m.group(1))}|{m.group(2)}", sid)
        if not skills:
            raise PoeError("Trade site không trả về nhóm stat 'mercenary' (đổi league / GGG đổi API?)")
        self.skills, self.supports, self.text, self.loaded = skills, sups, text, time.time()

    def skill_ids(self, name: str) -> list[str]:
        return self.skills.get(_norm(name), [])

    def support_id(self, name: str, tier: int | str) -> str | None:
        return self.supports.get(f"{_norm(name)}|{tier}")


def parse_warrant_text(text: str) -> dict:
    """Ctrl+C text of a Mercenary Warrant -> {name, build, level, skills:[{name, supports:[{name, tier}]}]}"""
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    blocks, cur = [], []
    for ln in lines:
        if ln.strip().startswith("--------"):
            blocks.append(cur); cur = []
        elif ln.strip():
            cur.append(ln.strip())
    blocks.append(cur)
    blocks = [b for b in blocks if b]
    if not blocks or not any("Mercenary Warrant" in ln for ln in blocks[0]):
        raise ValueError("Không phải Mercenary Warrant (dán nguyên văn Ctrl+C trong game, bản thường hoặc Ctrl+Alt+C)")
    out = {"name": "", "build": "", "level": None, "skills": []}
    for b in blocks[1:]:
        first = b[0]
        if first.startswith(("Right click", "Right-click", "Open a portal", "Note:", "Corrupted")):
            continue
        if any(ln.startswith("Build:") or ln.startswith("Mercenary Level:") for ln in b):
            for ln in b:
                if ln.startswith("Build:"):
                    out["build"] = ln.split(":", 1)[1].strip()
                elif ln.startswith("Mercenary Level:"):
                    m = re.search(r"\d+", ln)
                    out["level"] = int(m.group()) if m else None
            continue
        if not out["name"] and not out["build"] and not out["skills"] and len(b) == 1:
            out["name"] = first                       # "Aquilo, the Lightless"
            continue
        if _TIER.match(first):                        # a block can't start with a support
            continue
        sups = []
        for ln in b[1:]:
            m = _TIER.match(ln)
            if m:
                sups.append({"name": m.group(1).strip(), "tier": int(m.group(2))})
        out["skills"].append({"name": first, "supports": sups})
    if not out["skills"]:
        raise ValueError("Không đọc được skill nào trong warrant")
    return out


def parse_warrant_item(item: dict) -> dict | None:
    """Stash/trade item JSON -> same shape as parse_warrant_text, with ids already known."""
    skills = item.get("mercenarySkills")
    if not skills:
        return None
    props = {p.get("name"): (p.get("values") or [[None]])[0][0] for p in item.get("properties", [])}
    first = (item.get("properties") or [{}])[0]
    name = (first.get("values") or [[""]])[0][0] if not first.get("name") else ""
    lvl = props.get("Mercenary Level")
    return {"name": name, "build": props.get("Build") or "", "level": int(lvl) if lvl and str(lvl).isdigit() else None,
            "skills": [{"name": s.get("name", ""), "id": f"mercenary.skill_{s['hash']}",
                        "supports": [{"name": x.get("name", ""), "tier": x.get("tier"), "id": f"mercenary.support_{x['hash']}"}
                                     for x in s.get("supports", []) or []]} for s in skills]}


def resolve(cat: MercCatalog, w: dict) -> dict:
    """Attach trade stat ids to parsed skills/supports; unknown names get id None."""
    for s in w["skills"]:
        if not s.get("id"):
            ids = cat.skill_ids(s["name"])
            s["id"] = ids[0] if ids else None
            s["alt_ids"] = ids[1:]
        for x in s["supports"]:
            if not x.get("id"):
                x["id"] = cat.support_id(x["name"], x["tier"])
    return w


def build_query(groups: list[dict]) -> dict:
    """groups: [{skill: id|None, supports:[id,...]}] -> trade query (instant buyout, priced, cheapest first)."""
    stats = []
    for g in groups:
        f = ([{"id": g["skill"]}] if g.get("skill") else []) + [{"id": s} for s in g.get("supports", []) if s]
        if f:
            stats.append({"type": "mercenary", "filters": f})
    if not stats:
        raise ValueError("Chưa chọn skill/support nào")
    return {
        "query": {
            "status": {"option": "securable"},
            "type": "Mercenary Warrant",
            "stats": stats,
            "filters": {"trade_filters": {"filters": {"sale_type": {"option": "priced"}}}},
        },
        "sort": {"price": "asc"},
    }


def trade_url(league: str, query: dict) -> str:
    from urllib.parse import quote
    return f"https://www.pathofexile.com/trade/search/{quote(league)}?q={quote(json.dumps(query, separators=(',', ':')))}"


def warrant_text(w: dict) -> str:
    """Rebuild the in-game Ctrl+C text from parsed warrant data (for the paste box)."""
    parts = ["Item Class: Map Fragments\nRarity: Normal\nMercenary Warrant"]
    if w.get("name"):
        parts.append(w["name"])
    meta = ([f"Build: {w['build']}"] if w.get("build") else []) + \
           ([f"Mercenary Level: {w['level']}"] if w.get("level") is not None else [])
    if meta:
        parts.append("\n".join(meta))
    for s in w.get("skills", []):
        parts.append("\n".join([s["name"]] + [f"{x['name']} (Tier: {x['tier']})" for x in s.get("supports", [])]))
    return "\n--------\n".join(parts) + "\n"


def is_warrant(item: dict) -> bool:
    return bool(item.get("mercenarySkills")) or item.get("baseType") == "Mercenary Warrant" \
        or item.get("typeLine") == "Mercenary Warrant"


def collect_warrants(items: list[dict], source: str, label: str) -> list[dict]:
    out = []
    for it in items:
        if not is_warrant(it):
            continue
        w = parse_warrant_item(it) or {"name": "", "build": "", "level": None, "skills": []}
        w.update(id=it.get("id"), source=source, label=label, x=it.get("x"), y=it.get("y"),
                 icon=it.get("icon"), text=warrant_text(w))
        out.append(w)
    return out


def save_stash(path, league: str, warrants: list[dict], ts: float, snapshot_id: int | None) -> None:
    data = {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    data[league] = {"ts": ts, "snapshot_id": snapshot_id, "warrants": warrants}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def load_stash(path, league: str) -> dict:
    try:
        return json.loads(path.read_text()).get(league) or {}
    except (OSError, ValueError):
        return {}


class MercPricer:
    def __init__(self, prices: Prices):
        self.prices = prices
        self.catalog = MercCatalog()
        self.cache: dict[str, tuple[float, dict]] = {}
        self.client_factory = None   # set by app: () -> PoeClient

    def _to_chaos(self, price: dict | None) -> float | None:
        if not price or price.get("amount") is None:
            return None
        name = TRADE_CURRENCY.get(price.get("currency", ""))
        rate = self.prices.stack.get(name) if name else None
        return float(price["amount"]) * rate if rate else None

    async def parse(self, text: str) -> dict:
        w = parse_warrant_text(text)
        client = self.client_factory()
        try:
            await self.catalog.ensure(client)
        finally:
            await client.close()
        return resolve(self.catalog, w)

    async def price(self, league: str, groups: list[dict], min_level: int = 0, force: bool = False) -> dict:
        query = build_query(groups)
        key = hashlib.sha1(json.dumps([league, query, min_level], sort_keys=True).encode()).hexdigest()
        hit = self.cache.get(key)
        if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
            return {**hit[1], "cached": True, "age": time.time() - hit[0]}
        if not self.prices.stack:
            await self.prices.refresh(league)
        client = self.client_factory()
        try:
            res = await client.trade_search(query)
            ids, total = res.get("result", []), int(res.get("total", 0))
            listings = []
            for i in range(0, min(len(ids), FETCH_MAX), 10):
                for l in await client.trade_fetch(ids[i:i + 10], res["id"]):
                    it, lst = l.get("item", {}) or {}, l.get("listing", {}) or {}
                    w = parse_warrant_item(it) or {}
                    p = lst.get("price") or {}
                    listings.append({"chaos": self._to_chaos(p), "amount": p.get("amount"), "currency": p.get("currency"),
                                     "level": w.get("level"), "name": w.get("name") or "", "build": w.get("build") or "",
                                     "seller": ((lst.get("account") or {}).get("name") or ""),
                                     "skills": [s["name"] for s in w.get("skills", [])]})
                # stop early once we have 5 usable listings at the wanted level
                ok = [x for x in listings if x["chaos"] and (x["level"] or 0) >= min_level]
                if len(ok) >= 5:
                    break
        finally:
            await client.close()
        ok = sorted((x for x in listings if x["chaos"] and (x["level"] or 0) >= min_level), key=lambda x: x["chaos"])
        top = ok[:5]
        price = float(math.floor(sum(x["chaos"] for x in top) / len(top))) if top else 0.0
        if not ids:
            detail = "không có ai bán instant buyout"
        elif not top:
            detail = f"{len(listings)} listing rẻ nhất đều dưới level {min_level} hoặc giá lạ" if min_level else "listing có giá lạ"
        else:
            detail = f"TB {len(top)} giá rẻ nhất" + (f" (level ≥ {min_level})" if min_level else "") + ": " + \
                     ", ".join(f"{x['chaos']:.0f}" for x in top)
        out = {"price": price, "total": total, "detail": detail, "listings": listings,
               "trade_url": trade_url(league, query), "query": query, "min_level": min_level,
               "fetched": time.time(), "cached": False, "age": 0}
        self.cache[key] = (time.time(), out)
        return out
