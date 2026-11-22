"""Local web app. Binds to 127.0.0.1 only."""
from __future__ import annotations

import asyncio
import logging
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import alerts, flip
from . import config as cfgmod
from .db import DB
from .poeapi import PoeClient, PoeError
from .pricing import Prices
from .ratelimit import Limiter, RateLimited
from .snapshot import diff_snapshots, progress, take_snapshot
from .timeless import TimelessPricer
from .mercenary import MercPricer

HOST = "127.0.0.1"
PORT = int(os.environ.get("POE_WEALTH_PORT", "8787"))
STATIC = Path(__file__).parent / "static"

state: dict = {}


def cfg() -> cfgmod.Config:
    return state["cfg"]


def new_client() -> PoeClient:
    return PoeClient(cfg(), state["limiter"])


async def _snapshot_then(after=None, only=None):
    try:
        sid = await take_snapshot(cfg(), state["db"], state["prices"], state["timeless"], state["limiter"], only)
        if after:
            after(sid)
    except Exception:  # noqa: BLE001 - error is in progress["error"]
        return
    c = cfg()
    if c.alert_notify:
        try:
            res = alerts.compute(state["db"], c.league, c.alert_hours, c.alert_pct, c.alert_min_total)
            div = state["prices"].divine
            await state["notifier"].notify(res["alerts"], lambda v: f"{v / div:.2f}d" if div and v >= div else f"{v:.0f}c")
        except Exception:  # noqa: BLE001
            logging.getLogger("poewealth").exception("alert lỗi")


def run_bg(coro) -> None:
    t = asyncio.create_task(coro)
    state.setdefault("tasks", set()).add(t)
    t.add_done_callback(state["tasks"].discard)


def _my_chars() -> set[str]:
    db = state["db"]
    s = db.latest_snapshot(cfg().league) if cfg().league else None
    names = set(cfg().characters or [])
    if s:
        names |= {r["source"][5:] for r in db.q("SELECT source FROM snapshot_sources WHERE snapshot_id=? AND source LIKE 'char:%'", (s["id"],))}
    return names


async def scan_log() -> dict:
    from .gamelog import find_log
    lr = state["log"]
    path = find_log(cfg().client_log_path, cfg().patch_game_path)
    if not path:
        lr.status = "Không tìm thấy Client.txt – nhập đường dẫn trong Cài đặt"
        return {"found": False}
    try:
        st = await asyncio.to_thread(lr.scan, path, _my_chars())
        lr.status = ""
        return {"found": True, **st}
    except Exception as e:  # noqa: BLE001
        logging.getLogger("poewealth").exception("đọc Client.txt lỗi")
        lr.status = f"Lỗi đọc log: {e}"
        return {"found": True, "error": str(e)}


async def log_loop():
    while True:
        await scan_log()
        await asyncio.sleep(20)


async def auto_loop():
    next_try = 0.0  # after a failed auto snapshot, back off instead of retrying every 30 s
    while True:
        await asyncio.sleep(30)
        mins = cfg().auto_snapshot_minutes
        if mins <= 0 or progress["running"] or not cfg().league or time.time() < next_try:
            continue
        last = state["db"].latest_snapshot(cfg().league)
        if not last or time.time() - last["ts"] >= mins * 60:
            await _snapshot_then()
            if progress.get("error"):
                blocked = max((b["blocked_for"] for b in state["limiter"].state().values()), default=0)
                next_try = time.time() + max(blocked + 30, 300)
                logging.getLogger("poewealth").warning(
                    "Auto snapshot lỗi, thử lại sau %.0f phút", (next_try - time.time()) / 60)


def setup_logging() -> Path:
    from logging.handlers import RotatingFileHandler
    cfgmod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = cfgmod.CONFIG_DIR / "poe-wealth.log"
    h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("poewealth")
    root.setLevel(logging.DEBUG if os.environ.get("POE_WEALTH_DEBUG") else logging.INFO)
    root.addHandler(h)
    if os.environ.get("POE_WEALTH_DEBUG"):
        root.addHandler(logging.StreamHandler())
    return path


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["cfg"] = cfgmod.load()
    state["db"] = DB(cfgmod.DB_FILE)
    state["limiter"] = Limiter()
    state["prices"] = Prices(state["limiter"])
    tp = TimelessPricer(state["db"], state["prices"])
    tp.client_factory = new_client
    state["notifier"] = alerts.Notifier()
    state["flip"] = flip.FlipScanner(state["limiter"])
    state["timeless"] = tp
    from .gamelog import LogReader
    state["log"] = LogReader(state["db"])
    mp = MercPricer(state["prices"])
    mp.client_factory = new_client
    state["merc"] = mp
    loop_task = asyncio.create_task(auto_loop())
    log_task = asyncio.create_task(log_loop())
    yield
    loop_task.cancel()
    log_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        return JSONResponse({"detail": "bad host"}, status_code=403)   # DNS-rebinding guard
    if request.method != "GET" and request.headers.get("x-local-app") != "1":
        return JSONResponse({"detail": "missing header"}, status_code=403)  # CSRF guard
    return await call_next(request)


@app.exception_handler(RateLimited)
@app.exception_handler(PoeError)
async def poe_err(_, e: Exception):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ---------------- config ----------------
@app.get("/api/config")
async def get_config():
    return cfg().public()


@app.post("/api/config")
async def set_config(body: dict):
    c = cfg()
    sess = body.pop("poesessid", None)
    if body.pop("clear_poesessid", False):
        c.poesessid = ""
    elif isinstance(sess, str) and sess.strip():
        c.poesessid = sess.strip()
    for k, v in body.items():
        if hasattr(c, k) and k != "poesessid":
            setattr(c, k, v)
    cfgmod.save(c)
    return c.public()


@app.get("/api/leagues")
async def leagues():
    c = new_client()
    try:
        return await c.leagues()
    finally:
        await c.close()


@app.get("/api/tabs")
async def tabs():
    c = new_client()
    try:
        tabs = await c.stash_tabs()
        state["db"].save_tab_meta(tabs)
        return [{"id": t["id"], "i": t["i"], "n": t["n"], "type": t.get("type"),
                 "colour": t.get("colour")} for t in tabs]
    finally:
        await c.close()


@app.get("/api/characters")
async def characters():
    c = new_client()
    try:
        return [{"name": x["name"], "class": x.get("class"), "level": x.get("level")} for x in await c.characters()]
    finally:
        await c.close()


# ---------------- snapshots ----------------
@app.post("/api/snapshot")
async def snapshot():
    if progress["running"]:
        raise HTTPException(409, "Đang chạy snapshot")
    run_bg(_snapshot_then())
    return {"ok": True}


@app.post("/api/snapshot/source")
async def snapshot_source(body: dict):
    """Re-read one stash tab ("tab:<id>") or character ("char:<name>"); other sources are copied."""
    src = str(body.get("source") or "")
    if not (src.startswith("tab:") or src.startswith("char:")):
        raise HTTPException(400, "source phải là tab:<id> hoặc char:<tên>")
    if progress["running"]:
        raise HTTPException(409, "Đang chạy snapshot")
    if not state["db"].latest_snapshot(cfg().league):
        raise HTTPException(400, "Chưa có snapshot nào – bấm Snapshot đầy đủ trước")
    run_bg(_snapshot_then(only=src))
    return {"ok": True}


@app.get("/api/status")
async def status():
    return {"progress": progress, "timeless": state["timeless"].state(), "code_dir": str(Path(__file__).parent),
            "prices_age": time.time() - state["prices"].loaded_at if state["prices"].loaded_at else None,
            "divine": state["prices"].divine}


@app.get("/api/debug")
async def debug():
    """Rate-limit buckets, poe.ninja errors, progress – for troubleshooting."""
    return {"progress": progress, "limiter": state["limiter"].state(),
            "ninja_errors": state["prices"].errors, "prices_league": state["prices"].league,
            "code_dir": str(Path(__file__).parent), "log": str(cfgmod.CONFIG_DIR / "poe-wealth.log")}


@app.get("/api/history")
async def history(limit: int = 500):
    rows = state["db"].q(
        "SELECT id, ts, total_chaos, divine_rate FROM snapshots WHERE league=? ORDER BY id DESC LIMIT ?",
        (cfg().league, limit))
    return list(reversed(rows))


@app.get("/api/sources/history")
async def sources_history(limit: int = 500):
    """Value of every source (tab / character) per snapshot, aligned to the snapshot list."""
    db = state["db"]
    snaps = list(reversed(db.q(
        "SELECT id, ts, divine_rate FROM snapshots WHERE league=? ORDER BY id DESC LIMIT ?",
        (cfg().league, limit))))
    if not snaps:
        return {"snapshots": [], "series": {}, "meta": {}}
    idx = {s["id"]: n for n, s in enumerate(snaps)}
    rows = db.q(
        f"SELECT snapshot_id, source, label, value, item_count FROM snapshot_sources "
        f"WHERE snapshot_id IN ({','.join('?' * len(idx))}) ORDER BY snapshot_id", tuple(idx))
    series: dict[str, dict] = {}
    for r in rows:
        s = series.setdefault(r["source"], {"label": r["label"], "values": [None] * len(snaps),
                                            "counts": [None] * len(snaps)})
        n = idx[r["snapshot_id"]]
        s["values"][n] = r["value"]
        s["counts"][n] = r["item_count"]
        s["label"] = r["label"]  # newest label wins (tab rename)
    meta = {m["source"]: m for m in db.q("SELECT * FROM tab_meta")}
    return {"snapshots": snaps, "series": series, "meta": meta}


def _snapshot_detail(sid: int) -> dict:
    db = state["db"]
    snap = db.one("SELECT * FROM snapshots WHERE id=?", (sid,))
    if not snap:
        raise HTTPException(404, "Không có snapshot")
    sources = db.q("SELECT source,label,value,item_count FROM snapshot_sources WHERE snapshot_id=? ORDER BY value DESC", (sid,))
    items = db.q("SELECT source,key,name,category,qty,unit,total,icon,extra FROM snapshot_items WHERE snapshot_id=? ORDER BY total DESC", (sid,))
    for i in items:
        i["extra"] = json.loads(i["extra"] or "{}")
    return {"snapshot": snap, "sources": sources, "items": items}


@app.get("/api/snapshot/latest")
async def latest():
    s = state["db"].latest_snapshot(cfg().league)
    if not s:
        return {"snapshot": None, "sources": [], "items": []}
    return _snapshot_detail(s["id"])


@app.get("/api/snapshot/{sid}")
async def snapshot_detail(sid: int):
    return _snapshot_detail(sid)


@app.delete("/api/snapshot/{sid}")
async def delete_snapshot(sid: int):
    db = state["db"]
    if db.one("SELECT id FROM sessions WHERE start_snapshot=? OR end_snapshot=?", (sid, sid)):
        raise HTTPException(400, "Snapshot đang thuộc 1 phiên farm")
    with db.conn:
        for t in ("snapshot_items", "snapshot_sources"):
            db.conn.execute(f"DELETE FROM {t} WHERE snapshot_id=?", (sid,))
        db.conn.execute("DELETE FROM snapshots WHERE id=?", (sid,))
    return {"ok": True}


# ---------------- farm sessions ----------------
@app.post("/api/session/start")
async def session_start(body: dict | None = None):
    db = state["db"]
    if db.one("SELECT id FROM sessions WHERE end_snapshot IS NULL"):
        raise HTTPException(400, "Đang có phiên chưa dừng")
    if progress["running"]:
        raise HTTPException(409, "Đang chạy snapshot")
    name = (body or {}).get("name") or time.strftime("Phiên %d/%m %H:%M")
    tag = str((body or {}).get("tag") or "").strip()[:40]

    def after(sid):
        with db.conn:
            db.conn.execute("INSERT INTO sessions(name,start_snapshot,started,tag) VALUES (?,?,?,?)", (name, sid, time.time(), tag))
    run_bg(_snapshot_then(after))
    return {"ok": True}


@app.post("/api/session/stop")
async def session_stop():
    db = state["db"]
    s = db.one("SELECT id FROM sessions WHERE end_snapshot IS NULL")
    if not s:
        raise HTTPException(400, "Không có phiên đang chạy")
    if progress["running"]:
        raise HTTPException(409, "Đang chạy snapshot")

    def after(sid):
        with db.conn:
            db.conn.execute("UPDATE sessions SET end_snapshot=?, ended=? WHERE id=?", (sid, time.time(), s["id"]))
    run_bg(_snapshot_then(after))
    return {"ok": True}


@app.get("/api/sessions")
async def sessions():
    rows = state["db"].q(
        """SELECT s.*, a.total_chaos start_value, b.total_chaos end_value, b.divine_rate
           FROM sessions s JOIN snapshots a ON a.id=s.start_snapshot
           LEFT JOIN snapshots b ON b.id=s.end_snapshot ORDER BY s.id DESC""")
    for r in rows:
        end = r["ended"] or time.time()
        r["maps"] = state["db"].one("SELECT COUNT(*) n FROM map_runs WHERE started>=? AND started<=?", (r["started"], end))["n"]
        r["costs"] = json.loads(r.get("costs") or "[]")
        r["cost"] = sum(float(c.get("qty") or 0) * float(c.get("unit") or 0) for c in r["costs"])
        if r["end_value"] is not None:
            hours = max((r["ended"] - r["started"]) / 3600, 1e-6)
            r["gross"] = r["end_value"] - r["start_value"]
            r["profit"] = r["gross"] - r["cost"]
            r["per_hour"] = r["profit"] / hours
            r["hours"] = hours
            r["per_map"] = r["profit"] / r["maps"] if r["maps"] else None
    return rows


@app.post("/api/session/{sid}/tag")
async def session_tag(sid: int, body: dict):
    with state["db"].conn:
        state["db"].conn.execute("UPDATE sessions SET tag=? WHERE id=?", (str(body.get("tag") or "").strip()[:40], sid))
    return {"ok": True}


@app.post("/api/session/{sid}/costs")
async def session_costs(sid: int, body: dict):
    """body: {items:[{name, qty, unit?}]} – unit (chaos) defaults to today's poe.ninja price."""
    p = state["prices"]
    if cfg().league:
        await p.refresh(cfg().league)
    items = []
    for c in body.get("items", []):
        name, qty = str(c.get("name") or "").strip(), float(c.get("qty") or 0)
        if not name or qty <= 0:
            continue
        unit = c.get("unit")
        unit = float(unit) if unit not in (None, "") else p.stack.get(name, 0.0)
        items.append({"name": name, "qty": qty, "unit": unit})
    with state["db"].conn:
        state["db"].conn.execute("UPDATE sessions SET costs=? WHERE id=?", (json.dumps(items), sid))
    return {"items": items}


@app.get("/api/prices/names")
async def price_names():
    """Names + chaos price of everything poe.ninja prices as a stack (cost picker)."""
    p = state["prices"]
    if cfg().league:
        await p.refresh(cfg().league)
    return sorted(([n, round(v, 2)] for n, v in p.stack.items() if v > 0), key=lambda x: x[0])


@app.get("/api/session/{sid}/diff")
async def session_diff(sid: int):
    s = state["db"].one("SELECT * FROM sessions WHERE id=?", (sid,))
    if not s or not s["end_snapshot"]:
        raise HTTPException(404, "Phiên chưa kết thúc")
    return diff_snapshots(state["db"], s["start_snapshot"], s["end_snapshot"])


@app.get("/api/diff")
async def diff(a: int, b: int):
    return diff_snapshots(state["db"], a, b)


@app.delete("/api/session/{sid}")
async def delete_session(sid: int):
    with state["db"].conn:
        state["db"].conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    return {"ok": True}


# ---------------- timeless ----------------
@app.post("/api/timeless/recheck")
async def timeless_recheck():
    """Drop cached timeless prices for this league and re-run a snapshot."""
    with state["db"].conn:
        state["db"].conn.execute("DELETE FROM timeless_prices WHERE key LIKE ?", (cfg().league + "|%",))
    if not progress["running"]:
        run_bg(_snapshot_then())
    return {"ok": True}


# ---------------- price alerts ----------------
@app.get("/api/alerts")
async def get_alerts(hours: float | None = None, pct: float | None = None, min_total: float | None = None):
    c = cfg()
    res = alerts.compute(state["db"], c.league, hours or c.alert_hours, pct if pct is not None else c.alert_pct,
                         min_total if min_total is not None else c.alert_min_total)
    import shutil
    res["notify_available"] = bool(shutil.which("notify-send"))
    return res


# ---------------- currency exchange flip (beta) ----------------
@app.get("/api/flip")
async def flip_get():
    f = state["flip"]
    return {"state": f.state, "data": f.data, "wait": f.can_scan(cfg().league), "types": flip.FLIP_TYPES}


@app.post("/api/flip/scan")
async def flip_scan():
    f, c = state["flip"], cfg()
    if not c.league:
        raise HTTPException(400, "Chưa chọn league trong Cài đặt")
    if f.state["running"]:
        raise HTTPException(409, "Đang quét")
    wait = f.can_scan(c.league)
    if wait > 0:
        raise HTTPException(429, f"poe.ninja chỉ làm mới mỗi 30 phút – chờ thêm {wait / 60:.0f} phút rồi quét lại")
    run_bg(f.scan(c.league, c.flip_types, int(c.flip_max_items), float(c.flip_min_volume)))
    return {"ok": True}


# ---------------- timeless finder (Vilsol tree + buyout price per seed) ----------------
VILSOL_URL = "https://vilsol.github.io/timeless-jewels/tree"


@app.get("/api/finder/info")
async def finder_info():
    from .timeless import JEWEL_CONQUERORS
    return {"url": VILSOL_URL, "jewels": JEWEL_CONQUERORS}


def _owned_keys() -> list[dict]:
    """Timeless jewels in the newest snapshot: [{jewel, conq, seed, source}]"""
    db = state["db"]
    s = db.latest_snapshot(cfg().league)
    if not s:
        return []
    out = []
    labels = {r["source"]: r["label"] for r in db.q("SELECT source, label FROM snapshot_sources WHERE snapshot_id=?", (s["id"],))}
    for r in db.q("SELECT source, extra, qty FROM snapshot_items WHERE snapshot_id=? AND category='timeless'", (s["id"],)):
        k = (json.loads(r["extra"] or "{}").get("timeless_key") or "").split("|")
        if len(k) == 5:          # rule|league|jewel|conq|seed
            out.append({"jewel": k[2], "conq": k[3], "seed": int(k[4]), "source": labels.get(r["source"], r["source"]), "qty": r["qty"]})
    return out


@app.post("/api/finder/check")
async def finder_check(body: dict):
    from .timeless import JEWEL_CONQUERORS
    jewel, conq = body.get("jewel"), body.get("conqueror") or "*"
    if jewel not in JEWEL_CONQUERORS or (conq != "*" and conq not in JEWEL_CONQUERORS[jewel]):
        raise HTTPException(400, "Sai loại jewel / conqueror")
    seeds = sorted({int(x) for x in body.get("seeds", []) if str(x).isdigit()})[:200]
    if not seeds:
        raise HTTPException(400, "Chưa có seed nào")
    tp = state["timeless"]
    for sd in seeds:
        tp.enqueue_seed(cfg().league, jewel, conq, sd)
    return {"queued": len(seeds)}


@app.post("/api/finder/results")
async def finder_results(body: dict):
    from .timeless import cache_key
    jewel, conq = body.get("jewel"), body.get("conqueror") or "*"
    seeds = sorted({int(x) for x in body.get("seeds", []) if str(x).isdigit()})[:200]
    tp, league = state["timeless"], cfg().league
    owned = _owned_keys()
    rows = []
    for sd in seeds:
        key = cache_key(league, jewel, conq, sd)
        c = tp.cached(key)
        mine = [o for o in owned if o["jewel"] == jewel and o["seed"] == sd and (conq == "*" or o["conq"] == conq)]
        rows.append({"seed": sd, "status": ("pending" if key in tp.pending else "chưa check") if not c else c["mode"],
                     "price": c["price_chaos"] if c else None, "listings": c["listings"] if c else 0,
                     "detail": c["detail"] if c else "", "owned": [f"{o['conq']} ({o['source']})" for o in mine]})
    return {"rows": rows, "timeless": tp.state(), "divine": state["prices"].divine}


# ---------------- mercenary warrant (trade price) ----------------
@app.post("/api/merc/parse")
async def merc_parse(body: dict):
    try:
        return await state["merc"].parse(body.get("text") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/merc/stash")
async def merc_stash():
    """Warrants found in the tracked tabs/characters at the last snapshot."""
    from .mercenary import load_stash
    from .snapshot import WARRANT_FILE
    return load_stash(WARRANT_FILE, cfg().league)


@app.post("/api/merc/price")
async def merc_price(body: dict):
    """body: {groups:[{skill:id, supports:[id]}], min_level:int, force:bool}"""
    if not cfg().league:
        raise HTTPException(400, "Chưa chọn league trong Cài đặt")
    groups = [{"skill": g.get("skill"), "supports": [s for s in g.get("supports", []) if isinstance(s, str)]}
              for g in body.get("groups", []) if isinstance(g, dict)]
    try:
        return await state["merc"].price(cfg().league, groups, int(body.get("min_level") or 0), bool(body.get("force")))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- gems / div cards / stash clean-up / calculators ----------------
def _need_league():
    if not cfg().league:
        raise HTTPException(400, "Chưa chọn league trong Cài đặt")


@app.get("/api/gems")
async def gems_api(min_profit: float = 1, min_listings: int = 3):
    from . import gems
    _need_league()
    p = state["prices"]
    await p.refresh(cfg().league)
    res = gems.analyse(p)
    res["rows"] = [r for r in res["rows"] if r["profit"] >= min_profit and r["liquidity"] >= min_listings][:3000]
    res["age"] = time.time() - p.loaded_at
    return res


@app.get("/api/divcards")
async def divcards_api():
    from . import chest, divcards
    from .snapshot import STASH_FILE
    _need_league()
    p = state["prices"]
    await p.refresh(cfg().league)
    try:
        cards = await divcards.fetch_wiki(cfgmod.CONFIG_DIR / "divcards_wiki.json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Không tải được danh sách bài từ poewiki.net: {e}")
    owned: dict[str, int] = {}
    for it in chest.load(STASH_FILE, cfg().league).get("items", []):
        if it.get("frame") == 6:
            owned[it["typeLine"]] = owned.get(it["typeLine"], 0) + int(it.get("qty") or 1)
    res = divcards.analyse(p, cards, owned)
    res["age"] = time.time() - p.loaded_at
    return res


@app.get("/api/chest")
async def chest_api(junk_max: float = 1, include_stack: bool = False, recipe_max: float = 5):
    from . import chest
    from .snapshot import STASH_FILE
    data = chest.load(STASH_FILE, cfg().league)
    items = data.get("items", [])
    return {"ts": data.get("ts"), "count": len(items), "recipe": chest.recipe(items, recipe_max),
            "junk": chest.junk(items, junk_max, include_stack),
            "names": sorted({(i["label"], i["name"] + " " + i["typeLine"]) for i in items if i["total"] >= junk_max})}


@app.get("/api/calc/anoint")
async def calc_anoint():
    from . import anoint
    _need_league()
    p = state["prices"]
    await p.refresh(cfg().league)
    try:
        rows = await anoint.fetch(cfgmod.CONFIG_DIR / "anoints_wiki.json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Không tải được công thức anoint từ poewiki.net: {e}")
    oils = sorted({o for r in rows for o in r["oils"]})
    return {"anoints": rows, "oil_prices": {o: p.stack.get(o) for o in oils}, "divine": p.divine}


@app.get("/api/calc/prices")
async def calc_prices():
    p = state["prices"]
    if cfg().league:
        await p.refresh(cfg().league)
    names = ["Chromatic Orb", "Orb of Fusing", "Jeweller's Orb", "Vaal Orb", "Gemcutter's Prism", "Divine Orb"]
    return {n: p.stack.get(n) for n in names}


# ---------------- challenge tracker ----------------
def _ch_path():
    return cfgmod.CONFIG_DIR / "challenges.json"


async def _ch_guide() -> tuple[list[dict], str]:
    """Community guide (CraniumViolence) parsed, with poe.ninja prices filled in; [] if offline."""
    from . import challenges as ch
    p = state["prices"]
    try:
        if cfg().league:
            await p.refresh(cfg().league)
    except Exception:  # noqa: BLE001
        pass

    def price_of(name):
        if name in p.stack:
            return p.stack[name]
        vals = [float(l.get("chaosValue") or 0) for (n, _b), ls in p.uniques.items() if n == name for l in ls if not l.get("links")]
        return min(vals) if vals else None
    try:
        txt = await ch.fetch_guide(cfgmod.CONFIG_DIR / "challenges_guide.json")
        return ch.parse_guide(txt, price_of, p.divine), ""
    except Exception as e:  # noqa: BLE001
        return [], f"Không tải được hướng dẫn: {e}"


async def _ch_view(d) -> dict:
    from . import challenges as ch
    guide, err = await _ch_guide()
    return {**ch.view(d, guide), "url": ch.profile_url(cfg().account_name or ""), "guide_error": err,
            "guide_site": ch.GUIDE_SITE}


@app.get("/api/challenges")
async def ch_get():
    from . import challenges as ch
    return await _ch_view(ch.load(_ch_path(), cfg().league))


@app.post("/api/challenges/sync")
async def ch_sync():
    from . import challenges as ch
    _need_league()
    try:
        html = await ch.fetch(cfg().account_name or "", cfg().poesessid)
        d = ch.merge_sync(ch.load(_ch_path(), cfg().league), ch.parse_html(html), "tự động")
    except (RuntimeError, ValueError, httpx.HTTPError) as e:
        raise HTTPException(502, str(e))
    ch.save(_ch_path(), cfg().league, d)
    return await _ch_view(d)


@app.post("/api/challenges/paste")
async def ch_paste(body: dict):
    from . import challenges as ch
    _need_league()
    try:
        d = ch.merge_sync(ch.load(_ch_path(), cfg().league), ch.parse_html(body.get("html") or ""), "dán tay")
    except ValueError as e:
        raise HTTPException(400, str(e))
    ch.save(_ch_path(), cfg().league, d)
    return await _ch_view(d)


@app.post("/api/challenges/item")
async def ch_item(body: dict):
    """body: {id, note?, star?, done? (true/false, or null = follow the profile page)}"""
    from . import challenges as ch
    d = ch.load(_ch_path(), cfg().league)
    cid = body.get("id")
    if "note" in body:
        if body["note"]:
            d["notes"][cid] = str(body["note"])[:2000]
        else:
            d["notes"].pop(cid, None)
    if "star" in body:
        d["stars"] = [x for x in d["stars"] if x != cid] + ([cid] if body["star"] else [])
    if "done" in body:
        if body["done"] is None:
            d["manual"].pop(cid, None)
        else:
            d["manual"][cid] = bool(body["done"])
    if "step" in body:     # {id, step: "<step text>", step_done: true/false/null}
        from .challenges import _norm
        k = f"{cid}|{_norm(str(body['step']))}"
        if body.get("step_done") is None:
            d["step_manual"].pop(k, None)
        else:
            d["step_manual"][k] = bool(body["step_done"])
    if "milestones" in body:
        d["milestones"] = [int(x) for x in body["milestones"] if str(x).isdigit()]
    ch.save(_ch_path(), cfg().league, d)
    return await _ch_view(d)


# ---------------- game log: map tracker + trades ----------------
@app.get("/api/log/status")
async def log_status():
    from .gamelog import candidates, find_log
    lr = state["log"]
    return {"path": find_log(cfg().client_log_path, cfg().patch_game_path), "status": lr.status,
            "last_scan": lr.last_scan, "tried": candidates(cfg().patch_game_path)}


@app.post("/api/log/scan")
async def log_scan():
    return await scan_log()


@app.get("/api/maps")
async def maps_api(days: float = 1):
    db = state["db"]
    since = time.time() - days * 86400 if days > 0 else 0
    runs = db.q("SELECT * FROM map_runs WHERE started>=? ORDER BY started DESC LIMIT 2000", (since,))
    sess = db.q("SELECT id, name, tag, started, COALESCE(ended, ?) ended FROM sessions", (time.time(),))
    for r in runs:
        s = next((x for x in sess if x["started"] <= r["started"] <= x["ended"]), None)
        r["session"] = f"{s['name']}" + (f" [{s['tag']}]" if s and s["tag"] else "") if s else ""
    n = len(runs)
    act = sum(r["active"] for r in runs)
    span = (max((r["ended"] for r in runs), default=0) - min((r["started"] for r in runs), default=0)) if runs else 0
    by_name: dict[str, dict] = {}
    for r in runs:
        b = by_name.setdefault(r["name"], {"name": r["name"], "n": 0, "active": 0.0, "deaths": 0})
        b["n"] += 1; b["active"] += r["active"]; b["deaths"] += r["deaths"]
    return {"runs": runs, "count": n, "active": act, "avg": act / n if n else 0, "deaths": sum(r["deaths"] for r in runs),
            "per_hour": n / (span / 3600) if span > 600 else None, "by_name": sorted(by_name.values(), key=lambda x: -x["n"])[:30],
            "status": state["log"].status, "last_scan": state["log"].last_scan}


_CUR_ALIAS = {"chaos": "Chaos Orb", "divine": "Divine Orb", "exalted": "Exalted Orb", "exa": "Exalted Orb",
              "alch": "Orb of Alchemy", "alt": "Orb of Alteration", "fusing": "Orb of Fusing", "chrome": "Chromatic Orb",
              "mirror": "Mirror of Kalandra", "gcp": "Gemcutter's Prism", "vaal": "Vaal Orb", "regal": "Regal Orb"}


@app.get("/api/trades")
async def trades_api(days: float = 7):
    db, p = state["db"], state["prices"]
    since = time.time() - days * 86400 if days > 0 else 0
    rows = db.q("SELECT * FROM trades WHERE ts>=? ORDER BY ts DESC LIMIT 3000", (since,))
    for r in rows:
        cur = _CUR_ALIAS.get((r["currency"] or "").lower(), r["currency"] or "")
        rate = p.stack.get(cur)
        r["chaos"] = (r["price"] or 0) * rate if rate and r["price"] else None
    done_in = [r for r in rows if r["dir"] == "in" and r["status"] == "đã giao dịch"]
    done_out = [r for r in rows if r["dir"] == "out" and r["status"] == "đã giao dịch"]
    return {"rows": rows, "asked": sum(1 for r in rows if r["dir"] == "in"), "sold": len(done_in),
            "income": sum(r["chaos"] or 0 for r in done_in), "bought": len(done_out),
            "spent": sum(r["chaos"] or 0 for r in done_out), "status": state["log"].status}


@app.get("/api/item/history")
async def item_history(key: str, limit: int = 500):
    db = state["db"]
    rows = db.q("""SELECT s.id, s.ts, s.divine_rate, SUM(i.qty) qty, SUM(i.total) total
                   FROM snapshot_items i JOIN snapshots s ON s.id = i.snapshot_id
                   WHERE i.key=? AND s.league=? GROUP BY s.id ORDER BY s.id DESC LIMIT ?""", (key, cfg().league, limit))
    rows.reverse()
    for r in rows:
        r["unit"] = r["total"] / r["qty"] if r["qty"] else 0
    return rows


@app.get("/api/topbar")
async def topbar():
    from .gamelog import filter_status
    return {"filter": await asyncio.to_thread(filter_status, cfg().poe_docs_path), "warn_days": cfg().filter_warn_days,
            "lab_url": "https://www.poelab.com/"}


# ---------------- regex helpers ----------------
@app.get("/api/regex/mapmods")
async def regex_mapmods():
    return FileResponse(STATIC / "mapmods.json")


@app.get("/api/regex/items")
async def regex_items(type: str):
    if type not in ("Beast", "Tattoo", "Runegraft"):
        raise HTTPException(400, "type phải là Beast, Tattoo hoặc Runegraft")
    if not cfg().league:
        raise HTTPException(400, "Chưa chọn league trong Cài đặt")
    p = state["prices"]
    await p.refresh(cfg().league)
    items = sorted(p.by_type.get(type, []), key=lambda x: -x["chaos"])
    return {"items": items, "divine": p.divine, "age": time.time() - p.loaded_at}


# ---------------- game patch (minimap + altar colors) ----------------
patch_job: dict = {"running": False, "log": [], "result": None, "error": ""}


def _altar_choices(c: cfgmod.Config) -> dict:
    from .gamepatch import ALTAR_LINES
    return {k: (c.altar_choices.get(k, g) or None) for k, _t, g in ALTAR_LINES}


def _group_colors(c: cfgmod.Config) -> dict:
    from .gamepatch import GROUP_COLORS
    return {**GROUP_COLORS, **c.group_colors}


@app.get("/api/patch")
async def patch_info():
    from . import gamepatch as gp
    c = cfg()
    return {"path": c.patch_game_path, "minimap": c.patch_minimap, "altar": c.patch_altar,
            "lines": [{"key": k, "text": t, "default": g} for k, t, g in gp.ALTAR_LINES],
            "choices": _altar_choices(c), "colors": _group_colors(c), "groups": list(gp.GROUP_COLORS),
            "ooz": gp.ooz is not None, "game_running": gp.game_running(),
            "status": gp.status(c.patch_game_path), "job": patch_job}


@app.post("/api/patch/settings")
async def patch_settings(body: dict):
    c = cfg()
    for k in ("patch_game_path", "patch_minimap", "patch_altar", "altar_choices", "group_colors"):
        if k in body:
            setattr(c, k, body[k])
    cfgmod.save(c)
    return {"ok": True}


@app.post("/api/patch/run")
async def patch_run(body: dict):
    """mode: dry | apply | restore"""
    from . import gamepatch as gp
    if patch_job["running"]:
        raise HTTPException(409, "Đang patch")
    c = cfg()
    mode = body.get("mode", "dry")
    patch_job.update(running=True, log=[], result=None, error="")
    log = logging.getLogger("poewealth.patch")

    def say(msg: str):
        patch_job["log"].append(msg)
        log.info(msg)

    def work():
        return gp.run(c.patch_game_path, minimap=c.patch_minimap, altar=c.patch_altar,
                      altar_choices=_altar_choices(c), group_colors=_group_colors(c),
                      dry_run=mode == "dry", restore_all=mode == "restore", log=say)

    async def job():
        try:
            patch_job["result"] = await asyncio.to_thread(work)
        except Exception as e:  # noqa: BLE001
            log.exception("patch lỗi")
            patch_job["error"] = str(e)
        finally:
            patch_job["running"] = False
    run_bg(job())
    return {"ok": True}


def main():
    import uvicorn
    print(f"PoE Wealth (local) → http://{HOST}:{PORT}")
    print(f"Code đang chạy từ: {Path(__file__).parent}")
    print(f"Log: {setup_logging()}  (POE_WEALTH_DEBUG=1 để in log ra terminal)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
