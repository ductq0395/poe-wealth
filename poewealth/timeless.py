"""Price timeless jewels by seed + conqueror through the official trade API.

poe.ninja (and Wealthy Exile) only price timeless jewels as a generic unique.
Real value depends on the seed, so we search the trade site per jewel.
The trade search limit is tight (~5 req / 10 s, 15 / 60 s, 30 / 300 s),
so this runs as a slow background queue with a 6 h cache in SQLite.
"""
from __future__ import annotations

import asyncio
import json
import re
import math
import time

from .db import DB
from .poeapi import PoeClient, PoeError
from .pricing import Prices

JEWEL_CONQUERORS = {
    "Glorious Vanity": ["Xibaqua", "Zerphi", "Ahuana", "Doryani"],
    "Lethal Pride": ["Kaom", "Rakiata", "Kiloava", "Akoya"],
    "Brutal Restraint": ["Asenath", "Nasima", "Balbala", "Deshret"],
    "Militant Faith": ["Avarius", "Dominus", "Maxarius", "Venarius"],
    "Elegant Hubris": ["Cadiro", "Victario", "Chitus", "Caspiro"],
    "Heroic Tragedy": ["Vorana", "Uhtred", "Medved", "Olroth"],
}

TRADE_CURRENCY = {
    "chaos": "Chaos Orb", "divine": "Divine Orb", "exalted": "Exalted Orb", "alch": "Orb of Alchemy",
    "fusing": "Orb of Fusing", "chrome": "Chromatic Orb", "vaal": "Vaal Orb", "regal": "Regal Orb",
    "gcp": "Gemcutter's Prism", "annul": "Orb of Annulment", "mirror": "Mirror of Kalandra",
}

CACHE_SECONDS = 6 * 3600
ERROR_RETRY_SECONDS = 600
_NUM = re.compile(r"\d+")


def mod_lines(item: dict, key: str = "explicitMods") -> list[str]:
    """Mods come as plain strings or (newer API) as {"description": "line1\nline2"} objects."""
    out = []
    for m in item.get(key, []) or []:
        if isinstance(m, dict):
            m = m.get("description") or m.get("text") or ""
        if isinstance(m, str):
            out += [ln.strip() for ln in m.split("\n") if ln.strip()]
    return out


def parse_jewel(item: dict) -> tuple[str, str, int] | None:
    jewel = item.get("name", "")
    names = JEWEL_CONQUERORS.get(jewel)
    if not names:
        return None
    for mod in mod_lines(item):
        clean = re.sub(r"\[[^\]|]*\|([^\]]*)\]", r"\1", mod)  # strip [tag|text] markup if any
        m = _NUM.search(clean)
        conq = next((n for n in names if clean.rstrip(". ").endswith(n)), None)
        if m and conq:
            return jewel, conq, int(m.group())
    return None


PRICE_RULE = "b5"  # instant buyout, floor(mean of 5 cheapest); old cache entries (other rule) are ignored


def cache_key(league: str, jewel: str, conq: str, seed: int) -> str:
    return f"{PRICE_RULE}|{league}|{jewel}|{conq}|{seed}"


class TimelessPricer:
    def __init__(self, db: DB, prices: Prices):
        self.db = db
        self.prices = prices
        self.queue: asyncio.Queue[tuple[str, str, str, int]] = asyncio.Queue()
        self.pending: set[str] = set()
        self.stat_ids: dict[str, str] = {}
        self.status = "idle"
        self.task: asyncio.Task | None = None
        self.client_factory = None  # set by app: () -> PoeClient

    # ---- public ----
    def lookup(self, league: str, item: dict) -> tuple[str | None, dict | None]:
        p = parse_jewel(item)
        if not p:
            return None, None
        key = cache_key(league, *p)
        return key, self._cached(key)

    def _cached(self, key: str) -> dict | None:
        c = self.db.timeless_get(key, CACHE_SECONDS)
        if c and c["mode"] == "error" and time.time() - c["fetched"] > ERROR_RETRY_SECONDS:
            return None                       # errors (rate limit, network) are retried after 10 minutes
        return c

    def enqueue(self, league: str, item: dict) -> None:
        p = parse_jewel(item)
        if not p:
            return
        key = cache_key(league, *p)
        if key in self.pending or self._cached(key):
            return
        self.pending.add(key)
        self.queue.put_nowait((key, *p))
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

    def enqueue_seed(self, league: str, jewel: str, conq: str, seed: int) -> str:
        """Queue a price check for a seed that is not in the stash (timeless finder)."""
        key = cache_key(league, jewel, conq, seed)
        if key not in self.pending and not self._cached(key):
            self.pending.add(key)
            self.queue.put_nowait((key, jewel, conq, seed))
            if not self.task or self.task.done():
                self.task = asyncio.create_task(self._run())
        return key

    def cached(self, key: str) -> dict | None:
        return self._cached(key)

    def state(self) -> dict:
        return {"status": self.status, "pending": len(self.pending)}

    # ---- worker ----
    async def _resolve_stats(self, client: PoeClient) -> None:
        if self.stat_ids:
            return
        try:
            data = await client.trade_stats()
            for group in data.get("result", []):
                for e in group.get("entries", []):
                    sid = e.get("id", "")
                    if "pseudo_timeless_jewel_" in sid and sid.startswith("explicit."):
                        self.stat_ids[sid.rsplit("_", 1)[-1].lower()] = sid
        except PoeError:
            pass

    def _sid(self, conq: str) -> str:
        return self.stat_ids.get(conq.lower(), f"explicit.pseudo_timeless_jewel_{conq.lower()}")

    def _query(self, jewel: str, conquerors: list[str], seed: int) -> dict:
        return {
            "query": {
                "status": {"option": "securable"},   # = "Instant Buyout" on the trade site
                "name": jewel,
                "type": "Timeless Jewel",
                "stats": [{"type": "count", "value": {"min": 1},
                           "filters": [{"id": self._sid(c), "value": {"min": seed, "max": seed}}
                                       for c in conquerors]}],
                "filters": {"trade_filters": {"filters": {"sale_type": {"option": "priced"}}}},
            },
            "sort": {"price": "asc"},
        }

    def _to_chaos(self, price: dict | None) -> float | None:
        if not price or price.get("amount") is None:
            return None
        name = TRADE_CURRENCY.get(price.get("currency", ""))
        rate = self.prices.stack.get(name) if name else None
        return float(price["amount"]) * rate if rate else None

    async def _price_one(self, client: PoeClient, jewel: str, conq: str, seed: int):
        """Instant-buyout listings for this exact seed + conqueror.
        Price = floor(average of the 5 cheapest, in chaos); no listing -> 0."""
        conqs = JEWEL_CONQUERORS[jewel] if conq == "*" else [conq]
        res = await client.trade_search(self._query(jewel, conqs, seed))
        ids = res.get("result", [])
        if not ids:
            return 0.0, 0, "none", "không có ai bán instant buyout"
        listings = await client.trade_fetch(ids[:10], res["id"])
        vals = sorted(v for v in (self._to_chaos(l.get("listing", {}).get("price")) for l in listings) if v)
        if not vals:
            return 0.0, int(res.get("total", len(ids))), "none", "listing có giá lạ, không đổi được ra chaos"
        top = vals[:5]
        price = float(math.floor(sum(top) / len(top)))
        return price, int(res.get("total", len(ids))), "buyout", f"TB {len(top)} giá rẻ nhất: " + ", ".join(f"{v:.0f}" for v in top)

    async def _run(self) -> None:
        client: PoeClient = self.client_factory()
        try:
            await self._resolve_stats(client)
            while not self.queue.empty():
                key, jewel, conq, seed = await self.queue.get()
                self.status = f"đang check {jewel} {seed} ({conq})"
                try:
                    price, n, mode, detail = await self._price_one(client, jewel, conq, seed)
                    self.db.timeless_put(key, price, n, mode, detail)
                    self.apply_to_latest(key, price, mode)
                except PoeError as e:
                    self.db.timeless_put(key, None, 0, "error", str(e)[:200])
                finally:
                    self.pending.discard(key)
        finally:
            self.status = "idle"
            await client.close()

    def apply_to_latest(self, key: str, price: float, mode: str = "buyout") -> None:
        """Re-value the newest snapshot once a timeless price arrives."""
        conn = self.db.conn
        rows = self.db.q(
            "SELECT rowid, snapshot_id, qty, extra FROM snapshot_items WHERE snapshot_id="
            "(SELECT MAX(id) FROM snapshots) AND category='timeless'")
        hit = [r for r in rows if json.loads(r["extra"] or "{}").get("timeless_key") == key]
        if not hit:
            return
        sid = hit[0]["snapshot_id"]
        with conn:
            for r in hit:
                extra = json.loads(r["extra"] or "{}")
                extra["timeless_status"] = "priced (buyout)" if mode == "buyout" else "không có buyout → 0"
                conn.execute("UPDATE snapshot_items SET unit=?, total=?, extra=? WHERE rowid=?",
                             (price, price * r["qty"], json.dumps(extra), r["rowid"]))
        recompute_snapshot(self.db, sid)


def recompute_snapshot(db: DB, sid: int) -> None:
    """Totals from items. Items below min value are stored as one '(other)' row per source, so sums stay exact."""
    with db.conn:
        for s in db.q("SELECT source FROM snapshot_sources WHERE snapshot_id=?", (sid,)):
            v = db.one("SELECT COALESCE(SUM(total),0) v FROM snapshot_items WHERE snapshot_id=? AND source=?",
                       (sid, s["source"]))["v"]
            db.conn.execute("UPDATE snapshot_sources SET value=? WHERE snapshot_id=? AND source=?",
                            (v, sid, s["source"]))
        tot = db.one("SELECT COALESCE(SUM(value),0) v FROM snapshot_sources WHERE snapshot_id=?", (sid,))["v"]
        db.conn.execute("UPDATE snapshots SET total_chaos=? WHERE id=?", (tot, sid))
