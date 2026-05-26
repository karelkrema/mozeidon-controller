#!/usr/bin/env python3
"""
mozeidon-picker — cross-platform Firefox tab picker via mozeidon CLI

Commands:
  overlay          show interactive picker overlay (default)
  reuse <name>     activate named registry entry (switch or open tab)
  reorder          reorder Firefox tabs by registry defaultPosition.order

Requirements: python3 with tkinter  (apt install python3-tk on Debian/Ubuntu)
"""

import json
import os
import platform
import random
import re
import subprocess
import sys
import atexit
import signal
import threading
import time
from pathlib import Path

# ─── Platform & config ───────────────────────────────────────────────────────

IS_MACOS = platform.system() == "Darwin"

def _find_mozeidon() -> str:
    for p in (
        "/home/linuxbrew/.linuxbrew/bin/mozeidon",
        "/opt/homebrew/bin/mozeidon",
        "/usr/local/bin/mozeidon",
        "/usr/bin/mozeidon",
    ):
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which("mozeidon") or "mozeidon"

MOZEIDON = _find_mozeidon()

def _find_registry() -> Path:
    # Allow override via env var
    env = os.environ.get("MOZEIDON_REGISTRY")
    if env:
        return Path(env)
    # Script-local .hammerspoon (cloned repo layout)
    local = Path(__file__).parent / ".hammerspoon" / "mozeidon-registry.json"
    if local.exists():
        return local
    # macOS Hammerspoon default
    return Path.home() / ".hammerspoon" / "mozeidon-registry.json"

REGISTRY_PATH = _find_registry()
MAX_STARS     = 5
PIN_THRESHOLD = 100
ORDER_DEFAULT = 1_000_000_000
ORDER_OUTSIDE = 1_000_000_001
NAME_COL      = 38
URL_COL       = 80

# ─── Registry ────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except Exception:
        data = {}
    data.setdefault("version", 1)
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def save_registry(registry: dict) -> bool:
    registry["entries"].sort(
        key=lambda e: (e.get("defaultPosition") or {}).get("order", ORDER_DEFAULT)
    )
    try:
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
        return True
    except Exception as ex:
        print(f"registry save error: {ex}", file=sys.stderr)
        return False


def find_entry_by_name(registry: dict, name: str):
    for e in registry["entries"]:
        if e.get("name") == name:
            return e
    return None


def find_entry_by_url(registry: dict, url: str):
    for e in registry["entries"]:
        if e.get("url") == url:
            return e
    return None


def _matcher(entry: dict) -> dict:
    return entry.get("matcher") or {"type": "exact", "value": entry.get("url", "")}


_LUA_CLASSES = {
    'd': r'[0-9]',   'D': r'[^0-9]',
    'a': r'[A-Za-z]','A': r'[^A-Za-z]',
    'l': r'[a-z]',   'L': r'[^a-z]',
    'u': r'[A-Z]',   'U': r'[^A-Z]',
    'w': r'[A-Za-z0-9]', 'W': r'[^A-Za-z0-9]',
    's': r'[ \t\n\r\f\v]',
    'x': r'[0-9A-Fa-f]',
}

def _lua_to_re(pat: str) -> str:
    """Convert Lua string.find pattern (%-escapes) to Python regex."""
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == '%' and i + 1 < len(pat):
            nc = pat[i + 1]
            if nc in _LUA_CLASSES:
                out.append(_LUA_CLASSES[nc])
            elif nc == '%':
                out.append('%')
            else:
                out.append(re.escape(nc))  # %( → \(, %. → \., etc.
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def tab_matches(url: str, matcher: dict) -> bool:
    t, v = matcher.get("type", "exact"), matcher.get("value", "")
    if t == "exact":
        return url == v
    if t == "fragment":
        return v in url
    if t == "regex":
        try:
            return bool(re.search(_lua_to_re(v), url))
        except re.error:
            return False
    return False


def find_entry_for_url(registry: dict, url: str):
    for e in registry["entries"]:
        if tab_matches(url, _matcher(e)):
            return e
    return None


def find_tab_for_entry(tabs: list, entry: dict):
    m = _matcher(entry)
    for t in tabs:
        if tab_matches(t.get("url", ""), m):
            return t
    return None


def gen_id() -> str:
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))


def _dp(entry) -> dict:
    return (entry or {}).get("defaultPosition") or {}

# ─── mozeidon CLI ─────────────────────────────────────────────────────────────

def _run(args: list) -> tuple:
    try:
        r = subprocess.run([MOZEIDON] + args, capture_output=True, text=True, timeout=10)
        return r.stdout, r.returncode, r.stderr
    except Exception as ex:
        return "", -1, str(ex)


def _run_seq(cmds: list):
    for cmd in cmds:
        out, code, err = _run(cmd)
        if code != 0:
            print(f"mozeidon {' '.join(cmd)}: exit {code}: {err}", file=sys.stderr)


def fetch_tabs() -> list:
    out, code, _ = _run(["tabs", "get"])
    if code != 0 or not out:
        return []
    try:
        return json.loads(out).get("data", [])
    except Exception:
        return []


def fetch_bookmarks() -> list:
    out, code, _ = _run(["bookmarks"])
    if code != 0 or not out:
        return []
    try:
        return json.loads(out).get("data", [])
    except Exception:
        return []


def focus_firefox():
    if IS_MACOS:
        subprocess.Popen(["osascript", "-e", 'tell application "Firefox" to activate'])
        return
    for cmd in (["wmctrl", "-a", "Firefox"],):
        try:
            subprocess.Popen(cmd)
            return
        except FileNotFoundError:
            pass
    try:
        r = subprocess.run(["xdotool", "search", "--name", "Firefox"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            wids = r.stdout.strip().split()
            if wids:
                subprocess.Popen(["xdotool", "windowactivate", wids[-1]])
    except FileNotFoundError:
        pass


def switch_tab(window_id, tab_id) -> bool:
    _, code, err = _run(["tabs", "switch", f"{window_id}:{tab_id}"])
    if code != 0:
        print(f"tab switch failed: {err}", file=sys.stderr)
        return False
    focus_firefox()
    return True


def open_tab(url: str) -> bool:
    _, code, err = _run(["tabs", "new", url])
    if code != 0:
        print(f"tabs new failed: {err}", file=sys.stderr)
        return False
    focus_firefox()
    return True


def activate_entry(entry: dict):
    tabs = fetch_tabs()
    tab  = find_tab_for_entry(tabs, entry)
    if tab:
        switch_tab(tab.get("windowId"), tab.get("id"))
    else:
        open_tab(entry.get("url", ""))

# ─── Search ───────────────────────────────────────────────────────────────────

def compute_results(query: str, tabs: list, bookmarks: list, registry: dict) -> list:
    tokens  = query.lower().split()
    by_url  = {e.get("url"): e for e in registry["entries"]}

    def hits(text: str) -> bool:
        lo = text.lower()
        return all(t in lo for t in tokens)

    results = []

    for t in tabs:
        url = t.get("url", "")
        if hits(t.get("title", "") + " " + url):
            e  = by_url.get(url)
            dp = _dp(e)
            results.append(dict(
                kind="tab", title=t.get("title", ""), url=url,
                stars=(e or {}).get("stars", 0), name=(e or {}).get("name", ""),
                order=dp.get("order"), uniqueness=dp.get("uniqueness"),
                keepOpen=dp.get("keepOpen"),
                tabWindowId=t.get("windowId"), tabId=t.get("id"),
            ))

    for bm in bookmarks:
        url = bm.get("url", "")
        if not url:
            continue
        if hits(bm.get("title", "") + " " + url):
            e  = by_url.get(url)
            dp = _dp(e)
            results.append(dict(
                kind="bookmark", title=bm.get("title", ""), url=url,
                stars=(e or {}).get("stars", 0), name=(e or {}).get("name", ""),
                order=dp.get("order"), uniqueness=dp.get("uniqueness"),
                keepOpen=dp.get("keepOpen"),
            ))

    seen = {r["url"] for r in results}
    for e in registry["entries"]:
        url = e.get("url", "")
        if url in seen:
            continue
        if hits(e.get("name", "") + " " + url):
            dp = _dp(e)
            results.append(dict(
                kind="entry", title=e.get("name", ""), url=url,
                stars=e.get("stars", 0), name=e.get("name", ""),
                order=dp.get("order"), uniqueness=dp.get("uniqueness"),
                keepOpen=dp.get("keepOpen"),
            ))

    results.sort(key=lambda r: (
        -r["stars"],
        {"tab": 1, "bookmark": 2, "entry": 3}.get(r["kind"], 9)
    ))
    return results

# ─── Reorder ─────────────────────────────────────────────────────────────────

def _classify(tab: dict, registry: dict) -> tuple:
    e = find_entry_for_url(registry, tab.get("url", ""))
    if not e:
        return ORDER_OUTSIDE, "none", f"no_match::{tab.get('id')}", None
    dp = _dp(e)
    return dp.get("order", ORDER_DEFAULT), dp.get("uniqueness", "none"), f"entry::{e.get('id')}", e


def _reorder_window(wid, tabs: list, registry: dict):
    ann = []
    for t in tabs:
        order, uniq, key, entry = _classify(t, registry)
        ann.append(dict(tab=t, order=order, uniq=uniq, key=key, entry=entry,
                        orig=t.get("index", 0)))

    buckets: dict = {}
    for a in ann:
        buckets.setdefault(a["key"], []).append(a)

    # close strict duplicates (keep leftmost by original index)
    closed: set = set()
    close_cmds = []
    for bucket in buckets.values():
        if len(bucket) > 1 and bucket[0]["uniq"] == "strict":
            bucket.sort(key=lambda x: x["orig"])
            for a in bucket[1:]:
                closed.add(a["tab"]["id"])
                close_cmds.append(["tabs", "close", f"{wid}:{a['tab']['id']}"])
    _run_seq(close_cmds)

    rem = [a for a in ann if a["tab"]["id"] not in closed]
    rem.sort(key=lambda a: (a["order"], a["orig"]))

    pinned   = [a for a in rem if a["order"] < PIN_THRESHOLD]
    unpinned = [a for a in rem if a["order"] >= PIN_THRESHOLD]

    _run_seq([
        ["tabs", "update", "-t", str(a["tab"]["id"]), "-w", str(wid), "--pin=false"]
        for a in rem if a["tab"].get("pinned")
    ])
    _run_seq([
        ["tabs", "update", "-t", str(a["tab"]["id"]), "-w", str(wid), "--pin=true"]
        for a in pinned
    ])
    _run_seq([
        ["tabs", "update", "-t", str(a["tab"]["id"]), "-w", str(wid),
         "-i", str(len(pinned) + i), "--should-be-ungrouped"]
        for i, a in enumerate(unpinned)
    ])

    # wrap uniqueness=group sets into tab groups
    for bucket in buckets.values():
        if len(bucket) < 2 or bucket[0]["uniq"] != "group":
            continue
        if bucket[0]["order"] < PIN_THRESHOLD:
            continue
        grp = [a for a in bucket if a["tab"]["id"] not in closed]
        grp.sort(key=lambda a: a["orig"])
        if len(grp) < 2:
            continue
        anchor = grp[0]["tab"]
        title  = (grp[0]["entry"] or {}).get("name", "")
        out, code, err = _run([
            "tabs", "init-group",
            "-i", str(anchor["id"]), "-w", str(wid), "-t", title,
        ])
        if code != 0:
            print(f"init-group failed: {err}", file=sys.stderr)
            continue
        s = out.strip()
        gid = None
        if re.match(r"^-?\d+$", s):
            gid = int(s)
        else:
            try:
                p = json.loads(s)
                gid = p.get("groupId") or p.get("id") or (_dp(p).get("groupId"))
            except Exception:
                pass
        if gid is None:
            print(f"init-group: cannot parse group id from {out!r}", file=sys.stderr)
            continue
        _run_seq([
            ["tabs", "update", "-t", str(a["tab"]["id"]), "-w", str(wid), "-g", str(gid)]
            for a in grp[1:]
        ])


def cmd_reorder():
    registry = load_registry()
    tabs = fetch_tabs()

    missing = []
    for e in registry["entries"]:
        if not _dp(e).get("keepOpen"):
            continue
        if not any(tab_matches(t.get("url", ""), _matcher(e)) for t in tabs):
            missing.append(e)

    if missing:
        _run_seq([["tabs", "new", e["url"]] for e in missing])
        time.sleep(1.5)
        tabs = fetch_tabs()

    by_win: dict = {}
    for t in tabs:
        by_win.setdefault(t.get("windowId"), []).append(t)
    for wid, wtabs in by_win.items():
        _reorder_window(wid, wtabs, registry)

    if missing:
        tabs2: dict = {}
        for t in fetch_tabs():
            tabs2.setdefault(t.get("windowId"), []).append(t)
        for wid, wtabs in tabs2.items():
            _reorder_window(wid, wtabs, registry)


def cmd_reuse(name: str):
    registry = load_registry()
    entry = find_entry_by_name(registry, name)
    if not entry:
        print(f"entry not found: {name!r}", file=sys.stderr)
        sys.exit(1)
    activate_entry(entry)

# ─── Overlay UI (tkinter) ─────────────────────────────────────────────────────

COLORS = {
    "bg":    "#0f0f14",
    "text":  "#d9d9d9",
    "bright":"#ffffff",
    "dim":   "#737373",
    "cyan":  "#4dcce6",
    "yellow":"#ffd94d",
    "green": "#66e680",
    "selBg": "#ffd94d",
    "selFg": "#0f0f14",
}


def _active_monitor_geometry(root) -> tuple:
    """Return (x, y, w, h) of the monitor that currently holds the cursor."""
    try:
        cx = root.winfo_pointerx()
        cy = root.winfo_pointery()
        r  = subprocess.run(["xrandr", "--listactivemonitors"],
                            capture_output=True, text=True, timeout=3)
        # Each line: "N: +*NAME WxMM/HxMM+X+Y" e.g. "0: +*eDP-1 1920/340x1080/190+0+0"
        for line in r.stdout.splitlines()[1:]:
            m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", line)
            if not m:
                continue
            mw, mh, mx, my = int(m[1]), int(m[2]), int(m[3]), int(m[4])
            if mx <= cx < mx + mw and my <= cy < my + mh:
                return mx, my, mw, mh
    except Exception:
        pass
    # Fallback: full desktop
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def _trunc(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s.ljust(n)
    return s[: n - 1] + "…"


def _stars(n: int) -> str:
    n = max(0, min(MAX_STARS, n or 0))
    return "★" * n + "·" * (MAX_STARS - n)


def _order_prefix(item: dict) -> str:
    if item.get("order") is None and not item.get("uniqueness") and not item.get("keepOpen"):
        return ""
    u = {"strict": "S", "group": "G"}.get(item.get("uniqueness") or "none", "N")
    o = str(item["order"]) if item.get("order") is not None else "·"
    k = " K" if item.get("keepOpen") else ""
    return f"[{o} {u}{k}] "


class Overlay:
    def __init__(self):
        try:
            import tkinter as tk
            import tkinter.font as tkfont
        except ImportError:
            print("tkinter not available. Install python3-tk.", file=sys.stderr)
            sys.exit(1)

        self._tk     = tk
        self._tkfont = tkfont

        self.root = tk.Tk()
        self.root.withdraw()

        # search state
        self.query     = ""
        self.sel       = 0
        self.scroll    = 0
        self.tabs      = None
        self.bookmarks = None
        self.registry  = load_registry()
        self.results   : list = []

        # order-input mode
        self.mode      = "search"
        self.ord_input = ""
        self.ord_item  = None

        self._setup_window()
        self._setup_fonts()
        self._setup_widgets()
        self.root.bind("<KeyPress>", self._on_key)
        self._render()
        threading.Thread(target=self._bg_fetch, daemon=True).start()

    # ── window ──────────────────────────────────────────────────────────────

    def _setup_window(self):
        root = self.root
        root.title("Mozeidon")
        root.configure(bg=COLORS["bg"])
        # overrideredirect breaks WM focus on Wayland/XWayland — skip it on Linux
        if IS_MACOS:
            root.overrideredirect(True)
            root.attributes("-alpha", 0.97)
        root.attributes("-topmost", True)

        mx, my, mw, mh = _active_monitor_geometry(root)
        w = int(mw * 0.82)
        h = int(mh * 0.82)
        x = mx + (mw - w) // 2
        y = my + (mh - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")
        self._w, self._h = w, h

        root.deiconify()
        root.lift()
        root.focus_force()

    def _setup_fonts(self):
        tkf = self._tkfont
        avail = set(tkf.families())

        mono_chain = ["Menlo", "Monaco", "DejaVu Sans Mono", "Courier New",
                      "Monospace", "Courier"]
        mono = next((f for f in mono_chain if f in avail), "Courier")

        sans_chain = [".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue",
                      "Ubuntu", "Noto Sans", "Arial"]
        sans = next((f for f in sans_chain if f in avail), "Arial")

        self.f_mono  = tkf.Font(family=mono, size=13)
        self.f_small = tkf.Font(family=mono, size=11)
        self.f_title = tkf.Font(family=sans, size=16, weight="bold")

    def _setup_widgets(self):
        tk  = self._tk
        C   = COLORS
        pad = 24
        outer = tk.Frame(self.root, bg=C["bg"], padx=pad, pady=pad)
        outer.pack(fill=tk.BOTH, expand=True)

        self.lbl_title = tk.Label(outer, text="Mozeidon",
                                   bg=C["bg"], fg=C["bright"],
                                   font=self.f_title, anchor="w")
        self.lbl_title.pack(fill=tk.X, pady=(0, 3))

        self.lbl_query = tk.Label(outer, text="",
                                   bg=C["bg"], fg=C["cyan"],
                                   font=self.f_mono, anchor="w")
        self.lbl_query.pack(fill=tk.X, pady=(0, 6))

        self.txt = tk.Text(outer, bg=C["bg"], fg=C["text"],
                           font=self.f_mono, relief=tk.FLAT, bd=0,
                           state=tk.DISABLED, cursor="none",
                           selectbackground=C["bg"], insertwidth=0,
                           wrap=tk.NONE)
        self.txt.pack(fill=tk.BOTH, expand=True)

        self.txt.tag_configure("dim",   foreground=C["dim"])
        self.txt.tag_configure("stars", foreground=C["yellow"])
        self.txt.tag_configure("tab",   foreground=C["cyan"])
        self.txt.tag_configure("bm",    foreground=C["green"])
        self.txt.tag_configure("reg",   foreground=C["dim"])
        self.txt.tag_configure("name",  foreground=C["text"])
        self.txt.tag_configure("url",   foreground=C["dim"])
        self.txt.tag_configure("sel",   background=C["selBg"], foreground=C["selFg"])

        self.lbl_hint = tk.Label(outer, text="",
                                  bg=C["bg"], fg=C["dim"],
                                  font=self.f_small, anchor="w")
        self.lbl_hint.pack(fill=tk.X, pady=(4, 0))

    # ── render ───────────────────────────────────────────────────────────────

    def _render(self):
        self._render_header()
        self._render_list()
        self._render_hint()

    def _render_header(self):
        if self.mode == "order":
            item  = self.ord_item
            label = (item.get("name") or item.get("title") or item.get("url") or "?") if item else "?"
            self.lbl_query.configure(text=f"● order for {label}: {self.ord_input}_",
                                      fg=COLORS["yellow"])
        else:
            self.lbl_query.configure(text=f"▌ {self.query}_", fg=COLORS["cyan"])

    def _render_hint(self):
        if self.mode == "order":
            self.lbl_hint.configure(
                text="digits  ·  Enter save  ·  Backspace  ·  Esc cancel  ·  (0–99 = pinned)")
        else:
            self.lbl_hint.configure(
                text="↑↓ nav  ·  Enter open  ·  ←→ stars  ·  Ctrl+O order  ·  "
                     "Ctrl+M uniqueness  ·  Ctrl+K keepOpen  ·  Ctrl+E edit  ·  Esc")

    def _max_rows(self) -> int:
        lh = self.f_mono.metrics("linespace")
        return max(5, int(self._h * 0.75 / max(1, lh)))

    def _render_list(self):
        tk  = self._tk
        txt = self.txt
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)

        if self.tabs is None or self.bookmarks is None:
            txt.insert(tk.END, "  Loading…\n", "dim")
            txt.configure(state=tk.DISABLED)
            return

        if not self.results:
            txt.insert(tk.END, "  (no matches)\n", "dim")
            txt.configure(state=tk.DISABLED)
            return

        max_vis = self._max_rows()
        start   = self.scroll
        end     = min(start + max_vis, len(self.results))

        for i in range(start, end):
            self._insert_row(self.results[i], i == self.sel)

        rem = len(self.results) - end
        if rem > 0:
            txt.insert(tk.END,
                       f"  … {rem} more  ({self.sel + 1}/{len(self.results)})\n", "dim")

        txt.configure(state=tk.DISABLED)

    def _insert_row(self, item: dict, selected: bool):
        tk     = self._tk
        txt    = self.txt
        prefix = "▶ " if selected else "  "
        st     = _stars(item["stars"])
        kind   = item["kind"]
        badge  = {"tab": "[TAB]", "bookmark": "[BM] ", "entry": "[REG]"}.get(kind, "[???]")
        btag   = {"tab": "tab",   "bookmark": "bm",    "entry": "reg"  }.get(kind, "dim")
        name   = _trunc(item.get("name") or item.get("title") or "", NAME_COL)
        url    = _trunc(_order_prefix(item) + (item.get("url") or ""), URL_COL)

        if selected:
            txt.insert(tk.END, f"{prefix}{st} {badge}  {name}  {url}\n", "sel")
        else:
            txt.insert(tk.END, prefix,                    "dim")
            txt.insert(tk.END, st + " ",                  "stars")
            txt.insert(tk.END, badge,                     btag)
            txt.insert(tk.END, "  " + name,               "name")
            txt.insert(tk.END, "  " + url + "\n",         "url")

    # ── state helpers ────────────────────────────────────────────────────────

    def _clamp(self):
        n       = len(self.results)
        max_vis = self._max_rows()
        if n == 0:
            self.sel = self.scroll = 0
            return
        self.sel = max(0, min(self.sel, n - 1))
        if self.sel < self.scroll:
            self.scroll = self.sel
        elif self.sel >= self.scroll + max_vis:
            self.scroll = self.sel - max_vis + 1
        self.scroll = max(0, self.scroll)

    def _refilter(self, keep_url=None):
        if self.tabs is not None and self.bookmarks is not None:
            self.results = compute_results(self.query, self.tabs,
                                           self.bookmarks, self.registry)
        else:
            self.results = []
        if keep_url:
            for i, r in enumerate(self.results):
                if r["url"] == keep_url:
                    self.sel = i
                    break
        self._clamp()
        self._render()

    def _cur(self):
        if not self.results or not (0 <= self.sel < len(self.results)):
            return None
        return self.results[self.sel]

    def _ensure_entry(self, item: dict) -> dict:
        e = find_entry_by_url(self.registry, item["url"])
        if not e:
            e = {"id": gen_id(), "name": item.get("name", ""), "url": item["url"],
                 "stars": 0, "matcher": {"type": "exact", "value": item["url"]}}
            self.registry["entries"].append(e)
        return e

    # ── actions ──────────────────────────────────────────────────────────────

    def _do_activate(self):
        item = self._cur()
        self._dismiss()
        if not item:
            return
        if item["kind"] == "tab":
            switch_tab(item["tabWindowId"], item["tabId"])
        else:
            reg   = load_registry()
            entry = find_entry_by_url(reg, item["url"]) or {
                "url": item["url"], "matcher": {"type": "exact", "value": item["url"]}
            }
            activate_entry(entry)

    def _do_stars(self, delta: int):
        item = self._cur()
        if not item or not item.get("url"):
            return
        e = self._ensure_entry(item)
        e["stars"] = max(0, min(MAX_STARS, (e.get("stars") or 0) + delta))
        save_registry(self.registry)
        self._refilter(item["url"])

    def _enter_order(self):
        item = self._cur()
        if not item or not item.get("url"):
            return
        e   = find_entry_by_url(self.registry, item["url"])
        cur = _dp(e).get("order")
        self.mode      = "order"
        self.ord_item  = item
        self.ord_input = str(cur) if cur is not None else ""
        self._render()

    def _exit_order(self):
        self.mode = "search"
        self.ord_item = self.ord_input = None
        self.ord_input = ""
        self._render()

    def _confirm_order(self):
        item = self.ord_item
        if not item:
            return self._exit_order()
        try:
            n = int(self.ord_input)
        except (ValueError, TypeError):
            return
        e  = self._ensure_entry(item)
        dp = e.setdefault("defaultPosition", {"uniqueness": "none"})
        dp["order"] = n
        save_registry(self.registry)
        url = item["url"]
        self._exit_order()
        self._refilter(url)

    def _toggle_keep_open(self):
        item = self._cur()
        if not item or not item.get("url"):
            return
        e  = self._ensure_entry(item)
        dp = e.setdefault("defaultPosition", {"uniqueness": "none", "order": ORDER_DEFAULT})
        dp["keepOpen"] = not dp.get("keepOpen", False)
        save_registry(self.registry)
        self._refilter(item["url"])

    def _cycle_uniq(self):
        item = self._cur()
        if not item or not item.get("url"):
            return
        e  = self._ensure_entry(item)
        dp = e.setdefault("defaultPosition", {"order": ORDER_DEFAULT})
        dp["uniqueness"] = {"none": "strict", "strict": "group", "group": "none"}.get(
            dp.get("uniqueness", "none"), "none")
        save_registry(self.registry)
        self._refilter(item["url"])

    def _edit_registry(self):
        self._dismiss()
        if not REGISTRY_PATH.exists():
            save_registry(load_registry())
        path = str(REGISTRY_PATH)
        subprocess.Popen(["open" if IS_MACOS else "xdg-open", path])

    def _dismiss(self):
        self.root.destroy()

    # ── key handler ──────────────────────────────────────────────────────────

    def _on_key(self, event):
        sym  = event.keysym
        char = event.char
        ctrl = bool(event.state & 0x4)

        if self.mode == "order":
            if sym == "Escape":
                self._exit_order()
            elif sym in ("Return", "KP_Enter"):
                self._confirm_order()
            elif sym == "BackSpace":
                self.ord_input = self.ord_input[:-1]
                self._render()
            elif char and char.isdigit() and not ctrl:
                self.ord_input += char
                self._render()
            return "break"

        if sym == "Escape":
            self._dismiss()
        elif sym in ("Return", "KP_Enter"):
            self._do_activate()
        elif sym == "Up":
            self.sel -= 1;  self._clamp();  self._render()
        elif sym == "Down":
            self.sel += 1;  self._clamp();  self._render()
        elif sym == "Left":
            self._do_stars(-1)
        elif sym == "Right":
            self._do_stars(1)
        elif sym == "BackSpace":
            self.query = self.query[:-1];  self._refilter()
        elif ctrl and sym.lower() == "o":
            self._enter_order()
        elif ctrl and sym.lower() == "m":
            self._cycle_uniq()
        elif ctrl and sym.lower() == "k":
            self._toggle_keep_open()
        elif ctrl and sym.lower() == "e":
            self._edit_registry()
        elif not ctrl and char and char.isprintable() and len(char) == 1:
            self.query += char;  self._refilter()

        return "break"

    # ── data loading ─────────────────────────────────────────────────────────

    def _bg_fetch(self):
        bm   = fetch_bookmarks()   # sequential — native messaging can't handle parallel
        tabs = fetch_tabs()
        self.root.after(0, lambda: self._on_data(tabs, bm))

    def _on_data(self, tabs, bm):
        self.tabs      = tabs
        self.bookmarks = bm
        self._refilter()

    def _heartbeat(self):
        """Wake up mainloop every 200 ms so Python can deliver pending signals."""
        self.root.after(200, self._heartbeat)

    def run(self):
        signal.signal(signal.SIGTERM,
                      lambda *_: self.root.after(0, self._dismiss))
        # Periodic no-op so Python checks pending signals between Tcl events
        self._heartbeat()
        self.root.mainloop()


# ─── Single-instance lock ────────────────────────────────────────────────────

LOCK_FILE = Path("/tmp/mozeidon-picker.lock")


def _check_lock() -> bool:
    """Return True if we should launch. False = another instance is alive (and was killed = toggle)."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)          # raises if process is gone
            os.kill(pid, signal.SIGTERM)  # alive → kill it (toggle/dismiss)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass                     # stale lock — proceed
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))
    return True


def cmd_overlay():
    if not _check_lock():
        return          # another instance was running; we killed it → done
    Overlay().run()

# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    argv = sys.argv[1:]
    if not argv or argv[0] == "overlay":
        cmd_overlay()
    elif argv[0] == "reuse":
        if len(argv) < 2:
            print("usage: picker.py reuse <name>", file=sys.stderr)
            sys.exit(1)
        cmd_reuse(argv[1])
    elif argv[0] == "reorder":
        cmd_reorder()
    else:
        print("usage: picker.py [overlay | reuse <name> | reorder]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
