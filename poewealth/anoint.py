"""Anoint (Blight oils on an amulet): notable -> 3 oils, priced by poe.ninja.

Data from the PoE wiki cargo API (checked 22/11/2026): blight_crafting_recipes (472 recipes with a
passive_id, type UniqueOrAmulet) + blight_crafting_recipes_items (item_id, ordinal) + passive_skills
(name, stat_text) + items (oil name by metadata_id). Fetched from your PC, cached 7 days.
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import time
from pathlib import Path

import httpx

from .config import USER_AGENT

WIKI = "https://www.poewiki.net/w/api.php"
CACHE_DAYS = 7


def _clean_stat(t: str) -> str:
    t = htmlmod.unescape(t or "")
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"<br\s*/?>", "\n", t)
    return re.sub(r"<[^>]+>", "", t).strip()


async def _all(c: httpx.AsyncClient, **params) -> list[dict]:
    out = []
    for off in range(0, 10000, 500):
        r = await c.get(WIKI, params={"action": "cargoquery", "format": "json", "limit": 500, "offset": off, **params})
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"wiki: {j['error'].get('info')}")
        rows = [x["title"] for x in j.get("cargoquery", [])]
        out += rows
        if len(rows) < 500:
            break
    return out


async def fetch(cache: Path) -> list[dict]:
    try:
        data = json.loads(cache.read_text())
        if time.time() - data.get("ts", 0) < CACHE_DAYS * 86400 and data.get("anoints"):
            return data["anoints"]
    except (OSError, ValueError):
        pass
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60) as c:
        recs = await _all(c, tables="blight_crafting_recipes,passive_skills",
                          join_on="blight_crafting_recipes.passive_id=passive_skills.id",
                          fields="blight_crafting_recipes.id=rid,passive_skills.name=name,passive_skills.stat_text=stat,"
                                 "passive_skills.is_anointment_only=ao,passive_skills.is_keystone=ks",
                          where="blight_crafting_recipes.passive_id IS NOT NULL")
        parts = await _all(c, tables="blight_crafting_recipes_items",
                           fields="blight_crafting_recipes_items.recipe_id=rid,blight_crafting_recipes_items.item_id=item,"
                                  "blight_crafting_recipes_items.ordinal=ord")
        oils = await _all(c, tables="items", fields="items.name=name,items.metadata_id=mid",
                          where='items.metadata_id LIKE "Metadata/Items/Currency/Mushrune%"')
    oil_name = {o["mid"]: htmlmod.unescape(o["name"]) for o in oils}
    by_rec: dict[str, list[tuple[int, str]]] = {}
    for p in parts:
        by_rec.setdefault(p["rid"], []).append((int(p.get("ord") or 0), oil_name.get(p["item"], p["item"].rsplit("/", 1)[-1])))
    out = []
    for r in recs:
        ol = [n for _o, n in sorted(by_rec.get(r["rid"], []))]
        if not ol or not r.get("name"):
            continue
        out.append({"name": htmlmod.unescape(r["name"]), "stat": _clean_stat(r.get("stat") or ""), "oils": ol,
                    "anoint_only": r.get("ao") == "1", "keystone": r.get("ks") == "1"})
    out.sort(key=lambda x: x["name"])
    if out:
        cache.write_text(json.dumps({"ts": time.time(), "anoints": out}, ensure_ascii=False))
    return out
