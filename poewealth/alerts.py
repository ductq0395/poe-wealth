"""Price-change alerts: unit price of stash items now vs. N hours ago."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time

from .db import DB

SKIP_CATEGORIES = ("other", "timeless")   # timeless jumps 0 -> price while the trade check runs
log = logging.getLogger("poewealth.alerts")


def _items(db: DB, sid: int) -> dict[str, dict]:
    rows = db.q(
        f"""SELECT key, MAX(name) name, MAX(category) category, MAX(icon) icon, SUM(qty) qty, MAX(unit) unit,
                   SUM(total) total, GROUP_CONCAT(DISTINCT source) sources
            FROM snapshot_items WHERE snapshot_id=? AND category NOT IN ({",".join("?" * len(SKIP_CATEGORIES))})
            GROUP BY key""", (sid, *SKIP_CATEGORIES))
    return {r["key"]: r for r in rows}


def compute(db: DB, league: str, hours: float, pct: float, min_total: float) -> dict:
    latest = db.latest_snapshot(league)
    if not latest:
        return {"alerts": [], "base": None, "latest": None}
    base = db.one("SELECT * FROM snapshots WHERE league=? AND ts<=? AND id<>? ORDER BY ts DESC LIMIT 1",
                  (league, latest["ts"] - hours * 3600, latest["id"]))
    exact = base is not None
    if not base:  # not enough history yet: compare with the oldest snapshot that still has item detail
        base = db.one("""SELECT s.* FROM snapshots s WHERE s.league=? AND s.id<>?
                         AND EXISTS (SELECT 1 FROM snapshot_items i WHERE i.snapshot_id=s.id)
                         ORDER BY s.ts ASC LIMIT 1""", (league, latest["id"]))
    if not base:
        return {"alerts": [], "base": None, "latest": latest}
    old, new = _items(db, base["id"]), _items(db, latest["id"])
    labels = {r["source"]: r["label"] for r in db.q("SELECT source, label FROM snapshot_sources WHERE snapshot_id=?",
                                                     (latest["id"],))}
    out = []
    for key, n in new.items():
        o = old.get(key)
        if not o or o["unit"] <= 0 or n["unit"] <= 0:
            continue
        change = (n["unit"] - o["unit"]) / o["unit"] * 100
        if abs(change) < pct:
            continue
        # value at stake: what you hold now, at the bigger of the two prices
        stake = n["qty"] * max(n["unit"], o["unit"])
        if stake < min_total:
            continue
        out.append({
            "key": key, "name": n["name"], "category": n["category"], "icon": n["icon"],
            "qty": n["qty"], "old_unit": o["unit"], "new_unit": n["unit"], "pct": change,
            "delta_total": n["qty"] * (n["unit"] - o["unit"]), "total": n["total"],
            "sources": [labels.get(s, s) for s in (n["sources"] or "").split(",") if s],
        })
    out.sort(key=lambda a: -abs(a["delta_total"]))
    return {"alerts": out, "base": base, "latest": latest, "exact": exact}


class Notifier:
    """Desktop notification (notify-send) for alerts that are new since the last notification."""

    def __init__(self):
        self.sent: dict[str, tuple[int, float]] = {}  # key -> (direction, time)

    async def notify(self, alerts: list[dict], fmt) -> None:
        now = time.time()
        fresh = []
        for a in alerts:
            d = 1 if a["pct"] > 0 else -1
            prev = self.sent.get(a["key"])
            if prev and prev[0] == d and now - prev[1] < 12 * 3600:
                continue
            self.sent[a["key"]] = (d, now)
            fresh.append(a)
        if not fresh or not shutil.which("notify-send"):
            return
        lines = [f"{'▲' if a['pct'] > 0 else '▼'} {a['name']}: {fmt(a['old_unit'])} → {fmt(a['new_unit'])} "
                 f"({a['pct']:+.0f}%)" for a in fresh[:6]]
        if len(fresh) > 6:
            lines.append(f"… và {len(fresh) - 6} món khác")
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "PoE Wealth", "-i", "dialog-information",
                f"PoE Wealth: {len(fresh)} món đổi giá mạnh", "\n".join(lines))
            await proc.wait()
        except OSError as e:
            log.warning("notify-send lỗi: %s", e)
