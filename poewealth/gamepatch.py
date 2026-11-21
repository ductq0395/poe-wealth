"""Game file patches (port of PoeRedux, https://github.com/Gineticus/PoeRedux).

* Minimap  – whole minimap visible from the start (2 shaders).
* Altar colors – "Color Mods" limited to the 14 Eldritch altar lines (Eater of Worlds /
  Searing Exarch altars): divine-orb / duplicate lines get a color, dangerous ones red.

Works on standalone Content.ggpk and Steam/Epic Bundles2/_.index.bin.
Modifying game files is against GGG's ToS – the user accepts that risk.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from pathlib import Path
from typing import Callable

try:
    import ooz  # package "pyooz"
except ImportError:  # app still runs, the patch tab explains how to install it
    ooz = None

from .config import CONFIG_DIR

BACKUP_ROOT = CONFIG_DIR / "game-backup"
# backups made by the standalone poe_minimap.py script
LEGACY_MINIMAP_ROOT = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "poe-minimap"

# ----------------------------------------------------------------------------- patch definitions
MINIMAP_FILES = ["shaders/minimap_blending_pixel.hlsl", "shaders/minimap_visibility_pixel.hlsl"]
STATDESC_DIR = "metadata/statdescriptions"

GROUP_COLORS = {  # PoeRedux defaults
    "Dangerous": "#d12e2e",
    "Annoying": "#ffcc1b",
    "Good": "#4ae63a",
    "Great": "#0ebaff",
    "Neutral": "#e39eff",
}

# Eldritch altar lines (PoeRedux ModLineCatalog.Poe1, category "Altar") with their default group.
ALTAR_LINES = [
    ("chance_%_to_drop_additional_divine_orb", "#% chance to drop an additional Divine Orb", "Good"),
    ("map_boss_additional_divine_orb_to_drop", "Final Boss drops # additional Divine Orb", "Good"),
    # 3.30: Searing Exarch / Eater of Worlds altars can also drop Chaos / Annulment / Exalted (stat ids from poewiki)
    ("chance_%_to_drop_additional_chaos_orb", "#% chance to drop an additional Chaos Orb", "Good"),
    ("map_boss_additional_chaos_orb_to_drop", "Final Boss drops # additional Chaos Orbs", "Good"),
    ("chance_%_to_drop_additional_orb_of_annulment", "#% chance to drop an additional Orb of Annulment", "Good"),
    ("map_boss_additional_orb_of_annulment_to_drop", "Final Boss drops # additional Orbs of Annulment", "Good"),
    ("chance_%_to_drop_additional_exalted_orb", "#% chance to drop an additional Exalted Orb", "Good"),
    ("map_boss_additional_exalted_orb_to_drop", "Final Boss drops # additional Exalted Orbs", "Good"),
    ("chance_%_to_drop_additional_eldritch_chaos_orb", "#% chance to drop an additional Eldritch Chaos Orb", ""),
    ("map_boss_additional_eldritch_chaos_orb_to_drop", "Final Boss drops # additional Eldritch Chaos Orbs", ""),
    ("chance_%_to_drop_additional_eldritch_orb_of_annulment", "#% chance to drop an additional Eldritch Orb of Annulment", ""),
    ("map_boss_additional_eldritch_orb_of_annulment_to_drop", "Final Boss drops # additional Eldritch Orbs of Annulment", ""),
    ("chance_%_to_drop_additional_eldritch_exalted_orb", "#% chance to drop an additional Eldritch Exalted Orb", ""),
    ("map_boss_additional_eldritch_exalted_orb_to_drop", "Final Boss drops # additional Eldritch Exalted Orbs", ""),
    ("chance_%_to_drop_additional_regal_orb", "#% chance to drop an additional Regal Orb", ""),
    ("chance_%_to_drop_additional_vaal_orb", "#% chance to drop an additional Vaal Orb", ""),
    ("%_chance_to_duplicate_dropped_gems", "Gems … #% chance to be Duplicated", "Great"),
    ("%_chance_to_duplicate_dropped_maps", "Maps … #% chance to be Duplicated", "Great"),
    ("%_chance_to_duplicate_dropped_currency", "Basic Currency Items … #% chance to be Duplicated", "Great"),
    ("%_chance_to_duplicate_dropped_divination_cards", "Divination Cards … #% chance to be Duplicated", "Great"),
    ("%_chance_to_duplicate_dropped_scarabs", "Threads … #% chance to be Duplicated", "Great"),
    ("%_chance_to_duplicate_dropped_uniques", "Unique Items … #% chance to be Duplicated", "Great"),
    ("map_item_drop_quantity_+%", "#% increased Quantity of Items found in this Area", "Neutral"),
    ("life_mana_es_recovery_rate_+%_per_endurance_charge", "#% Recovery Rate of Life/Mana/ES per Endurance Charge", "Dangerous"),
    ("global_defences_+%_per_frenzy_charge", "#% Defences per Frenzy Charge", "Dangerous"),
    ("critical_strike_multiplier_+_per_power_charge", "#% Critical Strike Multiplier per Power Charge", "Dangerous"),
    ("random_projectile_direction", "Projectiles are fired in random directions", "Dangerous"),
    ("chaos_damage_per_minute_while_affected_by_flask", "Take # Chaos Damage per second during any Flask Effect", "Dangerous"),
    ("create_enemy_meteor_daemon_on_flask_use_%_chance", "#% chance to be targeted by a Meteor when you use a Flask", "Dangerous"),
    ("drain_x_flask_charges_over_time_on_hit_for_6_seconds", "Enemies lose # Flask Charges every 3 seconds …", "Dangerous"),
]


def patch_visibility(data: bytes) -> bytes | None:
    text = data.decode("latin-1")
    if "res_color = max(res_color, 0.18f);" in text:
        return None
    lines = text.split("\r\n")
    idx = next((i for i, l in enumerate(lines) if "res_color = float4(1.0f, 0.0f, 0.0f, 1.0f);" in l), -1)
    if idx == -1:
        return None
    lines.insert(idx + 1, "\tres_color = max(res_color, 0.18f);")
    return "\r\n".join(lines).encode("latin-1")


def patch_blending(data: bytes) -> bytes | None:
    text = data.decode("latin-1")
    text = text.replace(
        "float4 walkable_color = float4(1.0f, 1.0f, 1.0f, 0.01f);",
        "float4 walkable_color = float4(0.0f, 0.0f, 0.0f, 0.3f);")
    text = text.replace(
        "float4 walkability_map_color = lerp(walkable_color, float4(0.5f, 0.5f, 1.0f, 0.5f), walkable_to_edge_ratio);",
        "float4 walkability_map_color = lerp(walkable_color, float4(12.0f, 12.0f, 12.0f, 0.1f), walkable_to_edge_ratio);")
    return text.encode("latin-1")


MINIMAP_PATCHES = {
    "shaders/minimap_blending_pixel.hlsl": patch_blending,
    "shaders/minimap_visibility_pixel.hlsl": patch_visibility,
}

# --- Color Mods (ColorMods.cs) ---
_OUR_MARKUP = re.compile(r"<rgb\(\d+,\d+,\d+\)>\{\{(.*?)\}\}")


def _markup(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"rgb({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)})"


def _recolor_line(line: str, color: str | None) -> str:
    if color is None:
        return _OUR_MARKUP.sub(lambda m: m.group(1), line)
    if "<" in line:
        return re.sub(r"<.*?>", lambda _m: f"<{color}>", line)
    return re.sub(r'".*?"', lambda m: f'"<{color}>{{{{{m.group(0).replace(chr(34), "")}}}}}"', line)


def _int_or_none(s: str) -> int | None:
    try:
        return int(s.replace("\t", "").split(" ")[0])
    except ValueError:
        return None


def recolor_text(text: str, colors: dict[str, str | None]) -> tuple[str, int]:
    """colors: first stat id -> rgb(...) markup, or None to remove our color. Returns (text, blocks found)."""
    lines = text.split("\r\n")
    state, color, left, found = "seek", None, 0, 0
    for i, line in enumerate(lines):
        if line.startswith("description"):
            state = "stat"
            continue
        if state == "stat":
            words = line.split(" ")
            if len(words) >= 2 and words[1] in colors:
                color = colors[words[1]]
                found += color is not None
                state = "count"
            else:
                state = "seek"
        elif state == "count":
            n = _int_or_none(line)
            if n is not None:
                left = n
                state = "recolor" if n > 0 else "count"
        elif state == "recolor":
            lines[i] = _recolor_line(line, color)
            left -= 1
            if left == 0:
                state = "count"
    return "\r\n".join(lines), found


def make_csd_transform(colors: dict[str, str | None], counter: dict) -> Callable[[bytes], bytes | None]:
    def tf(data: bytes) -> bytes | None:
        text = data.decode("utf-16-le")
        body = text[1:] if text.startswith("\ufeff") else text
        if not body or not any(k in body for k in colors):
            return None
        out, found = recolor_text(body, colors)
        counter["blocks"] = counter.get("blocks", 0) + found
        if out == body:
            return None
        return b"\xff\xfe" + out.encode("utf-16-le")
    return tf


# --------------------------------------------------------------------------- hashing
M64 = 0xFFFFFFFFFFFFFFFF


def murmur64a(data: bytes, seed: int = 0x1337B33F) -> int:
    """Path hash used by the bundle index since PoE 3.21.2 (input: lowercase utf-8)."""
    if not data:
        return 0xF42A94E69CFF42FE
    if data.endswith(b"/"):
        data = data[:-1]
    m, r = 0xC6A4A7935BD1E995, 47
    h = (seed ^ (len(data) * m)) & M64
    n8 = len(data) // 8
    for (k,) in struct.iter_unpack("<Q", data[: n8 * 8]):
        k = (k * m) & M64
        k ^= k >> r
        k = (k * m) & M64
        h = ((h ^ k) * m) & M64
    rem = data[n8 * 8:]
    if rem:
        h = ((h ^ int.from_bytes(rem, "little")) * m) & M64
    h = ((h ^ (h >> r)) * m) & M64
    return h ^ (h >> r)


# --------------------------------------------------------------------------- bundle (Oodle chunks)
HDR = struct.Struct("<iiiiiqqii4i")  # 60 bytes
# Oodle compressor id -> block decoder type used for stored (uncompressed) blocks
DECODER_TYPE = {8: 6, 9: 10, 11: 10, 12: 6, 13: 12}


class Bundle:
    def __init__(self, data: bytes):
        f = HDR.unpack_from(data, 0)
        (self.unc, _comp, _head, self.compressor, self.unk1, _u, _c,
         count, self.chunk_size, *self.unk) = f
        if self.chunk_size != 0x40000:
            raise ValueError(f"chunk size lạ: {self.chunk_size}")
        sizes = struct.unpack_from(f"<{count}i", data, HDR.size)
        pos = 12 + _head
        self.chunks: list[bytes] = []
        for s in sizes:
            self.chunks.append(data[pos:pos + s])
            pos += s
        self._raw: dict[int, bytes] = {}

    def chunk_raw_len(self, i: int) -> int:
        return self.chunk_size if i < len(self.chunks) - 1 else self.unc - self.chunk_size * i

    def raw_chunk(self, i: int) -> bytes:
        if i not in self._raw:
            n = self.chunk_raw_len(i)
            out = ooz.decompress(self.chunks[i], n)
            if len(out) != n:
                raise ValueError(f"giải nén chunk {i} thất bại")
            self._raw[i] = out
        return self._raw[i]

    def read(self, offset: int, size: int) -> bytes:
        if size == 0:
            return b""
        a, b = offset // self.chunk_size, (offset + size - 1) // self.chunk_size
        buf = b"".join(self.raw_chunk(i) for i in range(a, b + 1))
        start = offset - a * self.chunk_size
        return buf[start:start + size]

    def read_all(self) -> bytes:
        return b"".join(self.raw_chunk(i) for i in range(len(self.chunks)))

    def stored(self, raw: bytes) -> bytes:
        # Oodle block header: 0x0C magic | 0x40 uncompressed | 0x80 restart, then decoder type
        return bytes((0xCC, DECODER_TYPE.get(self.compressor, 6))) + raw

    def serialize(self) -> bytes:
        comp = sum(map(len, self.chunks))
        n = len(self.chunks)
        hdr = HDR.pack(self.unc, comp, 48 + 4 * n, self.compressor, self.unk1,
                       self.unc, comp, n, self.chunk_size, *self.unk)
        return hdr + struct.pack(f"<{n}i", *map(len, self.chunks)) + b"".join(self.chunks)

    def append(self, datas: list[bytes]) -> list[int]:
        """Append files at the end; only the last chunk and new ones are re-encoded."""
        last = len(self.chunks) - 1
        base = self.chunk_size * last if last >= 0 else 0
        tail = self.raw_chunk(last) if last >= 0 else b""
        offsets, pos, parts = [], self.unc, [tail]
        for d in datas:
            offsets.append(pos)
            pos += len(d)
            parts.append(d)
        buf = b"".join(parts)
        keep = self.chunks[:last] if last >= 0 else []
        new = [self.stored(buf[i:i + self.chunk_size]) for i in range(0, len(buf), self.chunk_size)]
        self.chunks = keep + new
        self.unc = base + len(buf)
        self._raw = {}
        return offsets

    def replace_raw(self, old: bytes, new: bytes) -> None:
        """Same-size content change: re-encode only the chunks that differ."""
        assert len(old) == len(new) == self.unc
        cs = self.chunk_size
        for i in range(len(self.chunks)):
            if old[i * cs:(i + 1) * cs] != new[i * cs:(i + 1) * cs]:
                self.chunks[i] = self.stored(new[i * cs:(i + 1) * cs])
        self._raw = {}


# --------------------------------------------------------------------------- GGPK container
TAG_FILE, TAG_PDIR, TAG_FREE, TAG_GGPK = b"FILE", b"PDIR", b"FREE", b"GGPK"


class Ggpk:
    def __init__(self, path: Path, writable: bool):
        self.f = open(path, "r+b" if writable else "rb")
        length, tag, self.version, self.root_off, self.first_free = struct.unpack("<I4sIqq", self.f.read(28))
        if tag != TAG_GGPK:
            raise ValueError("Không phải file GGPK")
        self.cw = 4 if self.version == 4 else 2  # char width of names
        self.enc = "utf-32-le" if self.version == 4 else "utf-16-le"

    def _read(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        return self.f.read(n)

    def read_dir(self, off: int) -> dict:
        length, tag, name_len, count = struct.unpack("<I4sii", self._read(off, 16))
        assert tag == TAG_PDIR, f"PDIR expected at {off}"
        name = self._read(off + 48, (name_len - 1) * self.cw).decode(self.enc)
        ent_off = off + length - 12 * count
        raw = self._read(ent_off, 12 * count)
        entries = [struct.unpack_from("<Iq", raw, i * 12) for i in range(count)]
        return dict(off=off, length=length, name=name, entries=entries, ent_off=ent_off)

    def node_info(self, off: int) -> tuple[bytes, str]:
        length, tag, name_len = struct.unpack("<I4si", self._read(off, 12))
        name_at = off + (48 if tag == TAG_PDIR else 44)
        return tag, self._read(name_at, (name_len - 1) * self.cw).decode(self.enc)

    def node_hash(self, off: int) -> bytes:
        tag = self._read(off + 4, 4)
        return self._read(off + (16 if tag == TAG_PDIR else 12), 32)

    def find(self, path: str) -> list[tuple[dict, int]]:
        """Return chain [(dir, entry_index), ...] from root to the node at `path`."""
        chain, d = [], self.read_dir(self.root_off)
        parts = path.split("/")
        for depth, part in enumerate(parts):
            for i, (_h, off) in enumerate(d["entries"]):
                tag, name = self.node_info(off)
                if name.lower() == part.lower():
                    chain.append((d, i))
                    if depth < len(parts) - 1:
                        if tag != TAG_PDIR:
                            raise FileNotFoundError(path)
                        d = self.read_dir(off)
                    break
            else:
                raise FileNotFoundError(f"Không tìm thấy trong GGPK: {path}")
        return chain

    def read_file(self, path: str) -> bytes:
        d, i = self.find(path)[-1]
        off = d["entries"][i][1]
        length, tag, name_len = struct.unpack("<I4si", self._read(off, 12))
        data_off = off + 44 + name_len * self.cw
        return self._read(data_off, off + length - data_off)

    def write_file(self, path: str, content: bytes) -> list[tuple[dict, int]]:
        chain = self.find(path)
        d, i = chain[-1]
        old_off = d["entries"][i][1]
        old_len, tag, name_len = struct.unpack("<I4si", self._read(old_off, 12))
        name_raw = self._read(old_off + 44, name_len * self.cw)  # includes terminator
        rec_len = 44 + len(name_raw) + len(content)
        rec = struct.pack("<I4si", rec_len, TAG_FILE, name_len) + hashlib.sha256(content).digest() + name_raw
        # 1) write the new record into a free slot (best fit) or at the end of the file
        new_off = self._alloc(rec_len)
        self.f.seek(new_off)
        self.f.write(rec)
        self.f.write(content)
        self.f.flush()
        # 2) point the parent entry to it
        self.f.seek(d["ent_off"] + 12 * i + 4)
        self.f.write(struct.pack("<q", new_off))
        self.f.flush()
        # 3) turn the old record into a FreeRecord at the head of the free list
        self.f.seek(old_off)
        self.f.write(struct.pack("<I4sq", old_len, TAG_FREE, self.first_free))
        self.first_free = old_off
        self.f.seek(20)
        self.f.write(struct.pack("<q", old_off))
        self.f.flush()
        return chain

    def _alloc(self, need: int) -> int:
        """Take a FreeRecord of exactly `need` bytes or >= need+16 (rest stays free); else EOF."""
        best = None  # (len, prev_ptr_pos, off, next)
        prev_ptr, off, seen = 20, self.first_free, 0
        while off and seen < 1_000_000:
            length, tag, nxt = struct.unpack("<I4sq", self._read(off, 16))
            if tag != TAG_FREE:
                break  # broken list: don't touch it
            if (length == need or length >= need + 16) and (best is None or length < best[0]):
                best = (length, prev_ptr, off, nxt)
            prev_ptr, off, seen = off + 8, nxt, seen + 1
        if best is None:
            self.f.seek(0, os.SEEK_END)
            return self.f.tell()
        length, prev_ptr, off, nxt = best
        if length == need:
            link = nxt
        else:  # split: remainder becomes a FreeRecord in the same list position
            link = off + need
            self.f.seek(link)
            self.f.write(struct.pack("<I4sq", length - need, TAG_FREE, nxt))
        self.f.seek(prev_ptr)
        self.f.write(struct.pack("<q", link))
        if prev_ptr == 20:
            self.first_free = link
        self.f.flush()
        return off

    def renew_hashes(self, paths: list[str]) -> None:
        """Recompute directory hashes bottom-up, but keep Root and its direct children unchanged
        (same rule as LibGGPK3, otherwise the launcher re-downloads everything)."""
        dirs: dict[int, int] = {}  # dir offset -> depth
        for p in paths:
            for depth, (d, _i) in enumerate(self.find(p)):
                dirs[d["off"]] = depth
        for off, depth in sorted(dirs.items(), key=lambda x: -x[1]):
            if depth < 2:
                continue
            d = self.read_dir(off)
            h = hashlib.sha256(b"".join(self.node_hash(o) for _h, o in d["entries"])).digest()
            self.f.seek(off + 16)
            self.f.write(h)
        self.f.flush()

    def close(self):
        self.f.close()


# --------------------------------------------------------------------------- storage backends
class GgpkStore:
    def __init__(self, path: Path, writable: bool):
        self.g = Ggpk(path, writable)
        self.written: list[str] = []

    def read(self, rel: str) -> bytes:
        return self.g.read_file("Bundles2/" + rel)

    def write(self, rel: str, data: bytes) -> None:
        self.g.write_file("Bundles2/" + rel, data)
        self.written.append("Bundles2/" + rel)

    def finish(self) -> None:
        if self.written:
            self.g.renew_hashes(self.written)
        self.g.close()


class DirStore:
    def __init__(self, bundles2: Path):
        self.root = bundles2

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def write(self, rel: str, data: bytes) -> None:
        p = self.root / rel
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def finish(self) -> None:
        pass



# ----------------------------------------------------------------------------- index
def parse_index(raw: bytes):
    """Returns bundles [(name, size_field_pos, size)], files {hash: (rec_pos, bundle, offset, size)},
    dirs {hash: (offset, size)}, path-data bundle bytes."""
    pos = 0
    (nb,) = struct.unpack_from("<i", raw, pos); pos += 4
    bundles = []
    for _ in range(nb):
        (n,) = struct.unpack_from("<i", raw, pos); pos += 4
        name = raw[pos:pos + n].decode("utf-8"); pos += n
        bundles.append((name, pos, struct.unpack_from("<i", raw, pos)[0])); pos += 4
    (nf,) = struct.unpack_from("<i", raw, pos); pos += 4
    files = {}
    for i, (h, bi, off, size) in enumerate(struct.iter_unpack("<Qiii", memoryview(raw)[pos:pos + 20 * nf])):
        files[h] = (pos + 20 * i, bi, off, size)
    pos += 20 * nf
    (nd,) = struct.unpack_from("<i", raw, pos); pos += 4
    dirs = {h: (off, size) for h, off, size, _rec in struct.iter_unpack("<Qiii", memoryview(raw)[pos:pos + 20 * nd])}
    pos += 20 * nd
    return bundles, files, dirs, raw[pos:]


def decode_paths(data: bytes) -> list[str]:
    """Path representation used by the index (same algorithm as LibBundle3)."""
    out, temp, base, pos, n = [], [], False, 0, len(data)
    while pos + 4 <= n:
        (idx,) = struct.unpack_from("<i", data, pos); pos += 4
        if idx == 0:
            base = not base
            if base:
                temp = []
            continue
        end = data.index(b"\0", pos)
        s = data[pos:end].decode("utf-8", "replace"); pos = end + 1
        idx -= 1
        if idx < len(temp):
            s = temp[idx] + s
        (temp if base else out).append(s)
    return out


def list_dir_files(dirs: dict, path_bundle: bytes, directory: str, suffix: str) -> list[str]:
    data = Bundle(path_bundle).read_all()
    rec = dirs.get(murmur64a(directory.lower().encode()))
    prefix = directory.lower().rstrip("/") + "/"
    if rec:
        off, size = rec
        hits = [p for p in decode_paths(data[off:off + size]) if p.lower().startswith(prefix) and p.lower().endswith(suffix)]
        if hits:
            return hits
    # fallback: walk every directory record
    hits = []
    for off, size in dirs.values():
        hits += [p for p in decode_paths(data[off:off + size]) if p.lower().startswith(prefix) and p.lower().endswith(suffix)]
    return sorted(set(hits))


# ----------------------------------------------------------------------------- backup manifest
class Backup:
    """Original of every file we change + sha256 of what we wrote (to detect game updates)."""

    def __init__(self, game_path: Path):
        self.dir = BACKUP_ROOT / hashlib.sha1(str(game_path).encode()).hexdigest()[:12]
        self.file = self.dir / "manifest.json"
        self.data = json.loads(self.file.read_text()) if self.file.exists() else {"game": str(game_path), "files": {}}

    def _orig(self, path: str) -> Path:
        return self.dir / "files" / path

    def source_for(self, path: str, current: bytes) -> bytes:
        """Original if `current` is our earlier output, else `current` (never patched / game updated)."""
        e = self.data["files"].get(path)
        if e and hashlib.sha256(current).hexdigest() == e["written"] and self._orig(path).exists():
            return self._orig(path).read_bytes()
        return current

    def original_if_ours(self, path: str, current: bytes) -> bytes | None:
        e = self.data["files"].get(path)
        if e and hashlib.sha256(current).hexdigest() == e["written"] and self._orig(path).exists():
            return self._orig(path).read_bytes()
        return None

    def record(self, path: str, original: bytes, written: bytes) -> None:
        p = self._orig(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(original)
        self.data["files"][path] = {"written": hashlib.sha256(written).hexdigest()}

    def forget(self, path: str) -> None:
        self.data["files"].pop(path, None)
        try:
            self._orig(path).unlink()
        except FileNotFoundError:
            pass

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.data, indent=1))

    def patched(self) -> list[str]:
        return sorted(self.data["files"])


def legacy_minimap_original(game_path: Path, p: str, current: bytes) -> bytes | None:
    """If the file was patched earlier by poe_minimap.py, return the original it backed up."""
    if p not in MINIMAP_PATCHES:
        return None
    bak = LEGACY_MINIMAP_ROOT / hashlib.sha1(str(game_path).encode()).hexdigest()[:12] / p
    if not bak.exists():
        return None
    already = MINIMAP_PATCHES[p](current)
    if already is None or already == current:   # current is a patched shader
        return bak.read_bytes()
    return None


# ----------------------------------------------------------------------------- main entry
def game_running() -> bool:
    for p in Path("/proc").glob("[0-9]*/comm"):
        try:
            if p.read_text().strip().lower().startswith("pathofexile"):
                return True
        except OSError:
            pass
    return False


def open_store(path: Path, writable: bool):
    if path.suffix.lower() == ".ggpk":
        return GgpkStore(path, writable)
    if path.name.lower() == "_.index.bin":
        return DirStore(path.parent)
    raise ValueError("Cần đường dẫn tới Content.ggpk (bản standalone) hoặc Bundles2/_.index.bin (Steam/Epic)")


def run(game_path: str, *, minimap: bool, altar: bool, altar_choices: dict[str, str | None],
        group_colors: dict[str, str], dry_run: bool = False, restore_all: bool = False,
        log: Callable[[str], None] = print) -> dict:
    """Bring the game files to the wanted state. Each file always starts from its original, so turning a
    patch off restores that file, and changing colors does not stack."""
    if ooz is None:
        raise RuntimeError("Thiếu thư viện pyooz: chạy  pipx inject poe-wealth pyooz  rồi bật lại app")
    path = Path(game_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Không thấy file: {path}")
    if game_running() and not dry_run:
        raise RuntimeError("Game đang chạy – tắt game trước khi patch.")

    backup = Backup(path.resolve())
    store = open_store(path, writable=not dry_run)
    summary = {"changed": [], "unchanged": 0, "altar_blocks": 0, "dry_run": dry_run}
    try:
        log("Đọc _.index.bin …")
        index_bundle = Bundle(store.read("_.index.bin"))
        raw = index_bundle.read_all()
        bundles, files, dirs, path_bundle = parse_index(raw)
        if murmur64a(b"") not in dirs:
            raise RuntimeError("Phiên bản index không hỗ trợ (cần PoE ≥ 3.21.2 hoặc PoE2).")

        # wanted transform per path (None = back to original)
        wanted: dict[str, Callable[[bytes], bytes | None] | None] = {}
        for p in MINIMAP_FILES:
            wanted[p] = None if (restore_all or not minimap) else MINIMAP_PATCHES[p]
        counter: dict = {}
        if altar and not restore_all:
            log("Tìm file stat description (.csd) …")
            colors = {k: (_markup(group_colors.get(g, GROUP_COLORS.get(g, "#ffffff"))) if g else None)
                      for k, g in altar_choices.items()}
            tf = make_csd_transform(colors, counter)
            csd = list_dir_files(dirs, path_bundle, STATDESC_DIR, ".csd")
            log(f"  {len(csd)} file .csd")
            for p in csd:
                wanted[p] = tf
        for p in backup.patched():          # anything we patched before and is now off -> original
            wanted.setdefault(p, None)

        # locate + compute new content, grouped by bundle
        by_bundle: dict[int, list[str]] = {}
        for p in wanted:
            rec = files.get(murmur64a(p.lower().encode()))
            if rec is None:
                log(f"  ! không có trong index: {p}")
                continue
            by_bundle.setdefault(rec[1], []).append(p)

        new_raw = bytearray(raw)
        writes: list[tuple[str, bytes]] = []
        for bi, paths in by_bundle.items():
            bname, size_pos, _ = bundles[bi]
            rel = bname + ".bundle.bin"
            b = None
            changed: list[tuple[str, bytes]] = []
            for p in paths:
                rec_pos, _bi, off, size = files[murmur64a(p.lower().encode())]
                if b is None:
                    b = Bundle(store.read(rel))
                current = b.read(off, size)
                tf = wanted[p]
                legacy = None if p in backup.data["files"] else legacy_minimap_original(path.resolve(), p, current)
                if legacy is not None:
                    log(f"  {p}: dùng bản gốc từ backup của poe_minimap.py")
                if tf is None:
                    orig = backup.original_if_ours(p, current) or legacy
                    if orig is None:
                        if p in backup.data["files"]:
                            log(f"  {p}: game đã update file này, bỏ backup cũ")
                            if not dry_run:
                                backup.forget(p)
                        continue
                    new, source = orig, orig
                else:
                    source = legacy if legacy is not None else backup.source_for(p, current)
                    new = tf(source)
                    if new is None:
                        new = source
                if new == current:
                    summary["unchanged"] += 1
                    continue
                if not dry_run:
                    if tf is None:
                        backup.forget(p)
                    else:
                        backup.record(p, source, new)
                changed.append((p, new))
                summary["changed"].append(p)
                log(f"  {p}: {'khôi phục bản gốc' if tf is None else 'patch'} ({size} → {len(new)} bytes) [{rel}]")
            if not changed:
                continue
            offsets = b.append([d for _p, d in changed])
            for (p, d), off in zip(changed, offsets):
                struct.pack_into("<ii", new_raw, files[murmur64a(p.lower().encode())][0] + 12, off, len(d))
            struct.pack_into("<i", new_raw, size_pos, b.unc)
            writes.append((rel, b.serialize()))

        summary["altar_blocks"] = counter.get("blocks", 0)
        if altar and not restore_all:
            on = sum(1 for g in altar_choices.values() if g)
            log(f"Dòng altar được tô màu tìm thấy trong game: {summary['altar_blocks']} (đang bật {on} loại; "
                "một stat có thể xuất hiện ở nhiều file, stat game không còn dùng sẽ không thấy)")
        if not writes:
            log("Không có gì cần thay đổi.")
            return summary
        if dry_run:
            log(f"Dry run: sẽ đổi {len(summary['changed'])} file, không ghi gì.")
            return summary
        index_bundle.replace_raw(raw, bytes(new_raw))
        for rel, data in writes:            # bundles first, index last
            log(f"Ghi {rel} ({len(data) / 1e6:.1f} MB)")
            store.write(rel, data)
        store.write("_.index.bin", index_bundle.serialize())
        backup.save()
        log(f"Xong. Backup bản gốc ở {backup.dir}")
        return summary
    finally:
        store.finish()


def status(game_path: str) -> dict:
    if not game_path:
        return {"patched": [], "backup_dir": None}
    b = Backup(Path(game_path).expanduser().resolve())
    return {"patched": b.patched(), "backup_dir": str(b.dir)}
