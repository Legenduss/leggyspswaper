"""
Leggy's Soundpack Swap
----------------------
1. Finds the GTA V audio "sfx" folder (Steam, Epic Games, Rockstar Games Launcher,
   or a folder you pick manually).
2. Lets you choose a soundpack folder, then one of the "sounds" folders inside it.
3. Replaces RESIDENT.rpf and WEAPONS_PLAYER.rpf in the game's sfx folder with the
   ones from the selected sounds folder.

Originals are backed up the first time a file is replaced and can be restored with
the "RESTORE ORIGINALS" button.

Settings live in  %APPDATA%\\LeggysSoundpackSwap\\config.json
(currently: the last soundpack folder you selected).
"""

import glob
import json
import math
import os
import queue
import re
import shutil
import string
import subprocess
import sys
import threading

try:
    import tkinter as tk
    from tkinter import filedialog, ttk
    from tkinter import font as tkfont
except ImportError:
    print("Tkinter is not available in this Python installation.")
    sys.exit(1)

try:
    import winreg
except ImportError:  # not on Windows
    winreg = None

APP_NAME = "Leggy's Soundpack Swap"
APP_VERSION = "1.1"
APP_DIRNAME = "LeggysSoundpackSwap"
TARGET_FILES = ("RESIDENT.rpf", "WEAPONS_PLAYER.rpf")
SFX_REL = os.path.join("x64", "audio", "sfx")
BACKUP_DIRNAME = "_soundpack_backup"
GAME_EXES = ("GTA5.exe", "GTA5_Enhanced.exe")
CHUNK = 4 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000


# ============================================================================
# Config (stored in %APPDATA%\LeggysSoundpackSwap\config.json)
# ============================================================================
DEFAULT_CONFIG = {"version": 1, "last_soundpack_folder": ""}


def config_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIRNAME)


def config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    """Load settings; missing/corrupt files fall back to defaults."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    if not isinstance(cfg.get("last_soundpack_folder"), str):
        cfg["last_soundpack_folder"] = ""
    return cfg


def save_config(cfg):
    """Write settings atomically. Returns True on success."""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, config_path())
        return True
    except OSError:
        return False


# ============================================================================
# Game detection
# ============================================================================
def resolve_sfx(path):
    """Given a game root, x64, audio or sfx folder, return the sfx folder (or None)."""
    if not path:
        return None
    p = os.path.normpath(path)
    for c in (
        os.path.join(p, SFX_REL),
        os.path.join(p, "audio", "sfx"),
        os.path.join(p, "sfx"),
        p,
    ):
        if os.path.isdir(c) and os.path.basename(c).lower() == "sfx":
            return c
    return None


def _reg_values(hive, key):
    """Read all values under a registry key, checking both 32- and 64-bit views."""
    out = {}
    if winreg is None:
        return out
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    out.setdefault(name, val)
                    i += 1
        except OSError:
            continue
    return out


def _is_gta_dirname(name):
    return name.lower().startswith("grand theft auto v")


def _steam_libraries():
    roots = []
    if winreg is not None:
        v = _reg_values(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam").get("SteamPath")
        if v:
            roots.append(v)
        v = _reg_values(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam").get("InstallPath")
        if v:
            roots.append(v)
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, "Steam"))

    libs = []
    for r in roots:
        r = os.path.normpath(r)
        libs.append(r)
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            libs.append(m.group(1).replace("\\\\", "\\"))
        # Older VDF format: "1"  "D:\\SteamLibrary"
        for m in re.finditer(r'^\s*"\d+"\s+"([A-Za-z]:[^"]+)"', text, re.M):
            libs.append(m.group(1).replace("\\\\", "\\"))

    seen, unique = set(), []
    for l in libs:
        n = os.path.normcase(os.path.normpath(l))
        if n not in seen:
            seen.add(n)
            unique.append(os.path.normpath(l))
    return unique


def find_steam():
    found = []
    for lib in _steam_libraries():
        common = os.path.join(lib, "steamapps", "common")
        try:
            names = os.listdir(common)
        except OSError:
            continue
        for d in names:
            if _is_gta_dirname(d):
                sfx = resolve_sfx(os.path.join(common, d))
                if sfx:
                    found.append(sfx)
    return found


def find_epic():
    found = []
    pd = os.environ.get("ProgramData", r"C:\ProgramData")
    pattern = os.path.join(pd, "Epic", "EpicGamesLauncher", "Data", "Manifests", "*.item")
    for f in glob.glob(pattern):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if "grand theft auto v" in str(data.get("DisplayName", "")).lower():
            sfx = resolve_sfx(data.get("InstallLocation"))
            if sfx:
                found.append(sfx)
    return found


def find_rockstar():
    """Returns list of (label, sfx_path)."""
    found = []
    if winreg is not None:
        for key in (
            r"SOFTWARE\Rockstar Games\Grand Theft Auto V",
            r"SOFTWARE\Rockstar Games\GTAV",
        ):
            for name, val in _reg_values(winreg.HKEY_LOCAL_MACHINE, key).items():
                if not name.lower().startswith("installfolder") or not isinstance(val, str):
                    continue
                sfx = resolve_sfx(val)
                if not sfx:
                    continue
                low = name.lower()
                if low.endswith("steam"):
                    label = "Steam"
                elif low.endswith("epic"):
                    label = "Epic Games"
                else:
                    label = "Rockstar Games Launcher"
                found.append((label, sfx))

    rel_paths = (
        os.path.join("Program Files", "Rockstar Games", "Grand Theft Auto V"),
        os.path.join("Program Files (x86)", "Rockstar Games", "Grand Theft Auto V"),
        os.path.join("Rockstar Games", "Grand Theft Auto V"),
    )
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            for rel in rel_paths:
                sfx = resolve_sfx(f"{letter}:\\{rel}")
                if sfx:
                    found.append(("Rockstar Games Launcher", sfx))
    return found


def find_installs():
    """Return a de-duplicated list of (launcher_label, sfx_path)."""
    candidates = []
    candidates += [("Steam", p) for p in find_steam()]
    candidates += [("Epic Games", p) for p in find_epic()]
    candidates += find_rockstar()

    seen, result = set(), []
    for label, path in candidates:
        n = os.path.normcase(os.path.normpath(path))
        if n not in seen:
            seen.add(n)
            result.append((label, os.path.normpath(path)))
    return result


# ============================================================================
# Soundpack scanning / file operations
# ============================================================================
def scan_pack(root, max_depth=4):
    """
    Find every folder inside `root` (including root itself) that contains
    RESIDENT.rpf and/or WEAPONS_PLAYER.rpf.
    Returns a list of (folder_path, {target_name: file_path}) sorted by path.
    """
    root = os.path.normpath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        lower = {f.lower(): f for f in filenames}
        found = {
            t: os.path.join(dirpath, lower[t.lower()])
            for t in TARGET_FILES
            if t.lower() in lower
        }
        if found:
            results.append((dirpath, found))
    results.sort(key=lambda r: r[0].lower())
    return results


def copy_atomic(src, dst, progress_cb=None):
    """Copy via a temp file in the destination folder, then swap it in."""
    tmp = dst + ".sptmp"
    total = os.path.getsize(src)
    done = 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                buf = fi.read(CHUNK)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                if progress_cb:
                    progress_cb(done, total)
        if os.path.getsize(tmp) != total:
            raise IOError(f"Size mismatch while copying {os.path.basename(src)}")
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def install_files(sfx, files, log=print, progress=None):
    """
    Back up originals (once) and copy `files` ({name: src_path}) into `sfx`.
    Returns list of installed file names.
    """
    backup_dir = os.path.join(sfx, BACKUP_DIRNAME)
    os.makedirs(backup_dir, exist_ok=True)
    installed = []

    def cb_for(label):
        return (lambda d, t: progress(label, d, t)) if progress else None

    for name, src in files.items():
        dst = os.path.join(sfx, name)
        bak = os.path.join(backup_dir, name)
        if os.path.isfile(dst) and not os.path.exists(bak):
            log(f"Backing up original {name} ...")
            copy_atomic(dst, bak, cb_for(f"Backing up {name}"))
        log(f"Installing {name} ...")
        copy_atomic(src, dst, cb_for(f"Installing {name}"))
        installed.append(name)
    return installed


def restore_files(sfx, log=print, progress=None):
    backup_dir = os.path.join(sfx, BACKUP_DIRNAME)
    restored = []
    for name in TARGET_FILES:
        bak = os.path.join(backup_dir, name)
        if os.path.isfile(bak):
            log(f"Restoring {name} ...")
            cb = (lambda d, t, n=name: progress(f"Restoring {n}", d, t)) if progress else None
            copy_atomic(bak, os.path.join(sfx, name), cb)
            restored.append(name)
    return restored


def game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW,
        ).stdout.lower()
        return any(f'"{e.lower()}"' in out for e in GAME_EXES)
    except Exception:
        return False


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ============================================================================
# Sigil geometry: tapered "blades" built from Bezier curves
# ============================================================================
def _decasteljau(pts, t):
    pts = list(pts)
    while len(pts) > 1:
        pts = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(pts, pts[1:])]
    return pts[0]


def _centerline(ctrl, steps):
    """Sample a Bezier (2-4 pts) or a chain of Beziers (list of lists)."""
    segs = ctrl if isinstance(ctrl[0][0], (list, tuple)) else [ctrl]
    pts = []
    for si, seg in enumerate(segs):
        for i in range(steps + 1):
            if si > 0 and i == 0:
                continue
            pts.append(_decasteljau(seg, i / steps))
    return pts


def ribbon(ctrl, wmax, profile="leaf", steps=36, peak=0.3):
    """
    Sharp tapered blade along a Bezier curve (or chain of curves).
    profile: 'leaf'   pointed at both ends
             'taper'  full width at the start, pointed at the far end
             'rtaper' pointed at the start, full width at the far end
             'blade'  pointed at both ends, widest near `peak` (0..1) along the curve
    Returns a list of (x, y) polygon points.
    """
    c = _centerline(ctrl, steps)
    n = len(c)
    left, right = [], []
    for i, p in enumerate(c):
        a = c[max(0, i - 1)]
        b = c[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / d, dx / d
        t = i / (n - 1)
        if profile == "leaf":
            w = wmax * math.sin(math.pi * t) ** 0.6
        elif profile == "taper":
            w = wmax * (1 - t) ** 0.8
        elif profile == "blade":
            k = math.log(0.5) / math.log(peak)
            w = wmax * math.sin(math.pi * t ** k) ** 0.8
        else:
            w = wmax * t ** 0.8
        left.append((p[0] + nx * w, p[1] + ny * w))
        right.append((p[0] - nx * w, p[1] - ny * w))
    return left + right[::-1]


def mirror_x(poly, cx):
    return [(2 * cx - x, y) for x, y in poly]


# ---- LSFX wordmark (letter slot ~100 wide x 130 tall, y grows downward) -----
ADV = 118
_S_MID = (50, 66)
_S_A = [(92, 20), (72, -18), (8, -6), (10, 42)]
_S_A2 = [(10, 42), (12, 58), (30, 60), _S_MID]
_S_B = [_S_MID, (70, 72), (88, 74), (90, 90)]
_S_B2 = [(90, 90), (92, 138), (28, 146), (8, 112)]

GLYPHS = {
    "L": lambda: [
        ribbon([(24, -30), (24, 132)], 10, "blade", peak=0.7),
        ribbon([(24, 120), (58, 132), (92, 122), (116, 92)], 9, "taper"),
        ribbon([(24, 124), (10, 138), (4, 158)], 4, "taper"),
    ],
    "S": lambda: [
        ribbon([_S_A, _S_A2, _S_B, _S_B2], 9, "leaf", steps=30),
        ribbon([(92, 20), (100, 4), (98, -22)], 4, "taper"),
        ribbon([(8, 112), (0, 130), (2, 156)], 4, "taper"),
    ],
    "F": lambda: [
        ribbon([(24, -30), (24, 150)], 9, "leaf"),
        ribbon([(24, 10), (54, -2), (84, 0), (112, -24)], 8, "taper"),
        ribbon([(24, 62), (48, 52), (70, 54), (90, 38)], 6, "taper"),
        ribbon([(24, 136), (12, 152), (8, 172)], 4, "taper"),
    ],
    "X": lambda: [
        ribbon([(0, -22), (36, 40), (64, 92), (102, 152)], 9, "leaf"),
        ribbon([(102, -22), (66, 40), (38, 92), (0, 152)], 9, "leaf"),
        ribbon([(51, 65), (51, 30), (51, 0), (51, -34)], 3.5, "taper"),
        ribbon([(51, 65), (51, 100), (51, 130), (51, 164)], 3.5, "taper"),
    ],
}


def logo_letters(text="LSFX"):
    return [(GLYPHS[ch](), i * ADV) for i, ch in enumerate(text)]


def logo_wings(span=1.0, width=4 * ADV - 16):
    """Symmetric thorn wings flanking the wordmark: list of (polygon, colour_key)."""
    cx = width / 2
    left = [
        (ribbon([(-8, 62), (-56, 50), (-102, 20), (-158, -30)], 9, "blade", peak=0.22), "a"),
        (ribbon([(-8, 72), (-70, 74), (-128, 62), (-200, 32)], 8, "blade", peak=0.22), "a"),
        (ribbon([(-8, 82), (-58, 98), (-102, 124), (-148, 176)], 8, "blade", peak=0.22), "a"),
        (ribbon([(-4, 56), (-34, 28), (-48, -8), (-52, -52)], 5, "blade", peak=0.3), "b"),
        (ribbon([(-4, 90), (-30, 120), (-42, 156), (-44, 204)], 5, "blade", peak=0.3), "b"),
    ]
    out = []
    for poly, key in left:
        p = [(x * span, y) for x, y in poly]
        out.append((p, key))
        out.append((mirror_x(p, cx), key))
    return out


def logo_bar(width=4 * ADV - 16, y=196):
    """Hairline sigil bar under the wordmark. Returns list of polygons."""
    cx = width / 2
    half = ribbon([(cx, y), (cx + 90, y), (cx + 170, y), (cx + 250, y)], 3, "blade", peak=0.25)
    dia = [(cx, y - 9), (cx + 7, y), (cx, y + 9), (cx - 7, y)]
    return [half, mirror_x(half, cx), dia]


# ============================================================================
# Theme
# ============================================================================
BG = "#06060b"
PANEL = "#0b0b15"
PANEL2 = "#08080f"
LINE = "#2a2050"
CYAN = "#19f0ff"
CYAN_HI = "#d7fcff"
VIOLET = "#a24bff"
MAGENTA = "#ff2bd6"
GREEN = "#3dffa0"
AMBER = "#ffc233"
RED = "#ff3b5c"
TEXT = "#d9e4ff"
DIM = "#6a7096"
OFF_LINE = "#2b2b40"
OFF_TEXT = "#44445e"

UI = 1.0                  # DPI scale, set at startup
FONT_DISPLAY = "Segoe UI"
FONT_MONO = "Consolas"


def px(v):
    return int(round(v * UI))


def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a, b, t):
    """Blend colour a towards b by fraction t (0 = a, 1 = b)."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "#%02x%02x%02x" % (
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def flat(poly):
    return [c for pt in poly for c in pt]


def text_width(text, font):
    try:
        return tkfont.Font(font=font).measure(text)
    except Exception:
        return len(text) * px(8)


def init_ui_metrics(root):
    """Pick DPI scale and the best available fonts."""
    global UI, FONT_DISPLAY, FONT_MONO
    try:
        UI = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        UI = 1.0
    try:
        fams = {f.lower(): f for f in tkfont.families(root)}
    except Exception:
        fams = {}
    for name in ("Bahnschrift", "Orbitron", "Rajdhani", "Segoe UI Semibold", "Segoe UI"):
        if name.lower() in fams:
            FONT_DISPLAY = fams[name.lower()]
            break
    for name in ("Cascadia Mono", "Consolas", "Lucida Console", "Courier New"):
        if name.lower() in fams:
            FONT_MONO = fams[name.lower()]
            break


def apply_dark_titlebar(win):
    """Dark, on-theme title bar on Windows 10/11 (silently ignored elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi

        def setattr_(attr, value):
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        if setattr_(20, 1) != 0:      # immersive dark mode (Win10 20H1+/Win11)
            setattr_(19, 1)           # older Win10 builds
        r, g, b = _rgb(BG)
        setattr_(35, r | (g << 8) | (b << 16))                      # caption colour (Win11)
        r, g, b = _rgb(VIOLET)
        setattr_(34, r | (g << 8) | (b << 16))                      # border colour (Win11)
        r, g, b = _rgb(CYAN)
        setattr_(36, r | (g << 8) | (b << 16))                      # caption text (Win11)
    except Exception:
        pass


# ============================================================================
# Canvas drawing (canvas-agnostic helper functions)
# ============================================================================
def draw_glow_polys(c, layers, glow_px=(6, 3.5), fade=(0.86, 0.66)):
    """layers: list of (polygon_points_in_pixels, fill_colour, glow_colour)."""
    for width, t in zip(glow_px, fade):
        for poly, _fill, glow in layers:
            c.create_polygon(flat(poly), fill="", outline=mix(glow, BG, t),
                             width=px(width), joinstyle="round")
    for poly, fill, _glow in layers:
        c.create_polygon(flat(poly), fill=fill, outline=fill, width=1)


def draw_logo(c, cx, cy, scale, wings=True):
    """Draw the LSFX wordmark centred (letters) on (cx, cy)."""
    ox, oy = cx - 229 * scale, cy - 69 * scale

    def tr(poly, dx=0):
        return [(ox + (x + dx) * scale, oy + y * scale) for x, y in poly]

    layers = []
    if wings:
        for p, key in logo_wings():
            col = VIOLET if key == "a" else MAGENTA
            layers.append((tr(p), col, col))
        for p in logo_bar():
            layers.append((tr(p), "#ff96eb", MAGENTA))
    for glyphs, gx in logo_letters():
        for p in glyphs:
            layers.append((tr(p, gx), CYAN_HI, CYAN))
    draw_glow_polys(c, layers)


def draw_wing_cluster(c, rx, cy, scale, face_right=True):
    """A cluster of thorn blades rooted at (rx, cy), fanning left or right."""
    layers = []
    for p, key in logo_wings()[0::2]:                 # left-facing wings only
        col = VIOLET if key == "a" else MAGENTA
        if face_right:
            pts = [(rx - (x + 8) * scale, cy + (y - 69) * scale) for x, y in p]
        else:
            pts = [(rx + (x + 8) * scale, cy + (y - 69) * scale) for x, y in p]
        layers.append((pts, col, col))
    draw_glow_polys(c, layers, glow_px=(4, 2.5))


def draw_divider(c, w, y, accent=VIOLET):
    cx = w / 2
    half = ribbon([(cx, y), (cx + w * 0.16, y), (cx + w * 0.32, y), (cx + w / 2 - px(10), y)],
                  px(1.8), "blade", peak=0.2)
    c.create_polygon(flat(half), fill=accent, outline="")
    c.create_polygon(flat(mirror_x(half, cx)), fill=accent, outline="")
    d = px(6)
    c.create_polygon(cx, y - d, cx + d, y, cx, y + d, cx - d, y, fill=CYAN, outline="")


def draw_header(c, w, h):
    c.delete("all")
    # faint scan-lines
    step = px(4)
    line_col = mix(BG, VIOLET, 0.06)
    for y in range(0, int(h), step):
        c.create_line(0, y, w, y, fill=line_col)

    s = 0.42 * UI
    cx = px(16) + 429 * s
    cy = h / 2 - px(2)
    draw_logo(c, cx, cy, s)

    tx = cx + 427 * s + px(30)                        # just right of the logo's right wing
    # title (chromatic-offset shadow for that y2k glitch feel)
    title_font = (FONT_DISPLAY, 30, "bold")
    c.create_text(tx + px(2), h * 0.31 + px(2), text="LEGGY'S", fill=mix(MAGENTA, BG, 0.25),
                  font=title_font, anchor="w")
    c.create_text(tx, h * 0.31, text="LEGGY'S", fill=CYAN_HI, font=title_font, anchor="w")
    c.create_text(tx, h * 0.58, text="SOUNDPACK  SWAP", fill=CYAN,
                  font=(FONT_DISPLAY, 18, "bold"), anchor="w")
    c.create_text(tx, h * 0.79, text="GTA V  //  AUDIO SWAP UTILITY", fill=DIM,
                  font=(FONT_MONO, 9), anchor="w")

    if w > px(820):
        draw_wing_cluster(c, w - px(120), h / 2 - px(6), 0.42 * UI, face_right=True)
    draw_divider(c, w, h - px(6))


def draw_panel_header(c, w, h, title, accent):
    c.delete("all")
    d = px(4)
    cy = h / 2
    x = px(8)
    c.create_polygon(x, cy - d, x + d, cy, x, cy + d, x - d, cy, fill=accent, outline="")
    font = (FONT_MONO, 9, "bold")
    c.create_text(x + px(12), cy, text=title, fill=accent, font=font, anchor="w")
    x0 = x + px(12) + text_width(title, font) + px(14)
    x1 = w - px(14)
    if x1 > x0 + px(20):
        line = ribbon([(x0, cy), (x1, cy)], px(1.5), "blade", peak=0.12, steps=24)
        c.create_polygon(flat(line), fill=mix(accent, BG, 0.35), outline="")
        c.create_polygon(w - px(8), cy - d, w - px(4), cy, w - px(8), cy + d, w - px(12), cy,
                         fill=mix(accent, BG, 0.35), outline="")


BUTTON_STYLES = {
    "primary": {"accent": CYAN, "hover": "#06323a"},
    "danger": {"accent": MAGENTA, "hover": "#35082f"},
    "ghost": {"accent": VIOLET, "hover": "#22103f"},
}


def draw_button(c, w, h, text, style, state):
    """state: 'normal' | 'hover' | 'down' | 'disabled'"""
    c.delete("all")
    st = BUTTON_STYLES[style]
    acc = st["accent"]
    m = px(3)
    ch = h * 0.42
    pts = [(m, h / 2), (m + ch, m), (w - m - ch, m), (w - m, h / 2), (w - m - ch, h - m), (m + ch, h - m)]
    if state == "disabled":
        fill, outline, tcol = BG, OFF_LINE, OFF_TEXT
    elif state == "down":
        fill, outline, tcol = acc, acc, BG
    elif state == "hover":
        fill, outline, tcol = st["hover"], acc, CYAN_HI
    else:
        fill, outline, tcol = PANEL2, mix(acc, BG, 0.45), acc
    if state in ("hover", "down"):
        c.create_polygon(flat(pts), fill="", outline=mix(acc, BG, 0.62), width=px(5), joinstyle="round")
    c.create_polygon(flat(pts), fill=fill, outline=outline, width=max(1, px(1.5)), joinstyle="miter")
    # tiny thorn ticks on the tips
    if state != "disabled":
        c.create_line(0, h / 2, m + px(5), h / 2, fill=mix(acc, BG, 0.3), width=1)
        c.create_line(w, h / 2, w - m - px(5), h / 2, fill=mix(acc, BG, 0.3), width=1)
    c.create_text(w / 2, h / 2 - 1, text=text, fill=tcol, font=(FONT_DISPLAY, 10, "bold"))


def draw_progress(c, w, h, pct):
    c.delete("all")
    slant = px(5)
    n = max(12, int((w - px(4)) / px(13)))
    seg = (w - px(4) - slant) / n
    lit = int(round(n * max(0.0, min(100.0, pct)) / 100.0))
    for i in range(n):
        x = px(2) + i * seg
        pts = [(x + slant, px(2)), (x + seg - px(2) + slant, px(2)), (x + seg - px(2), h - px(2)), (x, h - px(2))]
        if i < lit:
            col = mix(CYAN, VIOLET, i / max(1, n - 1))
        else:
            col = "#12122a"
        c.create_polygon(flat(pts), fill=col, outline="")


# ============================================================================
# Custom widgets
# ============================================================================
class SigilButton(tk.Canvas):
    def __init__(self, parent, text, command, style="primary", width=170, height=36, bg=PANEL):
        self.sg_w, self.sg_h = px(width), px(height)
        super().__init__(parent, width=self.sg_w, height=self.sg_h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.sg_text, self.sg_cmd, self.sg_style = text, command, style
        self.sg_enabled, self.sg_hover, self.sg_down = True, False, False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    def _state(self):
        if not self.sg_enabled:
            return "disabled"
        if self.sg_down:
            return "down"
        return "hover" if self.sg_hover else "normal"

    def _redraw(self):
        draw_button(self, self.sg_w, self.sg_h, self.sg_text, self.sg_style, self._state())

    def set_enabled(self, enabled):
        self.sg_enabled = bool(enabled)
        self.sg_hover = self.sg_down = False
        self.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def _on_enter(self, _e):
        self.sg_hover = True
        self._redraw()

    def _on_leave(self, _e):
        self.sg_hover = self.sg_down = False
        self._redraw()

    def _on_press(self, _e):
        if self.sg_enabled:
            self.sg_down = True
            self._redraw()

    def _on_release(self, e):
        was_down = self.sg_down
        self.sg_down = False
        self._redraw()
        if self.sg_enabled and was_down and 0 <= e.x <= self.sg_w and 0 <= e.y <= self.sg_h:
            self.sg_cmd()


class SigilProgress(tk.Canvas):
    def __init__(self, parent, height=20, bg=BG):
        super().__init__(parent, height=px(height), bg=bg, highlightthickness=0, bd=0)
        self.sg_pct = 0.0
        self.sg_size = (px(200), px(height))
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, e):
        self.sg_size = (e.width, e.height)
        draw_progress(self, e.width, e.height, self.sg_pct)

    def set(self, pct):
        self.sg_pct = pct
        draw_progress(self, self.sg_size[0], self.sg_size[1], pct)


class SigilPanel(tk.Frame):
    """Dark panel with a sigil-styled title strip. Put widgets in `.body`."""

    def __init__(self, parent, title, accent=CYAN):
        super().__init__(parent, bg=PANEL, highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=LINE)
        self.head = tk.Canvas(self, height=px(30), bg=PANEL, highlightthickness=0, bd=0)
        self.head.pack(fill="x")
        self.head.bind("<Configure>",
                       lambda e: draw_panel_header(self.head, e.width, e.height, title, accent))
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=px(12), pady=(0, px(12)))


class SigilDialog(tk.Toplevel):
    """Themed replacement for the plain OS message boxes."""
    ACCENTS = {"info": CYAN, "ask": VIOLET, "warn": AMBER, "error": RED}

    def __init__(self, parent, title, message, kind="info", buttons=None):
        super().__init__(parent)
        self.withdraw()
        self.result = False
        accent = self.ACCENTS.get(kind, CYAN)
        self.title(APP_NAME)
        self.configure(bg=accent)
        self.resizable(False, False)
        self.transient(parent)
        if buttons is None:
            buttons = [("OK", True, "primary")]

        inner = tk.Frame(self, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Canvas(inner, height=px(34), bg=PANEL, highlightthickness=0, bd=0)
        head.pack(fill="x")
        head.bind("<Configure>",
                  lambda e: draw_panel_header(head, e.width, e.height, "// " + title.upper(), accent))
        tk.Label(inner, text=message, bg=PANEL, fg=TEXT, font=(FONT_MONO, 10), justify="left",
                 anchor="w", wraplength=px(520)).pack(fill="x", padx=px(22), pady=(px(8), px(18)))

        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x", padx=px(22), pady=(0, px(18)))
        for label, value, style in reversed(buttons):
            SigilButton(row, label, (lambda v=value: self._finish(v)), style=style,
                        width=150, height=34, bg=PANEL).pack(side="right", padx=(px(10), 0))

        self.bind("<Escape>", lambda e: self._finish(False))
        self.bind("<Return>", lambda e: self._finish(True))
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(False))

        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        apply_dark_titlebar(self)
        self.deiconify()
        try:
            self.wait_visibility()
            self.grab_set()
        except Exception:
            pass
        self.focus_force()
        self.wait_window(self)

    def _finish(self, value):
        self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def show_dialog(parent, title, message, kind="info", ask=False, yes="CONTINUE", no="CANCEL"):
    if ask:
        buttons = [(no, False, "ghost"), (yes, True, "primary")]
    else:
        buttons = [("CLOSE" if kind == "error" else "OK", True, "danger" if kind == "error" else "primary")]
    return SigilDialog(parent, title, message, kind, buttons).result


# ============================================================================
# Application
# ============================================================================
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.sfx = None
        self.installs = []
        self.pack_entries = {}     # tree item id -> (folder, files)
        self.q = queue.Queue()
        self.busy = False

        root.title(APP_NAME)
        root.configure(bg=BG)
        try:
            avail = root.winfo_screenheight() - px(90)
        except Exception:
            avail = px(860)
        win_h = min(px(860), max(px(620), avail))
        root.geometry(f"{px(940)}x{win_h}")
        root.minsize(px(880), min(px(720), win_h))
        self._init_style()
        self._build_ui()
        apply_dark_titlebar(root)
        root.after(150, self._startup)
        root.after(100, self._poll_queue)

    # ---- styling ----------------------------------------------------------
    def _init_style(self):
        r = self.root
        st = ttk.Style(r)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        mono = (FONT_MONO, 9)
        st.configure("Sigil.Treeview", background=PANEL2, fieldbackground=PANEL2, foreground=TEXT,
                     bordercolor=LINE, lightcolor=PANEL2, darkcolor=PANEL2, borderwidth=0,
                     rowheight=px(28), font=(FONT_MONO, 10))
        st.layout("Sigil.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.map("Sigil.Treeview", background=[("selected", "#26104a")], foreground=[("selected", CYAN_HI)])
        st.configure("Sigil.Treeview.Heading", background="#120e26", foreground=VIOLET, relief="flat",
                     borderwidth=0, font=(FONT_MONO, 9, "bold"), padding=(px(6), px(5)))
        st.map("Sigil.Treeview.Heading", background=[("active", "#1c1440")], foreground=[("active", CYAN)])
        st.configure("Sigil.Vertical.TScrollbar", background="#2a1a55", troughcolor=PANEL2,
                     bordercolor=PANEL2, lightcolor="#2a1a55", darkcolor="#2a1a55", arrowcolor=CYAN,
                     gripcount=0, relief="flat")
        st.map("Sigil.Vertical.TScrollbar", background=[("active", VIOLET)])
        st.configure("Sigil.TCombobox", fieldbackground=PANEL2, background="#2a1a55", foreground=TEXT,
                     arrowcolor=CYAN, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     selectbackground=PANEL2, selectforeground=CYAN_HI, padding=px(5))
        st.map("Sigil.TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", TEXT)],
               selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", CYAN_HI)],
               bordercolor=[("focus", CYAN)])
        r.option_add("*TCombobox*Listbox.background", PANEL2)
        r.option_add("*TCombobox*Listbox.foreground", TEXT)
        r.option_add("*TCombobox*Listbox.selectBackground", "#26104a")
        r.option_add("*TCombobox*Listbox.selectForeground", CYAN_HI)
        r.option_add("*TCombobox*Listbox.font", mono)

    # ---- layout -----------------------------------------------------------
    def _build_ui(self):
        r = self.root
        self.header = tk.Canvas(r, height=px(150), bg=BG, highlightthickness=0, bd=0)
        self.header.pack(fill="x")
        self.header.bind("<Configure>", lambda e: draw_header(self.header, e.width, e.height))

        # footer first (bottom-anchored) so it is never clipped on short screens
        foot = tk.Frame(r, bg=BG)
        foot.pack(side="bottom", fill="x", padx=px(22), pady=(0, px(8)))
        self.status = tk.Label(foot, text="\u25c6 READY", bg=BG, fg=CYAN, font=(FONT_MONO, 9, "bold"))
        self.status.pack(side="left")
        tk.Label(foot, text=f"{APP_NAME.upper()}  //  v{APP_VERSION}", bg=BG, fg=DIM,
                 font=(FONT_MONO, 9)).pack(side="right")

        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=px(22), pady=(px(6), 0))

        # 01 -- game folder
        p1 = SigilPanel(body, "01  //  TARGET  ::  GTA V  x64\\audio\\sfx", CYAN)
        p1.pack(side="top", fill="x", pady=(0, px(10)))
        row = tk.Frame(p1.body, bg=PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        self.game_combo = ttk.Combobox(row, state="readonly", style="Sigil.TCombobox", font=(FONT_MONO, 9))
        self.game_combo.pack(side="left", fill="x", expand=True)
        self.game_combo.bind("<<ComboboxSelected>>", self._on_game_selected)
        self.browse_game_btn = SigilButton(row, "BROWSE", self.browse_game, "ghost", width=104, height=32)
        self.browse_game_btn.pack(side="right", padx=(px(8), 0))
        self.rescan_btn = SigilButton(row, "RESCAN", self.scan_games, "ghost", width=104, height=32)
        self.rescan_btn.pack(side="right", padx=(px(8), 0))
        self.game_status = tk.Label(p1.body, text="", bg=PANEL, fg=DIM, font=(FONT_MONO, 9),
                                    anchor="w", justify="left")
        self.game_status.pack(fill="x", pady=(px(8), 0))

        # 02 -- soundpack folder
        p2 = SigilPanel(body, "02  //  SOUNDPACK FOLDER", VIOLET)
        p2.pack(side="top", fill="x", pady=(0, px(10)))
        row = tk.Frame(p2.body, bg=PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        self.pack_var = tk.StringVar()
        tk.Entry(row, textvariable=self.pack_var, state="readonly", readonlybackground=PANEL2,
                 fg=TEXT, relief="flat", font=(FONT_MONO, 9), highlightthickness=1,
                 highlightbackground=LINE, highlightcolor=LINE).pack(
            side="left", fill="x", expand=True, ipady=px(6))
        self.browse_pack_btn = SigilButton(row, "BROWSE", self.browse_pack, "ghost", width=104, height=32)
        self.browse_pack_btn.pack(side="right", padx=(px(8), 0))

        # bottom group (packed bottom-up: log, progress text, action row)
        p5 = SigilPanel(body, "LOG  //  OUTPUT", DIM)
        p5.pack(side="bottom", fill="x", pady=(0, px(6)))
        self.log_text = tk.Text(p5.body, height=5, state="disabled", wrap="word", bg=PANEL2, fg=TEXT,
                                relief="flat", font=(FONT_MONO, 9), padx=px(8), pady=px(6),
                                highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE,
                                insertbackground=CYAN, selectbackground="#26104a")
        self.log_text.pack(fill="x", pady=(px(4), 0))
        for tag, col in (("info", TEXT), ("sys", CYAN), ("ok", GREEN), ("warn", AMBER), ("err", RED)):
            self.log_text.tag_configure(tag, foreground=col)

        self.progress_label = tk.Label(body, text="", bg=BG, fg=DIM, font=(FONT_MONO, 9), anchor="w")
        self.progress_label.pack(side="bottom", fill="x", pady=(0, px(6)))

        act = tk.Frame(body, bg=BG)
        act.pack(side="bottom", fill="x", pady=(0, px(4)))
        self.install_btn = SigilButton(act, "INSTALL SOUNDS", self.on_install, "primary",
                                       width=210, height=42, bg=BG)
        self.install_btn.pack(side="left")
        self.restore_btn = SigilButton(act, "RESTORE ORIGINALS", self.on_restore, "danger",
                                       width=230, height=42, bg=BG)
        self.restore_btn.pack(side="left", padx=(px(10), 0))
        self.progress = SigilProgress(act, height=22, bg=BG)
        self.progress.pack(side="left", fill="x", expand=True, padx=(px(16), 0))

        # 03 -- sounds folder list (takes whatever space is left)
        p3 = SigilPanel(body, "03  //  SELECT SOUNDS FOLDER", MAGENTA)
        p3.pack(side="top", fill="both", expand=True, pady=(0, px(10)))
        wrap = tk.Frame(p3.body, bg=PANEL)
        wrap.pack(fill="both", expand=True, pady=(px(4), 0))
        self.tree = ttk.Treeview(wrap, columns=("resident", "weapons"), selectmode="browse",
                                 style="Sigil.Treeview", height=5)
        self.tree.heading("#0", text="SOUNDS FOLDER", anchor="w")
        self.tree.heading("resident", text="RESIDENT.RPF")
        self.tree.heading("weapons", text="WEAPONS_PLAYER.RPF")
        self.tree.column("#0", width=px(420), anchor="w")
        self.tree.column("resident", width=px(130), anchor="center")
        self.tree.column("weapons", width=px(190), anchor="center")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview,
                           style="Sigil.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        self._update_buttons()

    # ---- helpers ----------------------------------------------------------
    def log(self, msg, tag="info"):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\u203a " + msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text, colour):
        self.status.configure(text="\u25c6 " + text, fg=colour)

    def _selected_entry(self):
        sel = self.tree.selection()
        return self.pack_entries.get(sel[0]) if sel else None

    def _update_buttons(self):
        idle = not self.busy
        self.install_btn.set_enabled(bool(idle and self.sfx and self._selected_entry()))
        has_backup = bool(self.sfx) and any(
            os.path.isfile(os.path.join(self.sfx, BACKUP_DIRNAME, n)) for n in TARGET_FILES
        )
        self.restore_btn.set_enabled(bool(idle and has_backup))

    # ---- startup ----------------------------------------------------------
    def _startup(self):
        self.log(f"Config: {config_path()}", "sys")
        self.scan_games()
        last = self.cfg.get("last_soundpack_folder", "")
        if last:
            if os.path.isdir(last):
                self.log("Reloading your last soundpack folder ...", "sys")
                self.load_pack(last, startup=True)
            else:
                self.log(f"Last soundpack folder no longer exists: {last}", "warn")

    # ---- game folder ------------------------------------------------------
    def scan_games(self):
        self.log("Scanning for GTA V (Steam / Epic Games / Rockstar Launcher) ...", "sys")
        self.installs = find_installs()
        self._refresh_combo()
        if self.installs:
            self.game_combo.current(0)
            self._set_sfx(self.installs[0][1])
            self.log(f"Found {len(self.installs)} install(s).", "ok")
        else:
            self.sfx = None
            self.game_status.configure(
                text="Not found automatically \u2014 press BROWSE and pick the folder containing GTA5.exe.",
                fg=AMBER)
            self.log("No GTA V install found automatically.", "warn")
        self._update_buttons()

    def _refresh_combo(self):
        self.game_combo["values"] = [f"{label}   \u2502   {path}" for label, path in self.installs]

    def _on_game_selected(self, _e=None):
        i = self.game_combo.current()
        if 0 <= i < len(self.installs):
            self._set_sfx(self.installs[i][1])

    def browse_game(self):
        d = filedialog.askdirectory(title="Select your GTA V folder (or its x64\\audio\\sfx folder)")
        if not d:
            return
        sfx = resolve_sfx(d)
        if not sfx:
            show_dialog(self.root, "Folder not recognised",
                        "Couldn't find x64\\audio\\sfx inside that folder.\n\n"
                        "Pick the folder that contains GTA5.exe.", "error")
            return
        self.installs.append(("Manual", sfx))
        self._refresh_combo()
        self.game_combo.current(len(self.installs) - 1)
        self._set_sfx(sfx)

    def _set_sfx(self, sfx):
        self.sfx = sfx
        missing = [n for n in TARGET_FILES if not os.path.isfile(os.path.join(sfx, n))]
        if missing:
            self.game_status.configure(
                text=f"{sfx}\n! missing {', '.join(missing)} in this folder", fg=AMBER)
        else:
            self.game_status.configure(text=sfx, fg=DIM)
        self._update_buttons()

    # ---- soundpack --------------------------------------------------------
    def browse_pack(self):
        last = self.cfg.get("last_soundpack_folder", "")
        initial = os.path.dirname(last) if last and os.path.isdir(os.path.dirname(last)) else None
        kwargs = {"title": "Select the soundpack folder"}
        if initial:
            kwargs["initialdir"] = initial
        d = filedialog.askdirectory(**kwargs)
        if d:
            self.load_pack(d)

    def load_pack(self, path, startup=False):
        path = os.path.normpath(path)
        self.pack_var.set(path)
        self.tree.delete(*self.tree.get_children())
        self.pack_entries.clear()

        results = scan_pack(path)
        if not results:
            self.log("No RESIDENT.rpf or WEAPONS_PLAYER.rpf found in that soundpack.", "warn")
            if not startup:
                show_dialog(self.root, "Nothing to install",
                            "That folder doesn't contain any RESIDENT.rpf or WEAPONS_PLAYER.rpf "
                            "files (subfolders were searched too).", "warn")
            self._update_buttons()
            return

        for folder, files in results:
            rel = os.path.relpath(folder, path)
            name = "(soundpack folder itself)" if rel == "." else rel
            iid = self.tree.insert(
                "", "end", text="  " + name,
                values=("\u25c6" if "RESIDENT.rpf" in files else "\u00b7",
                        "\u25c6" if "WEAPONS_PLAYER.rpf" in files else "\u00b7"),
            )
            self.pack_entries[iid] = (folder, files)
        self.log(f"Found {len(results)} sounds folder(s) in the soundpack.", "ok")
        if len(results) == 1:
            self.tree.selection_set(self.tree.get_children()[0])

        # remember this folder for next launch
        self.cfg["last_soundpack_folder"] = path
        if not save_config(self.cfg):
            self.log("Couldn't write config file (settings won't be remembered).", "warn")
        self._update_buttons()

    # ---- actions ----------------------------------------------------------
    def _check_ready(self):
        if game_running():
            show_dialog(self.root, "GTA V is running",
                        "Close the game (and check the launcher isn't keeping it open) "
                        "before changing sound files.", "error")
            return False
        return True

    def on_install(self):
        entry = self._selected_entry()
        if not (self.sfx and entry) or self.busy or not self._check_ready():
            return
        folder, files = entry
        lines = "\n".join(f"   \u25c6 {n}  ({fmt_size(os.path.getsize(p))})" for n, p in files.items())
        if not show_dialog(
            self.root, "Confirm install",
            f"FROM\n  {folder}\n\nINTO\n  {self.sfx}\n\nREPLACING\n{lines}\n\n"
            "Originals are backed up first (one-time).", "ask", ask=True, yes="INSTALL"):
            return

        need = 0
        for n, p in files.items():
            need += os.path.getsize(p)
            dst = os.path.join(self.sfx, n)
            bak = os.path.join(self.sfx, BACKUP_DIRNAME, n)
            if os.path.isfile(dst) and not os.path.exists(bak):
                need += os.path.getsize(dst)
        try:
            if shutil.disk_usage(self.sfx).free < need * 1.05:
                show_dialog(self.root, "Not enough space",
                            f"Need about {fmt_size(need)} free on that drive.", "error")
                return
        except OSError:
            pass

        self._run_worker(lambda: install_files(self.sfx, files, self._q_log, self._q_progress), "Install")

    def on_restore(self):
        if not self.sfx or self.busy or not self._check_ready():
            return
        if not show_dialog(self.root, "Restore originals",
                           "Put the original RESIDENT.rpf / WEAPONS_PLAYER.rpf back from the backup?",
                           "ask", ask=True, yes="RESTORE"):
            return
        self._run_worker(lambda: restore_files(self.sfx, self._q_log, self._q_progress), "Restore")

    # ---- background work --------------------------------------------------
    def _q_log(self, msg):
        self.q.put(("log", msg))

    def _q_progress(self, label, done, total):
        self.q.put(("progress", label, done, total))

    def _run_worker(self, fn, what):
        self.busy = True
        self.set_status("WORKING ...", VIOLET)
        self._update_buttons()
        self.progress.set(0)

        def work():
            try:
                names = fn()
                self.q.put(("done", what, names))
            except PermissionError:
                self.q.put(("error", "Permission denied. Close GTA V and the launcher, and make sure "
                                     "this app is running as administrator."))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log(item[1])
                elif kind == "progress":
                    _, label, done, total = item
                    self.progress.set((done / total * 100) if total else 0)
                    self.progress_label.configure(text=f"{label}:  {fmt_size(done)} / {fmt_size(total)}")
                elif kind == "done":
                    _, what, names = item
                    self.busy = False
                    self.progress.set(100)
                    self.progress_label.configure(text="")
                    if names:
                        self.log(f"{what} complete: {', '.join(names)}", "ok")
                        self.set_status(f"{what.upper()} COMPLETE", GREEN)
                        show_dialog(self.root, f"{what} complete", "\n".join(f"\u25c6 {n}" for n in names))
                    else:
                        self.log("Nothing to do (no backups found).", "warn")
                        self.set_status("READY", CYAN)
                    self._update_buttons()
                elif kind == "error":
                    self.busy = False
                    self.progress.set(0)
                    self.progress_label.configure(text="")
                    self.log(f"ERROR: {item[1]}", "err")
                    self.set_status("ERROR", RED)
                    show_dialog(self.root, "Something went wrong", item[1], "error")
                    self._update_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


# ============================================================================
# Embedded window icon (128px PNG of the LSFX mark)
# ============================================================================
ICON_PNG_B64 = (
    "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"
)


def set_window_icon(root):
    try:
        img = tk.PhotoImage(data=ICON_PNG_B64)
        root.iconphoto(True, img)
        root._icon_ref = img          # keep a reference so it isn't garbage-collected
    except Exception:
        pass


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)          # crisp on high-DPI
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Leggy.SoundpackSwap")
        except Exception:
            pass

    # create the config file on first run
    cfg = load_config()
    if not os.path.exists(config_path()):
        save_config(cfg)

    root = tk.Tk()
    init_ui_metrics(root)
    set_window_icon(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
