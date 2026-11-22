"""Divination cards: price of a full set vs price of what you get when you turn it in.

Card rewards and stack sizes come from the PoE wiki cargo API (items + stackables tables,
checked 22/11/2026: 470 cards, reward is in items.html <span class="divicard-reward">).
Prices come from poe.ninja (Prices). The wiki is fetched from your PC and cached 7 days.
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import time
from pathlib import Path

import httpx

from .config import USER_AGENT
from .pricing import Prices

WIKI = "https://www.poewiki.net/w/api.php"
CACHE_DAYS = 7

_RE_FILE = re.compile(r"\[\[File:[^\]]*\]\]")
_RE_LINK2 = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
_RE_LINK1 = re.compile(r"\[\[([^\]]*)\]\]")
_RE_DISPLAY = re.compile(r'<span class="hoverbox__display[^"]*">.*?</span>', re.S)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_CLS = re.compile(r"tc -(\w+)")
_RE_QTY = re.compile(r"^(\d+)x\s+(.*)$")
_RE_LVL = re.compile(r"^Level (\d+) (.*)$")
_RE_Q = re.compile(r"Quality: \+(\d+)%")


def parse_reward(card_html: str) -> dict | None:
    """-> {cls, lines} from the wiki item html (cls = unique/currency/gem/divination/rare/...)."""
    h = htmlmod.unescape(card_html or "")
    i = h.find('<span class="divicard-reward">')
    if i < 0:
        return None
    h = h[i + len('<span class="divicard-reward">'):]
    j = h.find('<span class="divicard-flavour"')
    if j >= 0:
        h = h[:j]
    m = _RE_CLS.search(h)
    cls = m.group(1) if m else ""
    h = _RE_DISPLAY.sub("", h)
    h = _RE_FILE.sub("", h)
    h = _RE_LINK2.sub(r"\1", h)
    h = _RE_LINK1.sub(r"\1", h)
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = _RE_TAG.sub("", h)
    lines = [ln.strip() for ln in h.split("\n") if ln.strip()]
    return {"cls": cls, "lines": lines}


async def fetch_wiki(cache: Path) -> list[dict]:
    try:
        data = json.loads(cache.read_text())
        if time.time() - data.get("ts", 0) < CACHE_DAYS * 86400 and data.get("cards"):
            return data["cards"]
    except (OSError, ValueError):
        pass
    cards = []
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60) as c:
        for off in range(0, 3000, 500):
            r = await c.get(WIKI, params={
                "action": "cargoquery", "format": "json", "limit": 500, "offset": off,
                "tables": "items,stackables", "join_on": "items._pageID=stackables._pageID",
                "fields": "items.name=name,stackables.stack_size=stack,items.html=html,items.drop_enabled=drop",
                "where": 'items.class_id="DivinationCard"'})
            r.raise_for_status()
            rows = [x["title"] for x in r.json().get("cargoquery", [])]
            for t in rows:
                rw = parse_reward(t.get("html") or "")
                cards.append({"name": htmlmod.unescape(t.get("name") or ""), "stack": int(t.get("stack") or 1),
                              "drop": t.get("drop") == "1", "cls": rw["cls"] if rw else "",
                              "reward": rw["lines"] if rw else []})
            if len(rows) < 500:
                break
    if cards:
        cache.write_text(json.dumps({"ts": time.time(), "cards": cards}, ensure_ascii=False))
    return cards


def _unique_price(prices: Prices, name: str) -> float | None:
    vals = [float(l.get("chaosValue") or 0) for (n, _b), lines in prices.uniques.items() if n == name
            for l in lines if not l.get("links")]
    return min(vals) if vals else None


def _gem_price(prices: Prices, name: str, lvl: int, q: int, corrupted: bool) -> float | None:
    lines = [l for l in prices.gems.get(name, []) if bool(l.get("corrupted")) == corrupted]
    exact = [l for l in lines if int(l.get("gemLevel") or 1) == lvl and int(l.get("gemQuality") or 0) == q]
    if exact:
        return max(float(l.get("chaosValue") or 0) for l in exact)
    near = [l for l in lines if int(l.get("gemLevel") or 1) <= lvl and int(l.get("gemQuality") or 0) <= q]
    if near:
        return float(max(near, key=lambda l: (int(l.get("gemLevel") or 1), int(l.get("gemQuality") or 0))).get("chaosValue") or 0)
    return None


def value_reward(prices: Prices, card: dict) -> tuple[float | None, str]:
    """-> (chaos value of the reward or None, short note)"""
    lines = card.get("reward") or []
    if not lines:
        return None, "không rõ phần thưởng"
    first, rest = lines[0], lines[1:]
    corrupted = any(l == "Corrupted" for l in rest)
    cls = card.get("cls")
    m = _RE_QTY.match(first)
    qty, name = (int(m.group(1)), m.group(2)) if m else (1, first)
    if cls in ("currency", "divination", "white", "normal") or name in prices.stack:
        p = prices.stack.get(name)
        if p:
            return qty * p, ""
    if cls == "unique":
        p = _unique_price(prices, name)
        if p is None:
            return None, "unique ngẫu nhiên / không có giá"
        if True:
            note = "corrupted – giá theo bản thường" if corrupted else ""
            if any("Link" in x for x in rest):
                note = "có link – giá theo bản không link"
            return qty * p, note
    if cls == "gem":
        lm = _RE_LVL.match(first)
        if lm:
            qm = next((_RE_Q.search(x) for x in rest if _RE_Q.search(x)), None)
            p = _gem_price(prices, lm.group(2), int(lm.group(1)), int(qm.group(1)) if qm else 0, corrupted)
            if p is not None:
                return p, ""
        return None, "gem ngẫu nhiên"
    return None, "phần thưởng ngẫu nhiên / không có giá"


def analyse(prices: Prices, cards: list[dict], owned: dict[str, int]) -> dict:
    rows = []
    for c in cards:
        if c["name"] not in prices.stack and c["name"] not in owned:
            continue
        unit = prices.stack.get(c["name"])
        set_price = unit * c["stack"] if unit is not None else None
        rv, note = value_reward(prices, c)
        profit = rv - set_price if rv is not None and set_price is not None else None
        rows.append({"name": c["name"], "stack": c["stack"], "unit": unit, "set_price": set_price,
                     "reward": " · ".join(c["reward"]), "reward_value": rv, "profit": profit,
                     "roi": profit / set_price if profit is not None and set_price else None,
                     "note": note, "owned": owned.get(c["name"], 0), "drop": c["drop"],
                     "icon": prices.icons.get(c["name"], "")})
    rows.sort(key=lambda r: -(r["profit"] if r["profit"] is not None else -1e12))
    return {"rows": rows, "divine": prices.divine}
