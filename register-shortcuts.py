#!/usr/bin/env python3
"""
Zaregistruje GNOME custom keyboard shortcuts pro mozeidon-picker.
Idempotentní — bezpečné spustit opakovaně (stávající shortcuty se jen aktualizují).

Mapování vychází z kanata.kbd:
  RCtrl (held) → hyper vrstva → posílá Super+Alt+Ctrl+klávesa
  Výjimka: čísla posílají jen Super+číslo (M-1, M-2, ...)

Použití:
  python3 register-shortcuts.py           # zaregistruje vše
  python3 register-shortcuts.py --list    # zobrazí stávající custom shortcuts
  python3 register-shortcuts.py --remove  # odstraní mozeidon shortcuts
"""

import ast
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PICKER     = f"python3 {SCRIPT_DIR}/picker.py"

SCHEMA    = "org.gnome.settings-daemon.plugins.media-keys"
CK_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
BASE_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"

# Ekvivalent hypers.lua zkratek — zkontrolujte s hypers.lua a registrací v rejestu
SHORTCUTS = [
    # (jméno, binding, příkaz)
    # hyper+space → overlay
    ("Mozeidon Overlay",
     "<Super><Alt><Ctrl>space",
     f"{PICKER} overlay"),

    # hyper+shift+space → reorder
    ("Mozeidon Reorder",
     "<Shift><Super><Alt><Ctrl>space",
     f"{PICKER} reorder"),

    # hyper+F1..F4 → reuse named entries (kanata: multi lmet lalt lctl f1..f4)
    ("Mozeidon F1 JXL Roadmap",
     "<Super><Alt><Ctrl>F1",
     f'{PICKER} reuse "JXL Roadmap"'),

    ("Mozeidon F2 JXL Backlog",
     "<Super><Alt><Ctrl>F2",
     f'{PICKER} reuse "JXL Backlog"'),

    ("Mozeidon F3 JXL Status",
     "<Super><Alt><Ctrl>F3",
     f'{PICKER} reuse "JXL Status"'),

    ("Mozeidon F4 JXL Grooming",
     "<Super><Alt><Ctrl>F4",
     f'{PICKER} reuse "JXL Grooming"'),

    # hyper+g → Calendar (kanata: multi lmet lalt lctl g)
    ("Mozeidon Calendar Work",
     "<Super><Alt><Ctrl>g",
     f'{PICKER} reuse "Calendar - Work"'),

    # hyper+m → Google Meet (kanata: multi lmet lalt lctl m)
    ("Mozeidon Google Meet",
     "<Super><Alt><Ctrl>m",
     f'{PICKER} reuse "Google Meet - Work"'),
]

PREFIX = "Mozeidon"  # pro identifikaci našich zkratek při --remove


# ─── gsettings helpers ───────────────────────────────────────────────────────

def gs_get(key: str, path: str = None) -> str:
    schema = f"{CK_SCHEMA}:{path}" if path else SCHEMA
    r = subprocess.run(["gsettings", "get", schema, key],
                       capture_output=True, text=True)
    return r.stdout.strip()


def gs_set(key: str, value: str, path: str = None):
    schema = f"{CK_SCHEMA}:{path}" if path else SCHEMA
    subprocess.run(["gsettings", "set", schema, key, value], check=True)


def gs_reset(key: str, path: str = None):
    schema = f"{CK_SCHEMA}:{path}" if path else SCHEMA
    subprocess.run(["gsettings", "reset", schema, key], check=True)


def current_paths() -> list:
    raw = gs_get("custom-keybindings")
    if raw in ("@as []", "[]", ""):
        return []
    try:
        return ast.literal_eval(raw)
    except Exception:
        return []


def save_paths(paths: list):
    if not paths:
        gs_set("custom-keybindings", "@as []")
    else:
        val = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
        gs_set("custom-keybindings", val)


def strip_quotes(s: str) -> str:
    return s.strip("'\"")


def free_path(existing: list) -> str:
    i = 0
    used = set(existing)
    while f"{BASE_PATH}/custom{i}/" in used:
        i += 1
    return f"{BASE_PATH}/custom{i}/"


# ─── příkazy ─────────────────────────────────────────────────────────────────

def cmd_list():
    paths = current_paths()
    if not paths:
        print("Žádné custom shortcuts.")
        return
    print(f"{'Jméno':<35}  {'Binding':<35}  Příkaz")
    print("─" * 100)
    for p in paths:
        name    = strip_quotes(gs_get("name",    p))
        binding = strip_quotes(gs_get("binding", p))
        command = strip_quotes(gs_get("command", p))
        print(f"{name:<35}  {binding:<35}  {command}")


def cmd_remove():
    paths = current_paths()
    kept  = []
    removed = 0
    for p in paths:
        name = strip_quotes(gs_get("name", p))
        if name.startswith(PREFIX):
            # reset individual keys so the slot is clean
            for key in ("name", "binding", "command"):
                try:
                    gs_reset(key, p)
                except Exception:
                    pass
            print(f"  odstraněn  {name}")
            removed += 1
        else:
            kept.append(p)
    save_paths(kept)
    print(f"\nOdstraněno {removed} zkratek.")


def cmd_register():
    paths = current_paths()

    # Sestav index jméno → cesta pro existující zkratky
    name_to_path: dict = {}
    for p in paths:
        name = strip_quotes(gs_get("name", p))
        name_to_path[name] = p

    for name, binding, command in SHORTCUTS:
        if name in name_to_path:
            p      = name_to_path[name]
            action = "aktualizován"
        else:
            p = free_path(paths)
            paths.append(p)
            action = "přidán    "

        gs_set("name",    name,    p)
        gs_set("binding", binding, p)
        gs_set("command", command, p)
        print(f"  {action}  {name}")
        print(f"              {binding}")

    save_paths(paths)
    print(f"\nHotovo — {len(SHORTCUTS)} zkratek zaregistrováno.")
    print("Změny jsou aktivní okamžitě, restart není potřeba.")


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--list":
        cmd_list()
    elif arg == "--remove":
        cmd_remove()
    elif arg in ("", "--register"):
        cmd_register()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
