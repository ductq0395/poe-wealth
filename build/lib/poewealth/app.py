"""Local web app. Binds to 127.0.0.1 only."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config as cfgmod
from .db import DB
from .poeapi import PoeClient, PoeError
from .pricing import Prices
from .ratelimit import Limiter
from .snapshot import diff_snapshots, progress, take_snapshot
from .timeless import TimelessPricer

HOST = "127.0.0.1"
PORT = int(os.environ.get("POE_WEALTH_PORT", "8787"))
STATIC = Path(__file__).parent / "static"

state: dict = {}


def cfg() -> cfgmod.Config:
    return state["cfg"]


def new_client() -> PoeClient:
    return PoeClient(cfg(), state["limiter"])


async def _snapshot_then(after=None):
    try:
        sid = await take_snapshot(cfg(), state["db"], state["prices"], state["timeless"], state["limiter"])
        if after:
            after(sid)
    except Exception:  # noqa: BLE001 - error is in progress["error"]
        pass


def run_bg(coro) -> None:
    t = asyncio.create_task(coro)
    state.setdefault("tasks", set()).add(t)
    t.add_done_callback(state["tasks"].discard)


async def auto_loop():
    while True:
        await asyncio.sleep(30)
        mins = cfg().auto_snapshot_minutes
        if mins <= 0 or progress["running"] or not cfg().league:
            continue
        last = state["db"].latest_snapshot(cfg().league)
        if not last or time.time() - last["ts"] >= mins * 60:
            await _snapshot_then()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["cfg"] = cfgmod.load()
    state["db"] = DB(cfgmod.DB_FILE)
    state["limiter"] = Limiter()
    state["prices"] = Prices(state["limiter"])
    tp = TimelessPricer(state["db"], state["prices"])
    tp.client_factory = new_client
    state["timeless"] = tp
    loop_task = asyncio.create_task(auto_loop())
    yield
    loop_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        return JSONResponse({"detail": "bad host"}, status_code=403)   # DNS-rebinding guard
    if request.method != "GET" and request.headers.get("x-local-app") != "1":
        return JSONResponse({"detail": "missing header"}, status_code=403)  # CSRF guard
    return await call_next(request)


@app.exception_handler(PoeError)
async def poe_err(_, e: PoeError):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


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
        return [{"id": t["id"], "i": t["i"], "n": t["n"], "type": t.get("type"),
                 "colour": t.get("colour")} for t in await c.stash_tabs()]
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


@app.get("/api/status")
async def status():
    return {"progress": progress, "timeless": state["timeless"].state(),
            "prices_age": time.time() - state["prices"].loaded_at if state["prices"].loaded_at else None,
            "divine": state["prices"].divine}


@app.get("/api/history")
async def history(limit: int = 500):
    rows = state["db"].q(
        "SELECT id, ts, total_chaos, divine_rate FROM snapshots WHERE league=? ORDER BY id DESC LIMIT ?",
        (cfg().league, limit))
    return list(reversed(rows))


def _snapshot_detail(sid: int) -> dict:
    db = state["db"]
    snap = db.one("SELECT * FROM snapshots WHERE id=?", (sid,))
    if not snap:
        raise HTTPException(404, "Không có snapshot")
    sources = db.q("SELECT source,label,value,item_count FROM snapshot_sources WHERE snapshot_id=? ORDER BY value DESC", (sid,))
    items = db.q("SELECT source,name,category,qty,unit,total,icon,extra FROM snapshot_items WHERE snapshot_id=? ORDER BY total DESC", (sid,))
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

    def after(sid):
        with db.conn:
            db.conn.execute("INSERT INTO sessions(name,start_snapshot,started) VALUES (?,?,?)", (name, sid, time.time()))
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
        if r["end_value"] is not None:
            hours = max((r["ended"] - r["started"]) / 3600, 1e-6)
            r["profit"] = r["end_value"] - r["start_value"]
            r["per_hour"] = r["profit"] / hours
    return rows


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


def main():
    import uvicorn
    print(f"PoE Wealth (local) → http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
