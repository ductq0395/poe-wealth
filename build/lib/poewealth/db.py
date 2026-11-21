"""SQLite storage (local file next to the config)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    league TEXT NOT NULL,
    total_chaos REAL NOT NULL,
    divine_rate REAL NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshot_sources (
    snapshot_id INTEGER NOT NULL,
    source TEXT NOT NULL,          -- "tab:<id>" or "char:<name>"
    label TEXT NOT NULL,
    value REAL NOT NULL,
    item_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    key TEXT NOT NULL,             -- aggregation key (name|base|variant)
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    qty REAL NOT NULL,
    unit REAL NOT NULL,
    total REAL NOT NULL,
    icon TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_snap ON snapshot_items(snapshot_id);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_snapshot INTEGER NOT NULL,
    end_snapshot INTEGER,
    started REAL NOT NULL,
    ended REAL
);
CREATE TABLE IF NOT EXISTS timeless_prices (
    key TEXT PRIMARY KEY,          -- league|jewel|conqueror|seed
    price_chaos REAL,
    listings INTEGER,
    mode TEXT,                     -- exact | seed-only | none | error
    detail TEXT,
    fetched REAL NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    # ---- snapshots ----
    def save_snapshot(self, league: str, divine: float, sources: list[dict], items: list[dict]) -> int:
        total = sum(s["value"] for s in sources)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO snapshots(ts, league, total_chaos, divine_rate) VALUES (?,?,?,?)",
                (time.time(), league, total, divine))
            sid = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO snapshot_sources VALUES (?,?,?,?,?)",
                [(sid, s["source"], s["label"], s["value"], s["count"]) for s in sources])
            self.conn.executemany(
                "INSERT INTO snapshot_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(sid, i["source"], i["key"], i["name"], i["category"], i["qty"], i["unit"], i["total"],
                  i.get("icon"), json.dumps(i.get("extra") or {})) for i in items])
        self.prune_items()
        return sid

    def prune_items(self, keep: int = 200) -> None:
        """Keep item detail only for the newest `keep` snapshots and session endpoints."""
        with self.conn:
            self.conn.execute(
                """DELETE FROM snapshot_items WHERE snapshot_id NOT IN (
                       SELECT id FROM snapshots ORDER BY id DESC LIMIT ?)
                   AND snapshot_id NOT IN (SELECT start_snapshot FROM sessions)
                   AND snapshot_id NOT IN (SELECT end_snapshot FROM sessions WHERE end_snapshot IS NOT NULL)""",
                (keep,))

    def latest_snapshot(self, league: str) -> dict | None:
        return self.one("SELECT * FROM snapshots WHERE league=? ORDER BY id DESC LIMIT 1", (league,))

    # ---- timeless cache ----
    def timeless_get(self, key: str, max_age: float) -> dict | None:
        r = self.one("SELECT * FROM timeless_prices WHERE key=?", (key,))
        if r and time.time() - r["fetched"] < max_age:
            return r
        return None

    def timeless_put(self, key: str, price: float | None, listings: int, mode: str, detail: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO timeless_prices VALUES (?,?,?,?,?,?)",
                (key, price, listings, mode, detail, time.time()))
