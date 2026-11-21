"""Config: stored only on this machine (~/.config/poe-wealth/config.json, chmod 600)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("POE_WEALTH_HOME", Path.home() / ".config" / "poe-wealth"))
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = CONFIG_DIR / "wealth.sqlite3"

USER_AGENT = "poe-local-wealth/0.1 (personal local tool)"


@dataclass
class Config:
    poesessid: str = ""
    account_name: str = ""          # e.g. "duc#1234" (with discriminator)
    realm: str = "pc"
    league: str = ""                # e.g. "Keepers" / exact trade league name
    tab_ids: list[str] = field(default_factory=list)   # tracked stash tab ids
    include_characters: bool = True
    characters: list[str] = field(default_factory=list)  # empty = all chars in league
    auto_snapshot_minutes: int = 0  # 0 = off
    price_timeless: bool = True
    min_item_value: float = 1.0     # chaos; items below are summed but not listed

    def public(self) -> dict:
        d = asdict(self)
        d["poesessid"] = bool(self.poesessid)  # never send cookie to the browser
        return d


def load() -> Config:
    cfg = Config()
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text())
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    env = os.environ.get("POESESSID")
    if env:
        cfg.poesessid = env
    return cfg


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
    os.replace(tmp, CONFIG_FILE)
