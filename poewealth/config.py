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
    # game file patch (minimap + Eldritch altar colors)
    patch_game_path: str = ""       # Content.ggpk or Bundles2/_.index.bin
    patch_minimap: bool = True
    patch_altar: bool = True
    altar_choices: dict = field(default_factory=dict)   # stat id -> group, "" = off; missing = default
    group_colors: dict = field(default_factory=dict)    # group -> "#rrggbb"; missing = default
    # price-change alerts
    alert_pct: float = 20.0         # % change of unit price
    alert_hours: float = 24.0       # compare with snapshot this many hours ago
    alert_min_total: float = 20.0   # chaos value at stake (qty x price), filters noise like 1c -> 2c
    alert_notify: bool = True       # desktop notification (notify-send) after each snapshot
    # currency exchange flip (beta)
    flip_types: list = field(default_factory=lambda: ["Currency", "Fragment", "Essence", "Fossil", "Omen",
                                                     "Thread", "DeliriumOrb", "Oil"])
    flip_max_items: int = 150
    flip_min_volume: float = 2000.0   # chaos traded per day (poe.ninja volume)
    flip_min_spread: float = 5.0      # %
    flip_capital_div: float = 10.0
    # regex tab
    regex_mapmods: list = field(default_factory=list)   # map mod keys to avoid
    regex_map_min: dict = field(default_factory=dict)   # {"quantity": 80, "rarity": 0, "packsize": 0}
    regex_threshold: dict = field(default_factory=dict) # {"Beast": 10, "Tattoo": 5, "Runegraft": 5} chaos
    regex_override: dict = field(default_factory=dict)  # {"Beast": {"name": true/false}} manual tick
    # game log (map tracker / trade history) + filter age
    client_log_path: str = ""       # empty = auto-detect logs/Client.txt
    poe_docs_path: str = ""         # "My Games/Path of Exile" folder; empty = auto-detect
    filter_warn_days: float = 3.0

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
