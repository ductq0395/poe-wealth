"""Currency Exchange flip scanner (beta 1).

Every exchange item on poe.ninja has a price through the CHAOS pair and through the DIVINE pair.
When they differ, buying through one pair and selling through the other is a flip:
  buy item with chaos -> sell item for divine   (when the divine pair values it higher), or the reverse.

poe.ninja data is cached 30 min by Cloudflare and has no published rate limit, so the scanner
1) reads one overview per category, 2) fetches details only for the most traded items,
3) paces requests (~2/s) and refuses to rescan within 25 min.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from fractions import Fraction

import httpx

from .config import CONFIG_DIR, USER_AGENT
from .ratelimit import Limiter

API = "https://poe.ninja/poe1/api/economy/exchange/current"
POECDN = "https://web.poecdn.com"
CACHE_FILE = CONFIG_DIR / "flip_cache.json"
AUTO_FILE = CONFIG_DIR / "flip_auto.json"
MIN_RESCAN = 25 * 60
FLIP_TYPES = ["Currency", "Fragment", "Essence", "Fossil", "Omen", "Thread", "DeliriumOrb", "Oil",
              "Resonator", "Tattoo", "Runegraft", "AllflameEmber", "Astrolabe", "Artifact", "DivinationCard"]
log = logging.getLogger("poewealth.flip")


def nice_ratio(x: float, max_den: int = 50, tol: float = 0.004) -> tuple[int, int]:
    """Smallest integers a/b ≈ x (relative error <= tol, or best with b <= max_den)."""
    best = None
    for b in range(1, max_den + 1):
        a = max(1, round(x * b))
        err = abs(a / b - x) / x
        if best is None or err < best[2] - 1e-12:
            best = (a, b, err)
        if err <= tol:
            return a, b
    f = Fraction(x).limit_denominator(max_den)
    return (f.numerator or 1, f.denominator) if best is None else best[:2]


def _orders(direction: str, chaos_price: float, items_per_div: float, div_chaos: float) -> dict:
    """Integer ratios for the two orders of one cycle, rounded at market (fills quickly)."""
    # leg 1: buy the item; leg 2: sell it
    if direction == "chaos>div":
        # buy: pay chaos for items. ratio chaos : items
        if chaos_price >= 1:
            c, i = nice_ratio(chaos_price)
        else:
            i, c = nice_ratio(1 / chaos_price)
        buy = {"have": "Chaos Orb", "have_n": c, "want": None, "want_n": i}
        # sell: give items for divine. ratio items : div
        if items_per_div >= 1:
            n, d = nice_ratio(items_per_div)
        else:
            d, n = nice_ratio(1 / items_per_div)
        sell = {"have": None, "have_n": n, "want": "Divine Orb", "want_n": d}
        eff_buy = c / i                       # chaos per item
        eff_sell = d * div_chaos / n          # chaos-equivalent per item
    else:
        # buy: pay divine for items. ratio div : items
        if items_per_div >= 1:
            n, d = nice_ratio(items_per_div)
        else:
            d, n = nice_ratio(1 / items_per_div)
        buy = {"have": "Divine Orb", "have_n": d, "want": None, "want_n": n}
        if chaos_price >= 1:
            c, i = nice_ratio(chaos_price)
        else:
            i, c = nice_ratio(1 / chaos_price)
        sell = {"have": None, "have_n": i, "want": "Chaos Orb", "want_n": c}
        eff_buy = d * div_chaos / n
        eff_sell = c / i
    return {"buy": buy, "sell": sell, "eff_buy": eff_buy, "eff_sell": eff_sell,
            "eff_pct": (eff_sell - eff_buy) / eff_buy * 100}


class FlipScanner:
    def __init__(self, limiter: Limiter):
        self.limiter = limiter
        self.state = {"running": False, "message": "", "error": "", "done": 0, "total": 0}
        self.data: dict = {"scanned_at": 0, "league": "", "div_chaos": 0, "items": []}
        try:
            if CACHE_FILE.exists():
                self.data = json.loads(CACHE_FILE.read_text())
        except (OSError, ValueError):
            pass
        # trạng thái tự quét: last_etag/etag_changes lưu file; next_check/message chỉ trong RAM
        self.auto: dict = {"last_etag": None, "etag_changes": [], "next_check": 0, "message": ""}
        try:
            if AUTO_FILE.exists():
                self.auto.update(json.loads(AUTO_FILE.read_text()))
        except (OSError, ValueError):
            pass

    async def _etag(self, client: httpx.AsyncClient, league: str) -> str:
        """ETag của overview Currency – đổi khi poe.ninja có dữ liệu mới (1 request, Cloudflare cache)."""
        r = await self.limiter.request(client, "ninja-flip", "GET", f"{API}/overview",
                                       params={"league": league, "type": "Currency"})
        if r.status_code in (403, 429):
            raise RuntimeError(f"poe.ninja chặn tạm (HTTP {r.status_code}) – thử lại sau ít phút")
        if r.status_code != 200:
            raise RuntimeError(f"poe.ninja HTTP {r.status_code} (overview Currency)")
        return r.headers.get("etag") or hashlib.sha1(r.content).hexdigest()

    async def probe_etag(self, league: str) -> str:
        self.limiter.get("ninja-flip").rules = [(2, 1), (90, 60)]
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
            return await self._etag(c, league)

    def note_etag(self, etag: str) -> None:
        """Ghi lại lúc ETag đổi để biết poe.ninja cập nhật bao lâu một lần."""
        if etag and etag != self.auto.get("last_etag"):
            prev = self.auto.get("last_etag")
            self.auto["last_etag"] = etag
            if prev:
                ch = self.auto.setdefault("etag_changes", [])
                ch.append(time.time())
                del ch[:-50]
                if len(ch) >= 2:
                    log.info("poe.ninja ETag đổi (cách lần trước %.0f phút)", (ch[-1] - ch[-2]) / 60)
                else:
                    log.info("poe.ninja ETag đổi")
            self._save_auto()

    def _save_auto(self) -> None:
        try:
            AUTO_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTO_FILE.write_text(json.dumps({"last_etag": self.auto.get("last_etag"),
                                             "etag_changes": self.auto.get("etag_changes", [])}))
        except OSError:
            pass

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> dict:
        r = await self.limiter.request(client, "ninja-flip", "GET", f"{API}/{path}", params=params)
        if r.status_code in (403, 429):
            raise RuntimeError(f"poe.ninja chặn tạm (HTTP {r.status_code}) – thử lại sau ít phút")
        if r.status_code != 200:
            raise RuntimeError(f"poe.ninja HTTP {r.status_code} ({path} {params.get('type')})")
        return r.json()

    def can_scan(self, league: str) -> float:
        """Seconds until a rescan is useful (0 = now)."""
        if self.data.get("league") != league:
            return 0
        return max(0.0, MIN_RESCAN - (time.time() - self.data.get("scanned_at", 0)))

    async def scan(self, league: str, types: list[str], max_items: int, min_volume: float) -> None:
        self.state.update(running=True, message="Đọc danh sách…", error="", done=0, total=0)
        self.limiter.get("ninja-flip").rules = [(2, 1), (90, 60)]
        try:
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
                etag = await self._etag(c, league)
                self.note_etag(etag)
                cands = []
                for t in types:
                    ov = await self._get(c, "overview", league=league, type=t)
                    meta = {i["id"]: i for i in ov.get("items", [])}
                    for ln in ov.get("lines", []):
                        m = meta.get(ln.get("id"))
                        if not m or ln.get("id") in ("chaos", "divine"):
                            continue
                        vol = float(ln.get("volumePrimaryValue") or 0)
                        if vol >= min_volume:
                            cands.append((vol, t, m))
                cands.sort(key=lambda x: -x[0])
                cands = cands[:max_items]
                # divine price history (chaos per divine) for daily spreads
                dv = await self._get(c, "details", league=league, type="Currency", id="divine-orb")
                div_hist = {}
                div_now = 0.0
                for p in dv.get("pairs", []):
                    if p.get("id") == "chaos":
                        div_now = float(p.get("rate") or 0)
                        div_hist = {h["timestamp"][:10]: float(h["rate"]) for h in p.get("history", []) if h.get("rate")}
                if not div_now:
                    r = (dv.get("core") or {}).get("rates", {}).get("divine")
                    div_now = 1 / r if r else 0
                if not div_now:
                    raise RuntimeError("Không lấy được giá Divine – sai tên league?")

                self.state.update(total=len(cands), message="Đọc chi tiết từng món…")
                items = []
                for n, (vol, t, m) in enumerate(cands, 1):
                    self.state.update(done=n, message=f"{n}/{len(cands)}: {m['name']}")
                    try:
                        d = await self._get(c, "details", league=league, type=t, id=m["detailsId"])
                    except RuntimeError as e:
                        if "chặn" in str(e):
                            raise
                        log.warning("%s", e)
                        continue
                    row = self._analyse(d, t, m, div_now, div_hist)
                    if row:
                        items.append(row)
            self.data = {"scanned_at": time.time(), "league": league, "div_chaos": div_now, "items": items,
                         "etag": etag}
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self.data))
            self.state["message"] = f"Xong: {len(items)} món có đủ 2 cặp chaos + divine"
        except Exception as e:  # noqa: BLE001
            log.exception("flip scan lỗi")
            self.state["error"] = str(e)
        finally:
            self.state["running"] = False

    @staticmethod
    def _analyse(d: dict, t: str, m: dict, div_now: float, div_hist: dict) -> dict | None:
        pairs = {p.get("id"): p for p in d.get("pairs", [])}
        pc, pd = pairs.get("chaos"), pairs.get("divine")
        if not pc or not pd or not pc.get("rate") or not pd.get("rate"):
            return None
        chaos_price = float(pc["rate"])                 # chaos per item
        div_per_item = float(pd["rate"])                # divine per item
        via_div = div_per_item * div_now                # chaos-equivalent through the divine pair
        spread = (via_div - chaos_price) / chaos_price * 100
        direction = "chaos>div" if spread > 0 else "div>chaos"
        # daily spreads (same day in both pair histories)
        hc = {h["timestamp"][:10]: float(h["rate"]) for h in pc.get("history", []) if h.get("rate")}
        hd = {h["timestamp"][:10]: float(h["rate"]) for h in pd.get("history", []) if h.get("rate")}
        days = []
        for day in sorted(set(hc) & set(hd) & set(div_hist)):
            v = hd[day] * div_hist[day]
            days.append({"day": day, "chaos": hc[day], "via_div": v, "spread": (v - hc[day]) / hc[day] * 100})
        same = sum(1 for x in days if (x["spread"] > 0) == (spread > 0) and abs(x["spread"]) >= 3)
        vol_c, vol_d = float(pc.get("volumePrimaryValue") or 0), float(pd.get("volumePrimaryValue") or 0)
        return {
            "id": m["detailsId"], "name": m["name"], "type": t,
            "icon": POECDN + m["image"] if m.get("image") else "",
            "chaos_price": chaos_price, "via_div": via_div, "items_per_div": 1 / div_per_item,
            "spread": spread, "direction": direction,
            "vol_chaos": vol_c, "vol_div": vol_d, "liquidity": min(vol_c, vol_d),
            "days": days[-7:], "stable_days": same, "n_days": len(days[-7:]),
            "orders": _orders(direction, chaos_price, 1 / div_per_item, div_now),
        }


async def auto_step(scanner: FlipScanner, cfg) -> float:
    """
    Một bước của vòng tự quét. Trả số giây nên chờ trước bước sau.
    - dữ liệu mới hơn flip_auto_minutes: không làm gì;
    - cũ hơn: hỏi ETag overview Currency (1 request); khác ETag lần quét trước → quét đầy đủ;
      giống → chờ 5 phút hỏi lại; cũ hơn flip_auto_max_minutes thì quét luôn.
    """
    mins = int(getattr(cfg, "flip_auto_minutes", 0) or 0)
    if mins <= 0 or not cfg.league:
        scanner.auto["message"] = "Tự quét: tắt"
        return 60
    if scanner.state["running"]:
        return 30
    d = scanner.data
    age = time.time() - d.get("scanned_at", 0) if d.get("league") == cfg.league else float("inf")
    if age < mins * 60:
        scanner.auto["next_check"] = time.time() + mins * 60 - age
        scanner.auto["message"] = f"Tự quét: kiểm tra lúc {time.strftime('%H:%M', time.localtime(scanner.auto['next_check']))}"
        return min(mins * 60 - age, 300) + 1
    wait = scanner.can_scan(cfg.league)
    if wait > 0:
        return wait + 1
    max_age = max(mins, int(getattr(cfg, "flip_auto_max_minutes", 60) or 60)) * 60
    try:
        etag = await scanner.probe_etag(cfg.league)
    except Exception as e:  # noqa: BLE001
        scanner.auto["message"] = f"Tự quét: lỗi hỏi poe.ninja ({e}) – thử lại sau 10 phút"
        log.warning("flip auto: %s", e)
        return 600
    scanner.note_etag(etag)
    if etag == d.get("etag") and age < max_age:
        scanner.auto["next_check"] = time.time() + 300
        scanner.auto["message"] = "Tự quét: poe.ninja chưa có dữ liệu mới, hỏi lại sau 5 phút"
        return 300
    why = "poe.ninja có dữ liệu mới" if etag != d.get("etag") else f"dữ liệu đã cũ {age / 60:.0f} phút"
    scanner.auto["message"] = f"Tự quét: đang quét ({why})"
    log.info("flip auto scan: %s", why)
    await scanner.scan(cfg.league, cfg.flip_types, int(cfg.flip_max_items), float(cfg.flip_min_volume))
    if scanner.state.get("error"):
        scanner.auto["message"] = f"Tự quét: lỗi – {scanner.state['error']} (thử lại sau 10 phút)"
        return 600
    scanner.auto["message"] = f"Tự quét: xong lúc {time.strftime('%H:%M')}"
    return 60


async def run_scan(scanner: FlipScanner, league: str, types, max_items, min_volume):
    await scanner.scan(league, types, max_items, min_volume)


def start(scanner: FlipScanner, league: str, types, max_items, min_volume) -> asyncio.Task:
    return asyncio.create_task(run_scan(scanner, league, types, max_items, min_volume))
