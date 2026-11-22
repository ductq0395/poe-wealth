"""Stash clean-up ("Dọn rương"): chaos vendor recipe sets + cheap items to throw away.

Each snapshot stores a light copy of every item in the tracked stash tabs (stash_items.json in the
config dir, per league). Item slot comes from the icon art path, e.g.
https://web.poecdn.com/gen/image/<base64 json {"f":"2DItems/Armours/Helmets/..."}>/hash/x.png
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from .pricing import Prices

SLOTS = ["weapon", "helmet", "body", "gloves", "boots", "belt", "amulet", "ring"]
_ART = [
    ("Armours/Helmets", "helmet"), ("Armours/BodyArmours", "body"), ("Armours/Gloves", "gloves"),
    ("Armours/Boots", "boots"), ("Armours/Shields", "shield"), ("2DItems/Rings", "ring"),
    ("2DItems/Amulets", "amulet"), ("2DItems/Belts", "belt"), ("Weapons/TwoHandWeapons", "2h"),
    ("Weapons/OneHandWeapons", "1h"), ("2DItems/Quivers", "quiver"), ("2DItems/Flasks", "flask"),
    ("2DItems/Jewels", "jewel"), ("2DItems/Maps", "map"), ("2DItems/Gems", "gem"),
    ("2DItems/Currency", "currency"), ("2DItems/Divination", "divination"),
]


def art_path(icon: str) -> str:
    if not icon:
        return ""
    m = re.search(r"/gen/image/([^/]+)/", icon)
    if m:
        b = m.group(1)
        try:
            data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
            for part in data if isinstance(data, list) else [data]:
                if isinstance(part, dict) and part.get("f"):
                    return part["f"]
        except (ValueError, TypeError):
            return ""
    m = re.search(r"/image/(Art/[^?]+)", icon)
    return m.group(1) if m else ""


def slot_of(item: dict) -> str:
    art = art_path(item.get("icon", ""))
    for key, slot in _ART:
        if key in art:
            return slot
    return ""


def light_item(it: dict, source: str, label: str, prices: Prices) -> dict:
    try:
        unit, cat = prices.price(it)
    except Exception:  # noqa: BLE001
        unit, cat = 0.0, "other"
    qty = int(it.get("stackSize") or 1)
    n, t = it.get("name") or "", it.get("typeLine") or ""
    return {"source": source, "label": label, "x": it.get("x"), "y": it.get("y"), "w": it.get("w"), "h": it.get("h"),
            "name": n, "typeLine": t, "baseType": it.get("baseType") or t, "frame": it.get("frameType", 0),
            "ilvl": it.get("ilvl", 0), "identified": it.get("identified", True), "corrupted": bool(it.get("corrupted")),
            "qty": qty, "unit": unit, "total": unit * qty, "cat": cat, "slot": slot_of(it), "icon": it.get("icon", "")}


def save(path: Path, league: str, items: list[dict], ts: float) -> None:
    data = {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    data[league] = {"ts": ts, "items": items}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def load(path: Path, league: str) -> dict:
    try:
        return json.loads(path.read_text()).get(league) or {}
    except (OSError, ValueError):
        return {}


# ---------------- vendor recipe ----------------
def recipe(items: list[dict], min_value: float = 0.0) -> dict:
    """Count chaos recipe sets (rare ilvl >= 60, at least one 60-74 item per set).
    Rares worth more than min_value chaos (poe.ninja base price) are left out."""
    pool = {s: {"low": 0, "high": 0, "unid_low": 0, "unid_high": 0} for s in SLOTS + ["1h", "2h", "shield"]}
    used = []
    for it in items:
        if it["frame"] != 2 or it["ilvl"] < 60 or not it["slot"]:
            continue
        if it["slot"] not in pool or (min_value and it["unit"] > min_value):
            continue
        band = "low" if it["ilvl"] < 75 else "high"
        pool[it["slot"]][band] += 1
        if not it["identified"]:
            pool[it["slot"]]["unid_" + band] += 1
        used.append(it)

    def units(band_keys):
        c = {s: sum(pool[s][b] for b in band_keys) for s in pool}
        # one-handers pair with each other or with a shield
        oh = c["1h"]
        weapon = c["2h"] + min(oh, c["shield"]) + max(0, oh - c["shield"]) // 2
        per = {"weapon": weapon, "helmet": c["helmet"], "body": c["body"], "gloves": c["gloves"],
               "boots": c["boots"], "belt": c["belt"], "amulet": c["amulet"], "ring": c["ring"] // 2}
        return per, min(per.values())

    per_all, total_sets = units(("low", "high"))
    low_total = sum(pool[s]["low"] for s in pool)
    chaos_sets = min(total_sets, low_total)
    missing = sorted(per_all, key=lambda s: per_all[s])
    need = [s for s in SLOTS if per_all[s] == total_sets]   # slots that limit the next set
    return {"pool": pool, "per_set": per_all, "sets": total_sets,
            "chaos_sets": chaos_sets, "low_items": low_total,
            "bottleneck": need, "order": missing, "items": len(used)}


# ---------------- junk ----------------
KEEP_CATS = {"currency", "divination"}


def junk(items: list[dict], max_total: float, include_stack: bool = False) -> list[dict]:
    out = []
    for it in items:
        if it["total"] >= max_total:
            continue
        if not include_stack and (it["cat"] in KEEP_CATS or it["frame"] in (5, 6)):
            continue
        if it["frame"] == 2 and it["ilvl"] >= 60 and it["slot"] in SLOTS + ["1h", "2h", "shield"]:
            kind = "rare (dùng cho recipe)"
        elif it["frame"] == 2:
            kind = "rare"
        elif it["frame"] in (0, 1):
            kind = {"gem": "gem", "map": "map", "flask": "flask"}.get(it["slot"], "đồ trắng / magic")
        elif it["frame"] == 4:
            kind = "gem"
        elif it["frame"] in (3, 9, 10):
            kind = "unique"
        else:
            kind = "khác"
        out.append({**it, "kind": kind})
    out.sort(key=lambda x: (x["label"], x["y"] or 0, x["x"] or 0))
    return out
