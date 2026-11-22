"""Gem profit ideas from poe.ninja SkillGem lines (already loaded by Prices).

poe.ninja line fields used (checked 22/11/2026): name, icon, gemLevel, gemQuality (missing = 0), corrupted,
chaosValue, listingCount, metadata.gemKind == "transfigured" for transfigured gems ("Arc of Oscillating").

Ideas:
- level:      buy L1 (q0 / q20), level it, sell at max level, same quality, not corrupted
- quality:    buy L20 q0, add 20 Gemcutter's Prism, sell L20/20
- vaal:       buy 20/20 (or 1/20), Vaal Orb it, expected value of the corrupted outcomes (estimate)
- transfigure: lab Divine Font turns a gem into one of its transfigured versions (same level/quality)
"""
from __future__ import annotations

from collections import defaultdict

from .pricing import Prices

# Vaal Orb on a gem (approximation, 3.2x rules): 25% nothing, 25% vaal version (or nothing),
# 25% level +/-1, 25% quality +/- (we count "+" as reaching 23%).
VAAL_SPLIT = {"same": 0.5, "lvl_up": 0.125, "lvl_down": 0.125, "q_up": 0.125, "q_down": 0.125}


def _key(ln: dict) -> tuple[int, int, bool]:
    return int(ln.get("gemLevel") or 1), int(ln.get("gemQuality") or 0), bool(ln.get("corrupted"))


def _vstr(lvl: int, q: int, c: bool) -> str:
    return f"{lvl}" + (f"/{q}" if q else "") + ("c" if c else "")


class GemBook:
    def __init__(self, prices: Prices):
        self.by: dict[str, dict[tuple, dict]] = {}
        self.icon: dict[str, str] = {}
        self.transfig: dict[str, bool] = {}
        for name, lines in prices.gems.items():
            d: dict[tuple, dict] = {}
            for ln in lines:
                k = _key(ln)
                # several generations / duplicates: keep the most listed one
                if k not in d or int(ln.get("listingCount") or 0) > int(d[k].get("listingCount") or 0):
                    d[k] = ln
                self.icon.setdefault(name, ln.get("icon") or "")
                if (ln.get("metadata") or {}).get("gemKind") == "transfigured":
                    self.transfig[name] = True
            self.by[name] = d

    def get(self, name: str, lvl: int, q: int, c: bool) -> tuple[float, int] | None:
        ln = self.by.get(name, {}).get((lvl, q, c))
        if not ln:
            return None
        return float(ln.get("chaosValue") or 0), int(ln.get("listingCount") or 0)

    def max_level(self, name: str) -> int:
        lv = [k[0] for k in self.by.get(name, {}) if not k[2]]
        return max(lv, default=0)

    def base_of(self, name: str) -> str | None:
        """'Arc of Oscillating' -> 'Arc' (only for transfigured gems whose base is known)."""
        if not self.transfig.get(name) or " of " not in name:
            return None
        cand = name.rsplit(" of ", 1)[0]
        return cand if cand in self.by and not self.transfig.get(cand) else None


def _row(kind, name, icon, buy, sell, cost=0.0, note="", target=None):
    (bp, blc), (sp, slc) = buy[1], sell[1]
    profit = sp - bp - cost
    return {"kind": kind, "name": name, "target": target or name, "icon": icon,
            "buy": buy[0], "buy_price": bp, "buy_lc": blc, "sell": sell[0], "sell_price": sp, "sell_lc": slc,
            "cost": cost, "profit": profit, "roi": profit / (bp + cost) if bp + cost > 0 else 0.0,
            "liquidity": min(blc, slc), "note": note}


def analyse(prices: Prices) -> dict:
    book = GemBook(prices)
    gcp = prices.stack.get("Gemcutter's Prism", 0.0)
    vaal = prices.stack.get("Vaal Orb", 0.0)
    rows = []
    for name in book.by:
        icon = book.icon.get(name, "")
        mx = book.max_level(name)
        # ---- level up ----
        if mx > 1:
            for q in (0, 20):
                a, b = book.get(name, 1, q, False), book.get(name, mx, q, False)
                if a and b:
                    rows.append(_row("level", name, icon, (_vstr(1, q, False), a), (_vstr(mx, q, False), b),
                                     note=f"lên level 1 → {mx}"))
        # ---- quality ----
        for lvl in {1, mx} - {0}:
            a, b = book.get(name, lvl, 0, False), book.get(name, lvl, 20, False)
            if a and b and gcp:
                rows.append(_row("quality", name, icon, (_vstr(lvl, 0, False), a), (_vstr(lvl, 20, False), b),
                                 cost=20 * gcp, note=f"20 Gemcutter's Prism ≈ {20 * gcp:.0f}c"))
        # ---- vaal ----
        for lvl in {1, mx} - {0}:
            base = book.get(name, lvl, 20, False)
            same = book.get(name, lvl, 20, True)
            if not base or not same or not vaal:
                continue
            up = book.get(name, lvl + 1, 20, True)
            qup = book.get(name, lvl, 23, True)
            down = book.get(name, lvl - 1, 20, True) if lvl > 1 else None
            ev = (VAAL_SPLIT["same"] * same[0] + VAAL_SPLIT["q_down"] * same[0]
                  + VAAL_SPLIT["lvl_up"] * (up or same)[0] + VAAL_SPLIT["q_up"] * (qup or same)[0]
                  + VAAL_SPLIT["lvl_down"] * (down[0] if down else same[0] * 0.5))
            lc = min(same[1], (up or same)[1], (qup or same)[1])
            parts = [f"{_vstr(lvl, 20, True)} {same[0]:.0f}c"] + \
                    ([f"{_vstr(lvl + 1, 20, True)} {up[0]:.0f}c"] if up else []) + \
                    ([f"{_vstr(lvl, 23, True)} {qup[0]:.0f}c"] if qup else [])
            rows.append(_row("vaal", name, icon, (_vstr(lvl, 20, False), base), ("EV corrupt", (ev, lc)),
                             cost=vaal, note="ước tính: " + ", ".join(parts)))
        # ---- transfigure (lab Divine Font) ----
        base_name = book.base_of(name)
        if base_name:
            for lvl, q in ((1, 0), (1, 20), (20, 0), (20, 20)):
                a, b = book.get(base_name, lvl, q, False), book.get(name, lvl, q, False)
                if a and b:
                    rows.append(_row("transfigure", base_name, book.icon.get(base_name, icon),
                                     (_vstr(lvl, q, False), a), (_vstr(lvl, q, False), b),
                                     note="cần 1 lần chạy Lab (Divine Font)", target=name))
    rows.sort(key=lambda r: -r["profit"])
    return {"rows": rows, "gcp": gcp, "vaal": vaal, "divine": prices.divine}
