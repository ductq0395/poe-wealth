"""League challenge tracker.

Source: the account profile page https://www.pathofexile.com/account/view-profile/<name-1234>/challenges
(HTML, sent with POESESSID). Cloudflare often blocks scripted requests to profile pages, so the user can
also paste the page source (Ctrl+U → Ctrl+A → Ctrl+C) and we parse the same HTML.

The HTML parser is tolerant: every element whose class contains "achievement" is one challenge;
"incomplete" in its class = not done; first h2/h3 = title; li items = steps ("finished"/"complete"
in the li class = step done); "x/y" in the block = progress. Notes, stars and reward milestones are
kept locally and survive re-syncs.
"""
from __future__ import annotations

import json
import re
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .config import USER_AGENT

DEFAULT_MILESTONES = [12, 24, 32, 34, 36]   # 3.30 rewards: boots, gloves, body, helmet, back attachment
_PROG = re.compile(r"(\d+)\s*/\s*(\d+)")


def slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


class _Node:
    __slots__ = ("tag", "cls", "children", "text", "parent")

    def __init__(self, tag, cls, parent):
        self.tag, self.cls, self.children, self.text, self.parent = tag, cls, [], [], parent

    def all_text(self) -> str:
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else c.all_text())
        return " ".join(x for x in (s.strip() for s in out) if x)

    def find_all(self, pred):
        for c in self.children:
            if isinstance(c, _Node):
                if pred(c):
                    yield c
                yield from c.find_all(pred)


_VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "col", "embed", "param", "track"}


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", "", None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = tag
        n = _Node(tag, dict(attrs).get("class") or "", self.cur)
        self.cur.children.append(n)
        if tag not in _VOID:
            self.cur = n

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        if self.cur.tag in ("script", "style"):
            return
        if data.strip():
            self.cur.children.append(data)


def parse_html(html: str) -> list[dict]:
    t = _Tree()
    t.feed(html)
    blocks = list(t.root.find_all(lambda n: "achievement" in (n.cls or "").split()))
    # keep the outermost achievement blocks only
    outer = [b for b in blocks if not _has_ancestor(b, blocks)]
    out = []
    for b in outer:
        heads = list(b.find_all(lambda n: n.tag in ("h2", "h3")))
        title = heads[0].all_text() if heads else ""
        if not title:
            continue
        done = "incomplete" not in (b.cls or "").split()
        steps = []
        for li in b.find_all(lambda n: n.tag == "li"):
            txt = li.all_text()
            if txt:
                cls = (li.cls or "").lower()
                steps.append({"text": txt, "done": ("finished" in cls or "complete" in cls) and "incomplete" not in cls})
        desc_nodes = list(b.find_all(lambda n: "text" in (n.cls or "").split() or "detail" in (n.cls or "")))
        desc = desc_nodes[0].all_text() if desc_nodes else ""
        if steps and desc:
            for s in steps:          # detail usually contains the step list too – drop it from the description
                desc = desc.replace(s["text"], "")
        prog = None
        for h in heads[1:]:
            m = _PROG.search(h.all_text())
            if m:
                prog = [int(m.group(1)), int(m.group(2))]
        if not prog and steps:
            prog = [sum(s["done"] for s in steps), len(steps)]
        out.append({"id": slug(title), "title": unescape(title), "desc": re.sub(r"\s+", " ", desc).strip(),
                    "done": done, "steps": steps, "progress": prog})
    return out


def _has_ancestor(n, blocks) -> bool:
    p = n.parent
    s = set(map(id, blocks))
    while p is not None:
        if id(p) in s:
            return True
        p = p.parent
    return False


def profile_url(account: str) -> str:
    return f"https://www.pathofexile.com/account/view-profile/{account.replace('#', '-')}/challenges"


async def fetch(account: str, poesessid: str) -> str:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                                 cookies={"POESESSID": poesessid} if poesessid else {}, timeout=30,
                                 follow_redirects=True) as c:
        r = await c.get(profile_url(account))
    if r.status_code == 403 or "Just a moment" in r.text[:3000]:
        raise RuntimeError("Cloudflare chặn tải tự động – mở trang trong trình duyệt và dán nguồn trang (Ctrl+U → Ctrl+A → Ctrl+C)")
    if r.status_code != 200:
        raise RuntimeError(f"pathofexile.com trả HTTP {r.status_code}")
    return r.text


# ---------------- storage ----------------
def load(path: Path, league: str) -> dict:
    try:
        d = json.loads(path.read_text()).get(league) or {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("items", [])
    d.setdefault("notes", {})
    d.setdefault("stars", [])
    d.setdefault("milestones", DEFAULT_MILESTONES)
    d.setdefault("manual", {})          # id -> True/False override of done (tick by hand)
    d.setdefault("step_manual", {})     # "id|normalised step" -> True/False
    return d


def save(path: Path, league: str, data: dict) -> None:
    allx = {}
    try:
        allx = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    allx[league] = data
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(allx, ensure_ascii=False))
    tmp.replace(path)


def merge_sync(data: dict, items: list[dict], source: str) -> dict:
    if not items:
        raise ValueError("Không đọc được challenge nào – dán đúng nguồn trang challenge (có chữ 'achievement')?")
    data["items"] = items
    data["synced"] = time.time()
    data["source"] = source
    # a server-side "done" wins over an old manual tick
    for it in items:
        if it["done"]:
            data["manual"].pop(it["id"], None)
    return data


def view(data: dict, guide: list[dict] | None = None) -> dict:
    items = []
    src = data["items"] or items_from_guide(guide or [])
    for it in src:
        done = data["manual"].get(it["id"], it["done"])
        steps = []
        for st in it.get("steps", []):
            k = f'{it["id"]}|{_norm(st["text"])}'
            steps.append({**st, "done": data["step_manual"].get(k, st["done"]), "manual": k in data["step_manual"]})
        if it.get("from_guide") and steps:
            # no profile data: progress from the steps ticked by hand ("5 of 8 required" / "All required")
            m = re.match(r"(\d+) of (\d+)", it.get("req") or "")
            need = int(m.group(1)) if m else len(steps)
            got = sum(1 for x in steps if x["done"])
            it = {**it, "progress": [min(got, need), need]}
            if it["id"] not in data["manual"]:
                done = got >= need
        items.append({**it, "steps": steps, "done": done, "manual": it["id"] in data["manual"],
                      "note": data["notes"].get(it["id"], ""), "star": it["id"] in data["stars"]})
    n_done = sum(i["done"] for i in items)
    ms = sorted(int(x) for x in data["milestones"] if int(x) > 0)
    nxt = next((m for m in ms if m > n_done), None)
    v = {"items": items, "done": n_done, "total": len(items), "milestones": ms, "next": nxt,
         "need": (nxt - n_done) if nxt else 0, "synced": data.get("synced"), "source": data.get("source")}
    if guide:
        by = {g["id"]: g for g in guide}
        by_norm = {_norm(g["title"]): g for g in guide}
        for it in v["items"]:
            g = by.get(it["id"]) or by_norm.get(_norm(it["title"]))
            if not g:
                continue
            it["guide"] = g
            tips = {_norm(s["text"]): s["tips"] for s in g["steps"]}
            for s in it["steps"]:
                s["tips"] = tips.get(_norm(s["text"]), [])
    return v


# ---------------- community guide (CraniumViolence, CC BY-NC-SA 4.0) ----------------
GUIDE_SITE = "https://craniumviolence.github.io/"
GUIDE_URL = GUIDE_SITE + "league.txt"     # markdown-ish source the guide page renders with showdown
GUIDE_CACHE_HOURS = 12
_PRICE = re.compile(r"([brpm])\{([^}]+)\}((?:\{[^}]+\})*)")
_TAG = re.compile(r"<[^>]+>")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def _clean_line(t: str, price_of) -> str:
    def rep(m):
        kind, name, extra = m.group(1), m.group(2), m.group(3)
        p = price_of(name)
        mult = 1
        fm = re.match(r"\{x(\d+)\}", extra or "")
        if kind == "m" and fm:
            mult = int(fm.group(1))
        ps = f" (≈ {_fmt_c(p * mult)})" if p else ""
        return (f"{mult}× {name}" if mult > 1 else name) + ps
    t = _PRICE.sub(rep, t)
    t = re.sub(r"\[\.[a-z0-9 ]+\]", "", t)                 # [.ggg] [.com] class markers
    t = _TAG.sub("", t)
    return unescape(t).strip()


_DIV = {"rate": 0.0}


def _fmt_c(c: float) -> str:
    d = _DIV["rate"]
    if d and c >= d:
        return f"{c / d:.1f} div"
    return f"{c:.0f}c" if c >= 10 else f"{c:.1f}c"


def parse_guide(txt: str, price_of=lambda n: None, divine: float = 0.0) -> list[dict]:
    _DIV["rate"] = divine
    out = []
    parts = re.split(r"^# (\d+) (.+)$", txt, flags=re.M)
    # parts = [intro, num, title, body, num, title, body, ...]
    for i in range(1, len(parts) - 2, 3):
        num, title, body = int(parts[i]), parts[i + 1].strip(), parts[i + 2]
        body = body.split("</div>")[0]
        g = {"num": num, "title": title, "id": slug(title), "difficulty": "", "tags": [], "req": "", "desc": "",
             "steps": [], "notes": [], "anchor": GUIDE_SITE + "#" + _norm(f"{num}{title}")}
        cur_step = None
        for raw in body.split("\n"):
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("<div"):
                continue
            if line.startswith("## "):
                g["difficulty"] = line[3:].strip()
                continue
            if line.startswith("### "):
                g["tags"].append(line[4:].strip())
                continue
            m = re.search(r'<h2 class="req">(.*?)</h2>', line)
            if m:
                g["req"] = m.group(1).strip()
                continue
            depth = len(line) - len(line.lstrip("\t"))
            s = line.strip()
            if s.startswith("- [.ggg]") and depth == 0:
                cur_step = {"text": _clean_line(s[2:], price_of), "tips": []}
                g["steps"].append(cur_step)
                continue
            if s.startswith("[.ggg]") and not g["desc"]:
                g["desc"] = _clean_line(s, price_of)
                continue
            if s.startswith("- ") and depth >= 1 and cur_step is not None:
                cur_step["tips"].append({"depth": depth, "text": _clean_line(s[2:], price_of)})
                continue
            if depth == 0:
                cur_step = None
            txt_ = _clean_line(s[2:] if s.startswith("- ") else s, price_of)
            if txt_:
                g["notes"].append("  " * depth + ("• " if s.startswith("- ") else "") + txt_)
        out.append(g)
    return out


async def fetch_guide(cache: Path) -> str:
    try:
        d = json.loads(cache.read_text())
        if time.time() - d.get("ts", 0) < GUIDE_CACHE_HOURS * 3600 and d.get("txt"):
            return d["txt"]
    except (OSError, ValueError):
        d = {}
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
            r = await c.get(GUIDE_URL)
            r.raise_for_status()
            txt = r.text
        cache.write_text(json.dumps({"ts": time.time(), "txt": txt}, ensure_ascii=False))
        return txt
    except Exception:  # noqa: BLE001 – offline: use an old copy if we have one
        if d.get("txt"):
            return d["txt"]
        raise


def items_from_guide(guide: list[dict]) -> list[dict]:
    return [{"id": g["id"], "title": g["title"], "desc": g["desc"], "done": False, "req": g["req"],
             "steps": [{"text": s["text"], "done": False} for s in g["steps"]], "progress": None,
             "from_guide": True} for g in guide]
