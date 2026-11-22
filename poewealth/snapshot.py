"""Take a wealth snapshot: fetch tracked tabs + characters, price, store."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

from .config import Config
from .db import DB
from .poeapi import PoeClient
from .pricing import Prices, _prop_int, is_timeless, iter_items, max_links
from .timeless import TimelessPricer, mod_lines
from . import chest, mercenary
from .config import CONFIG_DIR

WARRANT_FILE = CONFIG_DIR / "warrants.json"
STASH_FILE = CONFIG_DIR / "stash_items.json"     # light copy of tracked tab items (Dọn rương, Bài div)

progress = {"running": False, "message": "", "error": ""}
_lock = asyncio.Lock()


def _display_name(item: dict) -> str:
    n, t = item.get("name") or "", item.get("typeLine") or ""
    return f"{n} {t}".strip() if n and item.get("frameType") in (3, 9, 10) else (t or n)


def _variant(item: dict, cat: str) -> str:
    if cat == "gem":
        v = f"L{_prop_int(item, 'Level', 1)} Q{_prop_int(item, 'Quality', 0)}"
        return v + (" corrupted" if item.get("corrupted") else "")
    if cat == "unique":
        l = max_links(item)
        return f"{l}L" if l >= 5 else ""
    if cat == "map":
        return f"T{_prop_int(item, 'Map Tier', 0)}"
    if cat == "base":
        infl = "/".join(sorted(item.get("influences") or {}))
        return f"ilvl{item.get('ilvl')} {infl}".strip()
    return ""


def price_items(items: list[dict], source: str, cfg: Config, prices: Prices, timeless: TimelessPricer):
    agg: dict[str, dict] = {}
    other_total, other_qty = 0.0, 0
    to_queue = []
    bad: list[dict] = []
    for it in iter_items(items):
        try:
            qty = float(it.get("stackSize") or 1)
            if is_timeless(it) and cfg.price_timeless:
                key, cached = timeless.lookup(cfg.league, it)
                if cached and cached.get("price_chaos") is not None:
                    unit = cached["price_chaos"]
                    status = "priced (buyout)" if cached["mode"] == "buyout" else (
                        "lỗi check giá → 0" if cached["mode"] == "error" else "không có buyout → 0")
                elif cached:                                 # recent error, retried later
                    unit, status = 0.0, "lỗi check giá → 0"
                else:
                    unit, status = 0.0, "pending"          # not checked yet: 0 until the trade check finishes
                    to_queue.append(it)
                agg_key = f"timeless|{key or it.get('id')}"
                if agg_key in agg:  # identical jewel twice
                    agg[agg_key]["qty"] += 1
                    agg[agg_key]["total"] += unit
                    continue
                agg[agg_key] = {"source": source, "key": agg_key, "name": _display_name(it), "category": "timeless",
                                "qty": 1, "unit": unit, "total": unit, "icon": prices.icon_for(it),
                                "extra": {"timeless_key": key, "timeless_status": status,
                                          "mods": mod_lines(it)[:2]}}
                continue
            unit, cat = prices.price(it)
            total = unit * qty
            if unit <= 0:
                continue
            if total < cfg.min_item_value and unit < cfg.min_item_value:
                other_total += total
                other_qty += int(qty)
                continue
            var = _variant(it, cat)
            k = f"{cat}|{_display_name(it)}|{var}"
            row = agg.get(k)
            if row:
                row["qty"] += qty
                row["total"] += total
            else:
                agg[k] = {"source": source, "key": k, "name": _display_name(it) + (f" ({var})" if var else ""),
                          "category": cat, "qty": qty, "unit": unit, "total": total, "icon": prices.icon_for(it)}
        except Exception:  # noqa: BLE001 – one weird item must not kill the snapshot
            bad.append(it)
            logging.getLogger("poewealth").exception(
                "Không định giá được item %s / %s (frameType %s). JSON: %s", it.get("name"), it.get("typeLine"),
                it.get("frameType"), json.dumps({k: v for k, v in it.items() if k not in ("icon",)}, ensure_ascii=False)[:3000])
    if other_total > 0:
        agg["other"] = {"source": source, "key": "other", "name": f"(item rẻ < {cfg.min_item_value}c)",
                        "category": "other", "qty": other_qty, "unit": 0, "total": other_total, "icon": ""}
    if bad:
        agg["unpriced"] = {"source": source, "key": "unpriced", "name": f"({len(bad)} item lỗi định giá – xem log)",
                           "category": "other", "qty": len(bad), "unit": 0, "total": 0, "icon": ""}
    rows = list(agg.values())
    return rows, to_queue


async def take_snapshot(cfg: Config, db: DB, prices: Prices, timeless: TimelessPricer, limiter) -> int:
    if _lock.locked():
        raise RuntimeError("Đang snapshot rồi")
    async with _lock:
        progress.update(running=True, message="Tải giá poe.ninja…", error="")
        base_msg = {"m": ""}

        def on_wait(bucket, secs):
            progress["message"] = f"{base_msg['m']} – chờ rate limit {bucket} {secs:.0f}s"
        limiter.on_wait = on_wait
        client = PoeClient(cfg, limiter)
        try:
            await prices.refresh(cfg.league)
            sources, all_rows, queue = [], [], []
            warrants: list[dict] = []
            inventory: list[dict] = []

            if cfg.tab_ids:
                progress["message"] = "Lấy danh sách tab…"
                tabs = {t["id"]: t for t in await client.stash_tabs()}
                db.save_tab_meta(list(tabs.values()))
                for n, tid in enumerate(cfg.tab_ids, 1):
                    t = tabs.get(tid)
                    if not t:
                        continue
                    progress["message"] = base_msg["m"] = f"Tab {n}/{len(cfg.tab_ids)}: {t['n']}"
                    items = await client.stash_items(t["i"])
                    warrants += mercenary.collect_warrants(items, f"tab:{tid}", t["n"])
                    inventory += [chest.light_item(it, f"tab:{tid}", t["n"], prices) for it in items]
                    rows, q = price_items(items, f"tab:{tid}", cfg, prices, timeless)
                    sources.append({"source": f"tab:{tid}", "label": t["n"], "value": sum(r["total"] for r in rows),
                                    "count": len(items)})
                    all_rows += rows
                    queue += q

            if cfg.include_characters:
                progress["message"] = "Lấy nhân vật…"
                chars = await client.characters()
                for c in chars:
                    if cfg.characters and c["name"] not in cfg.characters:
                        continue
                    progress["message"] = base_msg["m"] = f"Nhân vật {c['name']}"
                    items = await client.character_items(c["name"])
                    warrants += mercenary.collect_warrants(items, f"char:{c['name']}", c["name"])
                    rows, q = price_items(items, f"char:{c['name']}", cfg, prices, timeless)
                    sources.append({"source": f"char:{c['name']}", "label": f"🧍 {c['name']}",
                                    "value": sum(r["total"] for r in rows), "count": len(items)})
                    all_rows += rows
                    queue += q

            sid = db.save_snapshot(cfg.league, prices.divine, sources, all_rows)
            try:
                mercenary.save_stash(WARRANT_FILE, cfg.league, warrants, time.time(), sid)
                chest.save(STASH_FILE, cfg.league, inventory, time.time())
            except Exception:  # noqa: BLE001 – the warrant list must not break a snapshot
                logging.getLogger("poewealth").exception("lưu danh sách warrant lỗi")
            for it in queue:
                timeless.enqueue(cfg.league, it)
            msg = f"Xong snapshot #{sid}"
            if prices.errors:
                msg += f" (poe.ninja lỗi {len(prices.errors)} loại: {prices.errors[0]})"
            progress["message"] = msg
            return sid
        except Exception as e:  # noqa: BLE001
            logging.getLogger("poewealth").exception("snapshot lỗi")
            progress["error"] = f"{progress['message']}: {e}"
            raise
        finally:
            progress["running"] = False
            progress["finished"] = time.time()
            limiter.on_wait = None
            await client.close()


def diff_snapshots(db: DB, a: int, b: int) -> list[dict]:
    """Per-item change from snapshot a to b (aggregated across sources)."""
    def load(sid):
        d = defaultdict(lambda: {"qty": 0.0, "total": 0.0, "name": "", "icon": "", "category": ""})
        for r in db.q("SELECT key,name,category,qty,total,icon FROM snapshot_items WHERE snapshot_id=?", (sid,)):
            x = d[r["key"]]
            x["qty"] += r["qty"]; x["total"] += r["total"]
            x["name"], x["icon"], x["category"] = r["name"], r["icon"], r["category"]
        return d
    A, B = load(a), load(b)
    out = []
    for k in set(A) | set(B):
        x, y = A.get(k), B.get(k)
        ref = y or x
        dq = (y["qty"] if y else 0) - (x["qty"] if x else 0)
        dv = (y["total"] if y else 0) - (x["total"] if x else 0)
        if abs(dv) >= 0.5 or dq:
            out.append({"name": ref["name"], "icon": ref["icon"], "category": ref["category"],
                        "dqty": dq, "dvalue": dv})
    out.sort(key=lambda r: -abs(r["dvalue"]))
    return out
