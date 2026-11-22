"""Read the game's Client.txt: map runs (map tracker) and trade whispers.

Lines used (PoE1 3.x):
  2026/11/22 18:01:02 123456 cffb0719 [DEBUG Client 1234] Generating level 83 area "MapWorldsStrand" with seed 3210404040
  ... [INFO Client 1234] : You have entered Strand.          (older)   /   [INFO Client 1234] [SCENE] Set Source [Strand]
  ... [INFO Client 1234] @From <GUILD> Name: Hi, I would like to buy your X listed for 5 chaos in Covens (stash tab "~b/o"; position: left 3, top 5)
  ... [INFO Client 1234] @To Name: Hi, I'd like to buy your 10 Chaos Orb for my 1 Divine Orb in Covens.
  ... [INFO Client 1234] : Name has joined the area.   /   : Trade accepted.   /   : Name has been slain.

The file is read incrementally (byte offset kept in the DB). A first scan of a huge file only reads the
last LOG_FIRST_SCAN_MB.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path

from .db import DB

LOG_FIRST_SCAN_MB = 150

_LINE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) \S+ \S+ \[[A-Z]+ Client \d+\] (.*)$")
_GEN = re.compile(r'^Generating level (\d+) area "([^"]+)"(?: with seed (\d+))?')
_ENTER = re.compile(r"^: You have entered (.+?)\.$")
_SCENE = re.compile(r"^\[SCENE\] Set Source \[(.+?)\]$")
_FROM = re.compile(r"^@(From|To) (?:<[^>]*> )?([^:]+): (.*)$")
_JOIN = re.compile(r"^: (.+?) has joined the area\.$")
_SLAIN = re.compile(r"^: (.+?) has been slain\.$")
_BUY1 = re.compile(r"(?:Hi, )?I would like to buy your (?P<item>.+?)(?: listed for (?P<price>[\d.,]+) (?P<cur>.+?))? in (?P<league>[^(]+?)"
                   r'(?: \(stash tab "(?P<tab>.*?)"; position: left (?P<x>\d+), top (?P<y>\d+)\))?(?:\s.*)?$', re.I)
_BULK = re.compile(r"(?:Hi, )?I'd like to buy your (?P<qty>[\d.,]+) (?P<item>.+?) for my (?P<price>[\d.,]+) (?P<cur>.+?) in (?P<league>.+?)\.?\s*(?:\s.*)?$", re.I)

TOWN_HINTS = ("_town", "Hideout", "KalguuranSettlersLeague", "HeistHub", "Sanctum", "Menagerie", "Delve", "Labyrinth_Airlock")

SCHEMA = """
CREATE TABLE IF NOT EXISTS map_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT, code TEXT, name TEXT, level INTEGER,
    started REAL, ended REAL, active REAL DEFAULT 0, deaths INTEGER DEFAULT 0, entries INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON map_runs(started);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, dir TEXT, name TEXT, item TEXT, qty REAL, price REAL, currency TEXT, league TEXT,
    tab TEXT, x INTEGER, y INTEGER, status TEXT DEFAULT 'hỏi', raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE TABLE IF NOT EXISTS log_state (path TEXT PRIMARY KEY, offset INTEGER, size INTEGER, ctx TEXT);
"""


def candidates(game_path: str = "") -> list[str]:
    out = []
    if game_path:          # patch_game_path points at Content.ggpk or Bundles2/_.index.bin
        d = os.path.dirname(game_path)
        for base in (d, os.path.dirname(d), game_path):
            out.append(os.path.join(base, "logs", "Client.txt"))
    home = str(Path.home())
    out += [
        "/mnt/games/PoE/logs/Client.txt",
        f"{home}/.local/share/Steam/steamapps/common/Path of Exile/logs/Client.txt",
        f"{home}/.steam/steam/steamapps/common/Path of Exile/logs/Client.txt",
        r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile\logs\Client.txt",
        r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile\logs\Client.txt",
    ]
    out += glob.glob("/mnt/*/SteamLibrary/steamapps/common/Path of Exile/logs/Client.txt")
    return out


def find_log(cfg_path: str, game_path: str) -> str | None:
    for p in ([cfg_path] if cfg_path else []) + candidates(game_path):
        if p and os.path.isfile(p):
            return p
    return None


def _ts(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y/%m/%d %H:%M:%S"))


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def is_map_area(code: str, level: int) -> bool:
    return code.startswith("Map") and not any(h in code for h in TOWN_HINTS)


def is_town(code: str) -> bool:
    return any(h in code for h in TOWN_HINTS)


class LogReader:
    def __init__(self, db: DB):
        self.db = db
        with db.conn:
            db.conn.executescript(SCHEMA)
        self.path: str | None = None
        self.status = ""
        self.last_scan = 0.0

    # ---------------- scanning ----------------
    def scan(self, path: str, my_chars: set[str], max_bytes: int | None = None) -> dict:
        """Read new lines since the stored offset. Returns {lines, runs, trades}."""
        self.path = path
        size = os.path.getsize(path)
        st = self.db.one("SELECT * FROM log_state WHERE path=?", (path,))
        offset = st["offset"] if st else 0
        ctx = json.loads(st["ctx"]) if st and st["ctx"] else {}
        if offset > size:                      # file was truncated / replaced
            offset, ctx = 0, {}
        if not st and size > LOG_FIRST_SCAN_MB * 1024 * 1024:
            offset = size - LOG_FIRST_SCAN_MB * 1024 * 1024
        stats = {"lines": 0, "runs": 0, "trades": 0}
        with open(path, "rb") as f:
            f.seek(offset)
            if offset and not st:
                f.readline()                   # first scan started mid-file: skip the partial line
            start = f.tell()
            data = f.read(max_bytes) if max_bytes else f.read()
        cut = data.rfind(b"\n")               # only consume complete lines
        if cut < 0:
            return stats
        chunk, new_offset = data[:cut + 1], start + cut + 1
        with self.db.conn:
            for raw in chunk.decode("utf-8", "replace").splitlines():
                self._line(raw, ctx, my_chars, stats)
            self.db.conn.execute("INSERT OR REPLACE INTO log_state VALUES (?,?,?,?)",
                                 (path, new_offset, size, json.dumps(ctx)))
        self.last_scan = time.time()
        return stats

    def _line(self, raw: str, ctx: dict, my_chars: set[str], stats: dict) -> None:
        m = _LINE.match(raw)
        if not m:
            return
        stats["lines"] += 1
        ts, msg = _ts(m.group(1)), m.group(2)
        conn = self.db.conn
        g = _GEN.match(msg)
        if g:
            level, code, seed = int(g.group(1)), g.group(2), g.group(3) or ""
            prev = ctx.get("area") or {}
            self._close_area(ctx, ts)
            ctx["area"] = {"code": code, "level": level, "seed": seed, "since": ts}
            # a new map is always opened from a hideout/town map device; a map-like area entered straight
            # from inside a run (vaal side area, league encounter area…) belongs to that run
            from_run = bool(prev.get("run")) and not is_town(prev.get("code", ""))
            if is_map_area(code, level) and from_run and ctx.get("run"):
                ctx["area"]["run"] = ctx["run"]
                return
            if is_map_area(code, level):
                run = conn.execute("SELECT id FROM map_runs WHERE seed=? AND code=? AND ended > ?",
                                   (seed, code, ts - 6 * 3600)).fetchone() if seed else None
                if run:
                    ctx["run"] = run[0]
                    conn.execute("UPDATE map_runs SET entries=entries+1, ended=? WHERE id=?", (ts, run[0]))
                else:
                    cur = conn.execute("INSERT INTO map_runs(seed,code,name,level,started,ended) VALUES (?,?,?,?,?,?)",
                                       (seed, code, code, level, ts, ts))
                    ctx["run"] = cur.lastrowid
                    stats["runs"] += 1
                ctx["area"]["run"] = ctx["run"]
            elif is_town(code) or level < 68:
                ctx["area"]["run"] = None       # hideout/town: run keeps going but no active time
            else:
                ctx["area"]["run"] = ctx.get("run")   # side areas from a map (vaal room, league area…)
            return
        e = _ENTER.match(msg) or _SCENE.match(msg)
        if e:
            a = ctx.get("area") or {}
            if a.get("run") and a.get("code", "").startswith("Map"):
                conn.execute("UPDATE map_runs SET name=? WHERE id=? AND name=code", (e.group(1), a["run"]))
            return
        w = _FROM.match(msg)
        if w:
            self._whisper(ts, w.group(1), w.group(2).strip(), w.group(3), ctx, stats)
            return
        j = _JOIN.match(msg)
        if j:
            ctx.setdefault("joined", {})[j.group(1)] = ts
            return
        if msg.startswith(": Trade accepted."):
            self._trade_done(ts, ctx)
            return
        s = _SLAIN.match(msg)
        if s and (not my_chars or s.group(1) in my_chars):
            a = ctx.get("area") or {}
            if a.get("run"):
                conn.execute("UPDATE map_runs SET deaths=deaths+1 WHERE id=?", (a["run"],))

    def _close_area(self, ctx: dict, ts: float) -> None:
        a = ctx.get("area")
        if a and a.get("run"):
            dt = max(0.0, min(ts - a["since"], 3 * 3600))    # cap: game left open
            self.db.conn.execute("UPDATE map_runs SET active=active+?, ended=MAX(ended, ?) WHERE id=?", (dt, ts, a["run"]))

    def _whisper(self, ts, direction, name, text, ctx, stats):
        ctx["pending"] = {k: v for k, v in ctx.get("pending", {}).items() if ts - v[1] < 3600}
        d = "in" if direction == "From" else "out"
        b = _BULK.search(text)
        m = None if b else _BUY1.search(text)
        if not b and not m:
            return                                  # normal chat
        if b:
            row = (ts, d, name, b.group("item"), _num(b.group("qty")), _num(b.group("price")), b.group("cur"),
                   b.group("league").strip(), None, None, None, text[:500])
        else:
            row = (ts, d, name, m.group("item"), 1.0, _num(m.group("price")), m.group("cur"), m.group("league").strip(),
                   m.group("tab"), int(m.group("x")) if m.group("x") else None, int(m.group("y")) if m.group("y") else None,
                   text[:500])
        cur = self.db.conn.execute(
            "INSERT INTO trades(ts,dir,name,item,qty,price,currency,league,tab,x,y,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        ctx.setdefault("pending", {})[name] = [cur.lastrowid, ts]
        stats["trades"] += 1

    def _trade_done(self, ts, ctx):
        """'Trade accepted.' has no name: credit the latest whisper partner who joined our area recently."""
        pend, joined = ctx.get("pending", {}), ctx.get("joined", {})
        best = None
        for name, (tid, wts) in pend.items():
            jts = joined.get(name)
            if ts - wts < 3600 and (jts is None or ts - jts < 1800):
                score = jts or wts
                if not best or score > best[0]:
                    best = (score, name, tid)
        if best:
            self.db.conn.execute("UPDATE trades SET status='đã giao dịch' WHERE id=?", (best[2],))
            pend.pop(best[1], None)
        # forget stale entries so the context stays small
        ctx["pending"] = {k: v for k, v in pend.items() if ts - v[1] < 3600}
        ctx["joined"] = {k: v for k, v in joined.items() if ts - v < 3600}


# ---------------- filter age ----------------
def docs_candidates() -> list[str]:
    home = str(Path.home())
    out = glob.glob(f"{home}/.local/share/Steam/steamapps/compatdata/*/pfx/drive_c/users/steamuser/Documents/My Games/Path of Exile")
    out += glob.glob(f"{home}/.steam/steam/steamapps/compatdata/*/pfx/drive_c/users/steamuser/Documents/My Games/Path of Exile")
    out += glob.glob("/mnt/*/SteamLibrary/steamapps/compatdata/*/pfx/drive_c/users/steamuser/Documents/My Games/Path of Exile")
    out += [f"{home}/Documents/My Games/Path of Exile", os.path.expandvars(r"%USERPROFILE%\Documents\My Games\Path of Exile")]
    return out


def filter_status(cfg_path: str) -> dict:
    """Newest loot filter file: local *.filter or online filters (FilterBlade sync) cached in OnlineFilters/."""
    for d in ([cfg_path] if cfg_path else []) + docs_candidates():
        if not d or not os.path.isdir(d):
            continue
        files = [f for f in glob.glob(os.path.join(d, "*.filter")) + glob.glob(os.path.join(d, "OnlineFilters", "*"))
                 if os.path.isfile(f)]
        if not files:
            return {"dir": d, "file": None, "mtime": None}
        newest = max(files, key=os.path.getmtime)
        name = os.path.basename(newest)
        if "OnlineFilters" in newest:
            try:   # online filter files start with a comment line holding the filter name
                with open(newest, "r", encoding="utf-8", errors="replace") as f:
                    for ln in f.read(4000).splitlines():
                        if ln.strip().startswith("#") and len(ln.strip()) > 2:
                            name = ln.strip("# ").strip()[:80]
                            break
            except OSError:
                pass
        return {"dir": d, "file": name, "mtime": os.path.getmtime(newest), "online": "OnlineFilters" in newest}
    return {"dir": None, "file": None, "mtime": None}
