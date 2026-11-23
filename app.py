from collections import Counter
from datetime import datetime, timezone
import base64
import ctypes
from ctypes import wintypes
import functools
import glob
import http.server
import importlib
import importlib.util
import io
import itertools
import json
import os
import platform
import queue
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
import winreg

HERE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if HERE_DIR not in sys.path:
    sys.path.insert(0, HERE_DIR)
LOCAL_DEPS = os.path.join(HERE_DIR, 'deps')
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)
if os.path.isdir(r'D:\mmacro_deps') and r'D:\mmacro_deps' not in sys.path:
    sys.path.insert(0, r'D:\mmacro_deps')

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')      # numpy reserves memory per core; low-RAM PCs fail to open


def dpi_aware():
    """Real pixels on every monitor. Must run before any window exists."""
    for aware in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
                  lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0,
                  lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            if aware():
                return
        except (AttributeError, OSError):
            pass


dpi_aware()


def _unblock_zone_identifiers():
    """Strip Windows Mark-of-the-Web (:Zone.Identifier) from bundled DLLs so pythonnet/WebView2 load cleanly."""
    try:
        delete_w = ctypes.windll.kernel32.DeleteFileW
    except (AttributeError, OSError):
        return
    roots = {HERE_DIR}
    if getattr(sys, 'frozen', False):
        roots.add(os.path.dirname(os.path.abspath(sys.executable)))
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for cur, _, files in os.walk(root):
            for name in files:
                if name.lower().endswith(('.dll', '.exe', '.pyd', '.config')):
                    try:
                        delete_w(os.path.join(cur, name) + ':Zone.Identifier')
                    except Exception:
                        pass


_unblock_zone_identifiers()

REQUIRED_PACKAGES = (
    ('numpy', 'numpy>=1.24,<2.4'),
    ('PIL', 'pillow>=10.0'),
    ('pynput', 'pynput>=1.7'),
    ('psutil', 'psutil>=5.9'),
    ('mousekey', 'mousekey>=1.0'),
    ('cv2', 'opencv-python>=4.8'),
    ('rapidocr_onnxruntime', 'rapidocr_onnxruntime>=1.4,<1.5'),
    ('webview', 'pywebview>=5.0'),
)

SYSTEM_RUNTIMES = {
    'WebView2': (
        'Microsoft Edge WebView2 Runtime',
        'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
        'MicrosoftEdgeWebview2Setup.exe',
        ['/silent', '/install'],
    ),
    'NetFx48': (
        'Microsoft .NET Framework 4.8',
        'https://go.microsoft.com/fwlink/?linkid=2088631',
        'ndp48-web.exe',
        ['/q', '/norestart'],
    ),
    'VCRedist': (
        'Microsoft Visual C++ Redistributable (x64)',
        'https://aka.ms/vs/17/release/vc_redist.x64.exe',
        'vc_redist.x64.exe',
        ['/install', '/passive', '/norestart'],
    ),
}


def _has_webview2():
    guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (
            r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s' % guid,
            r'SOFTWARE\Microsoft\EdgeUpdate\Clients\%s' % guid,
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    pv, _ = winreg.QueryValueEx(k, 'pv')
                    if str(pv or '').strip() and str(pv).strip() != '0.0.0.0':
                        return True
            except OSError:
                pass
    return False


def _has_netfx():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as k:
            rel, _ = winreg.QueryValueEx(k, 'Release')
            return int(rel) >= 461808
    except OSError:
        return False


def _has_vcredist():
    sys32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
    return os.path.isfile(os.path.join(sys32, 'vcruntime140_1.dll')) and os.path.isfile(os.path.join(sys32, 'msvcp140.dll'))


def detect_missing_requirements():
    """Return (missing_pip_specs, missing_system_runtime_keys) on this PC."""
    missing_pkgs = []
    if not getattr(sys, 'frozen', False):
        for mod, spec in REQUIRED_PACKAGES:
            try:
                if importlib.util.find_spec(mod) is None:
                    missing_pkgs.append(spec)
            except Exception:
                missing_pkgs.append(spec)
    missing_sys = []
    if not _has_webview2():
        missing_sys.append('WebView2')
    if not _has_netfx():
        missing_sys.append('NetFx48')
    if not _has_vcredist():
        missing_sys.append('VCRedist')
    return missing_pkgs, missing_sys


def ensure_dependencies(prompt_fn=None):
    """Detect missing Python packages or Windows runtimes, ask permission first, then install."""
    missing_pkgs, missing_sys = detect_missing_requirements()
    if not missing_pkgs and not missing_sys:
        return True
    lines = ['Veyra detected missing components on this PC:\n']
    for spec in missing_pkgs:
        lines.append('  • Python package: %s' % spec)
    for key in missing_sys:
        lines.append('  • %s' % SYSTEM_RUNTIMES[key][0])
    lines.append('\nWould you like Veyra to download and install them now?')
    ask = prompt_fn or (lambda title, msg: ctypes.windll.user32.MessageBoxW(0, msg, title, 0x04 | 0x20) == 6)
    if not ask('Veyra - Missing Requirements', '\n'.join(lines)):
        return False
    try:
        if missing_pkgs:
            os.makedirs(LOCAL_DEPS, exist_ok=True)
            if LOCAL_DEPS not in sys.path:
                sys.path.insert(0, LOCAL_DEPS)
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--upgrade', '--target', LOCAL_DEPS, *missing_pkgs],
                check=True,
            )
            importlib.invalidate_caches()
        for key in missing_sys:
            label, url, fname, flags = SYSTEM_RUNTIMES[key]
            dst = os.path.join(tempfile.gettempdir(), fname)
            urllib.request.urlretrieve(url, dst)
            subprocess.run([dst, *flags], check=True)
            try:
                os.remove(dst)
            except OSError:
                pass
    except Exception as e:
        dark_message('Veyra - Setup Failed',
                     'Automatic installation failed:\n\n%s\n\nPlease check your internet connection or install manually.' % e)
        return False
    return True


if not ensure_dependencies():
    sys.exit(0)

try:
    from pynput import keyboard  # noqa: E402
    from PIL import Image  # noqa: E402
    import webview  # noqa: E402
    import game  # noqa: E402
    import eden  # noqa: E402
    import fishing  # noqa: E402
    import merchant as shop  # noqa: E402
    import points  # noqa: E402
    import clipper  # noqa: E402
    import equip  # noqa: E402
    import selector  # noqa: E402
    import potions  # noqa: E402
    import merchant_cache  # noqa: E402
    import support_link  # noqa: E402
    import discord_presence  # noqa: E402
    from calibrator_ui import ToastService  # noqa: E402
except Exception as e:          # numpy's DLLs raise OSError, not ImportError
    why = str(e).strip().splitlines()[-1] if str(e).strip() else type(e).__name__
    try:
        folder = os.path.join(os.getenv('LOCALAPPDATA') or os.path.expanduser('~'), 'Veyra')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'startup_error.txt'), 'w', encoding='utf-8') as f:
            f.write('%s\n\nexe: %s\n' % (e, sys.executable))
    except OSError:
        pass
    dark_message('Veyra', 'The macro could not load one of its own files.\n\n%s\n\n'
                 'Most often an antivirus removed it, or the PC is out of memory:\n'
                 '  - add the macro to your antivirus exclusions and download it again\n'
                 '  - close other programs, then open it again\n%s\n'
                 'Send this message to Manas if it keeps happening.'
                 % (why, '  - on Windows N or KN, install the Media Feature Pack (Settings > Apps '
                    '> Optional features)\n' if 'DLL load failed' in why else ''))
    sys.exit(1)

TEXT = '#f1f5f9'
DIM = '#58677a'
ACCENT = '#38bdf8'
GOOD = '#22d3ee'
WARN = '#fbbf24'
BAD = '#f87171'

BIOMES_DIR = os.path.join(HERE_DIR, 'biomes')
os.makedirs(BIOMES_DIR, exist_ok=True)

TITLE = 'Veyra'
VERSION = '1.0.4'

# Unified developer-mode passkey (baked into the .exe; session-only unlock).
# Only the person who knows this key can open Developer Mode.
DEVELOPER_PASSKEY = 'veyradevmode262010'


def dark_message(title, message):
    """Dark-themed message box — Windows MessageBoxW ignores the app theme."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
        win.title(title)
        win.configure(bg='#0b0d12')
        win.resizable(False, False)
        win.attributes('-topmost', True)
        w, h = 440, 180
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
        win.geometry('%dx%d+%d+%d' % (w, h, x, y))
        tk.Label(win, text=message, bg='#0b0d12', fg='#ffffff', wraplength=390,
                 justify='left', font=('Segoe UI', 10)).pack(padx=22, pady=22, expand=True)
        btn = tk.Button(win, text='OK', width=10, bg='#1e293b', fg='#ffffff',
                        activebackground='#334155', activeforeground='#ffffff',
                        relief='flat', command=win.destroy)
        btn.pack(pady=(0, 18))
        win.transient(root)
        win.grab_set()
        btn.focus_set()
        win.bind('<Return>', lambda _e: win.destroy())
        win.wait_window()
        root.destroy()
    except Exception:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
REPO_SLUG = os.environ.get('MMACRO_REPO') or 'ahmed10920/veyra-macro'
RELEASE_PAGE = f'https://github.com/{REPO_SLUG}/releases'
RELEASES = os.environ.get('MMACRO_UPDATE_URL') or f'https://api.github.com/repos/{REPO_SLUG}/releases?per_page=5'
SOURCE_VERSION_URL = os.environ.get('MMACRO_SOURCE_VERSION_URL') or f'https://raw.githubusercontent.com/{REPO_SLUG}/main/app.py'
SOURCE_ZIP_URL = os.environ.get('MMACRO_SOURCE_ZIP_URL') or f'https://github.com/{REPO_SLUG}/archive/refs/heads/main.zip'
INVITE = 'https://discord.gg/oppression'
ITEM_EVERY_S = {game.SC: 20 * 60, game.BR: 35 * 60}
ITEM_RETRY_S = 5 * 60
ITEM_SPARE_S = 15
RUNE = 'cursed rune fragment'
RUNE_BIOMES = ('PUMPKIN MOON', 'GRAVEYARD')
RUNE_TRIES, RUNE_RETRY_S = 2, 60
SELECTOR_RETRY_S = 5 * 60
POP_BIOMES = ('GLITCHED', 'DREAMSPACE', 'CYBERSPACE')
HOLD_BIOMES = ('GLITCHED', 'DREAMSPACE', 'CYBERSPACE', 'THE RADIANT FOREST', 'SINGULARITY', 'BLOOD RAIN',
               'GRAVEYARD', 'PUMPKIN MOON')
SELECTOR_REFUSED_S = 11 * 60
SELECTOR_REFUSALS = 3
SELECTOR_MISSES = 3
REJOIN_WAITS = (15, 30, 60)
IN_GAME_S = 150
OUT_S = 180
PLAY_EVERY_S = 20
STALL_S = 600
SLEPT_S = 60
WATCH_EVERY = 0.5
CRASH_WAITS = (30, 60, 120, 300)
LOOK_EVERY_S = 1.5
AFK_EVERY_S = 300
PC_RESUME_IDLE_S = 10.0          # seconds without mouse/keyboard input before a PC pause auto-resumes
EDEN_RETRY_S = 60
EDEN_GIVE_UP_S = 30 * 60
EDEN_AFK_REST_S = 900
CRACK = 'portable crack'
CRACK_S = 3.0
EQUIP_RETRY_S = 5 * 60
JOIN_NONE_S = 300
EQUIP_GAP_S = 90
MERCHANT_STALE_S = 150
LATE_RESULT_S = 8
DAILY_UTC_S = 22 * 3600 + 60
DAILY_LATE_S = 90
STRAY_EVERY_S = 20
MERCHANT_REPEAT_S = 600
BOSS_BIOMES = {'Sinister S': 'GRAVEYARD', 'Sinister P.': 'PUMPKIN MOON'}
BOSS_EARLY_S = 5
BOSS_HERE_S = 180                  # how long a confirmed boss counts as present
MERCHANT_TRIES = 3

LOG_MAX = 4 << 20

_log_lock = threading.Lock()
_recent_logs = []


def _sanitize_log_text(text):
    t = str(text or '')
    t = re.sub(r'https://discord(?:app)?\.com/api/webhooks/[^\s"\'<>]+', 'https://discord.com/api/webhooks/[REDACTED]', t)
    t = re.sub(r'(privateServerLinkCode=|linkCode=)[A-Za-z0-9_-]+', r'\1[REDACTED]', t)
    return t


def note(tag, msg):
    """Append a line to logs.txt; never raises."""
    with _log_lock:
        _note(tag, msg)


def _note(tag, msg):
    ts = time.strftime('%H:%M:%S')
    _recent_logs.append((ts, tag, str(msg)))
    if len(_recent_logs) > 400:
        del _recent_logs[:100]
    try:
        os.makedirs(game.APP_DIR, exist_ok=True)
        path = os.path.join(game.APP_DIR, 'logs.txt')
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            with open(path, 'rb') as f:
                f.seek(-LOG_MAX // 2, os.SEEK_END)
                tail = f.read().split(b'\n', 1)[-1]
            with open(path, 'wb') as f:
                f.write(tail)
        with open(path, 'a', encoding='utf-8') as f:
            f.write('%s  [%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), tag, msg))
    except OSError:
        pass


CACHE_UNREADABLE = ('Roblox\'s cache (rbx-storage.db) could not be read, so merchants and the boss are not seen. Close '
                    'Roblox, delete the rbx-storage.db files in %LOCALAPPDATA%\\Roblox (Roblox makes them again) and '
                    'open Roblox normally, not as administrator.')
STORE_ROBLOX = ('Merchant and boss detection read Roblox\'s cache (rbx-storage.db), which was not found on this PC. '
                'It works with Roblox from roblox.com, Bloxstrap or Fishstrap.\n\nOpen Roblox once, or leave detection '
                'off.')


class _ToLog:
    """The windowed exe has no console, so print goes to logs.txt."""

    def __init__(self):
        self.buf = ''

    def write(self, s):
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            if line.strip():
                note('out', line.strip())

    def flush(self):
        pass


if sys.stdout is None or type(sys.stdout).__name__ == 'NullWriter':
    sys.stdout = sys.stderr = _ToLog()


def clip_floor(text):
    """'from 1 in' as a number; junk means 1 in a billion, never every roll."""
    try:
        return max(1, int(float(re.sub(r'[,\s_]', '', text or ''))))
    except (ValueError, OverflowError):
        return 1_000_000_000


def device_line():
    """PC info for bug reports; never raises."""
    try:
        import platform
        u = ctypes.windll.user32
        w = game.find_roblox_window()
        client = 'no Roblox window'
        if w is not None:
            x, y, cw, ch = game.client_rect(w)
            s = game.window_scale(w)
            client = '%dx%d at %d,%d (%s%s)' % (cw, ch, x, y, game.awareness(w.hwnd), '' if s == 1 else
                                               ', laid out at %dx%d, stretched %d%% by Windows'
                                               % (round(cw / s), round(ch / s), round(s * 100)))
        dpi = u.GetDpiForWindow(w.hwnd) if w is not None else 0
        prof = game.machine_profile()
        hw = '%s GB, %s cores' % (prof.get('ram_gb') or '?', prof.get('cores') or '?')
        if prof.get('cpu'):
            hw += ', ' + prof['cpu']
        if prof.get('lite'):
            hw += ', light mode'
        return ('v%s, Windows %s, %s, screen %dx%d (all %dx%d), Roblox client %s, scale %d%%%s, Veyra %s%s%s'
                % (VERSION, platform.version(), hw, u.GetSystemMetrics(0), u.GetSystemMetrics(1), u.GetSystemMetrics(78),
                   u.GetSystemMetrics(79), client, round(dpi * 100 / 96), game.in_dpi(game.REAL, display_line, w),
                   game.awareness(), '' if u.IsProcessDPIAware() else ' (a compatibility setting on the exe?)',
                   ', mouse buttons swapped' if u.GetSystemMetrics(23) else ''))
    except Exception as e:
        return 'v%s, device not read: %s: %s' % (VERSION, type(e).__name__, e)


def display_line(w):
    """Real monitor resolution and scale, outside our own DPI view."""
    try:
        u, g = ctypes.WinDLL('user32'), ctypes.WinDLL('gdi32')
        u.GetDC.restype = u.MonitorFromWindow.restype = ctypes.c_void_p
        u.GetDC.argtypes, u.ReleaseDC.argtypes = [ctypes.c_void_p], [ctypes.c_void_p, ctypes.c_void_p]
        g.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        hdc = u.GetDC(None)
        real = '%dx%d' % (g.GetDeviceCaps(hdc, 118), g.GetDeviceCaps(hdc, 117))
        u.ReleaseDC(None, hdc)
        mon = u.MonitorFromWindow(w.hwnd if w is not None else None, 2)
        x, y = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(x), ctypes.byref(y))
        return ' (display %s, monitor scale %d%%%s)' % (real, round(x.value * 100 / 96),
                                                        ', remote desktop' if u.GetSystemMetrics(0x1000) else '')
    except Exception as e:
        return ' (display not read: %s)' % type(e).__name__


# ================================================================ Discord & Biomes
# One application for every copy of Veyra. Users never create their own.
DISCORD_APP_ID = '1540076645262565507'
FOOTER = 'Download the latest Veyra release'
TESTERS = ('Forsakened One', 'Daniel', 'Slpxy', 'Nova', 'imsomeone', 'Evil Talin Siran', 'ghost', '███\'█_Guest',
           'delta_likes_cats_xd')
EMBED_GOOD, EMBED_BAD, EMBED_INFO, EMBED_BIOME, EMBED_GOLD = 0x57F287, 0xED4245, 0x8A8F98, 0x58B9FF, 0xFFD700
BIOME_COLOURS = {'NORMAL': 0x4A90E2, 'WINDY': 0x7ED6DF, 'RAINY': 0x3898EC, 'SNOWY': 0xA8D8FF, 'SAND STORM': 0xE0A85A,
                 'HELL': 0xE74C3C, 'STARFALL': 0x6C7BFF, 'CORRUPTION': 0x9B59B6, 'NULL': 0x64748B, 'HEAVEN': 0xFCD34D,
                 'LOST FOREST': 0x2ECC71, 'GLITCHED': 0x3BE37A, 'DREAMSPACE': 0xE86AF0, 'CYBERSPACE': 0x2FD3FF,
                 'SINGULARITY': 0xF7A91D, 'THE RADIANT FOREST': 0xFDE047,
                 'GRAVEYARD': 0x6B7280, 'PUMPKIN MOON': 0xF97316, 'BLOOD RAIN': 0xDC2626}

BIOME_RATES = {
    'NORMAL': '1/1/sec',
    'WINDY': '1/500/sec',
    'SNOWY': '1/600/sec',
    'RAINY': '1/750/sec',
    'SAND STORM': '1/3,000/sec',
    'HELL': '1/6,666/sec',
    'STARFALL': '1/7,500/sec',
    'HEAVEN': '1/9,000/sec',
    'CORRUPTION': '1/9,000/sec',
    'NULL': '1/13,333/sec',
    'GLITCHED': '1/30,000/sec',
    'DREAMSPACE': '1/5,000,000/sec',
    'CYBERSPACE': '1/1,000,000/sec',
    'LOST FOREST': '1/15,000/sec',
    'SINGULARITY': 'Event Biome',
    'THE RADIANT FOREST': 'Event Biome',
    'GRAVEYARD': 'Event Biome',
    'PUMPKIN MOON': 'Event Biome',
    'BLOOD RAIN': 'Event Biome',
}

PING = {'GLITCH', 'GLITCHED', 'DREAMSPACE', 'CYBERSPACE'}
CLIP_COLOURS = ((10 ** 10, 0xFFC94D), (10 ** 9, 0xB38CFF), (10 ** 8, 0x58B9FF), (1, EMBED_GOOD))
MERCHANT_COLOURS = {'Rin': 0xFF8C00, 'Mari': 0xFFD700, 'Jester': 0x9B59B6, 'Sinister S': 0x6A0DAD, 'Sinister P.': 0xFF7A00}
MERCHANT_BADGES = {'Rin': '🟠', 'Mari': '🟡', 'Jester': '🟣', 'Sinister S': '☠️', 'Sinister P.': '☠️'}
MERCHANT_STATUS = {'Sinister S': 'BOSS SIGNAL LOCKED', 'Sinister P.': 'BOSS SIGNAL LOCKED'}
MERCHANT_FACES = {'Rin': 'merchant_rin.png', 'Jester': 'merchant_jester.png', 'Mari': 'merchant_mari.png'}
_merchant_icons = {}


def merchant_icon(name=''):
    """Bytes of that merchant's portrait (boss compass emblem for bosses), cached once."""
    boss = name in BOSS_BIOMES
    key = 'boss_alert.png' if boss else MERCHANT_FACES.get(name, 'merchant_tracker.png')
    if key not in _merchant_icons:
        folder = 'ui' if boss else os.path.join('ui', 'items')
        try:
            with open(os.path.join(HERE_DIR, folder, key), 'rb') as f:
                _merchant_icons[key] = f.read()
        except OSError:
            _merchant_icons[key] = None
    return _merchant_icons[key]


def merchant_card(name, status='IN YOUR SERVER', action='TELEPORT ARMED'):
    """The radar terminal strip that makes merchant posts instantly recognisable."""
    width = 31
    rows = [
        '╔' + '═' * width + '╗',
        '║' + ' VEYRA // MERCHANT RADAR '.center(width) + '║',
        '╠' + '═' * width + '╣',
        '║' + (' TARGET  : ' + (name or 'UNKNOWN').upper()).ljust(width)[:width] + '║',
        '║' + (' STATUS  : ' + status).ljust(width)[:width] + '║',
        '║' + (' ACTION  : ' + action).ljust(width)[:width] + '║',
        '╚' + '═' * width + '╝',
    ]
    return '```\n' + '\n'.join(rows) + '\n```'


def base_biome(name):
    """Event biomes may come as '(Event) Graveyard'."""
    return re.sub(r'^\W*EVENT\W*', '', (name or '').upper()).strip()


def biome_colour(name):
    return BIOME_COLOURS.get(base_biome(name), EMBED_BIOME)


def biome_rgb(name):
    c = biome_colour(name)
    return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)


def biome_slug(name):
    return re.sub(r'[^a-z0-9]+', '_', base_biome(name).lower()).strip('_')


def find_biome_photo(name):
    """Return path to custom biome photo in `biomes/` folder if present, else None."""
    slug = biome_slug(name)
    if not slug:
        return None
    for ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
        for cand in (slug + ext, slug.replace('_', '') + ext, slug.replace('_', ' ') + ext):
            p = os.path.join(BIOMES_DIR, cand)
            if os.path.isfile(p):
                return p
    return None


def biome_web_path(name):
    """Return URL path rooted at HERE_DIR for the WebView UI."""
    p = find_biome_photo(name)
    if p:
        rel = os.path.relpath(p, HERE_DIR).replace('\\', '/')
        return '/' + rel
    return '/biomes/normal.png'


def biome_png(name):
    """PNG bytes of the biome photo for Discord webhook embeds, or None."""
    path = find_biome_photo(name)
    if not path:
        return None
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return data
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert('RGBA').save(buf, 'PNG')
        return buf.getvalue()
    except Exception:
        return None


def next_daily(now=None):
    """Next 22:01 GMT in epoch seconds; any time zone."""
    now = time.time() if now is None else now
    t = now // 86400 * 86400 + DAILY_UTC_S
    return t if t > now else t + 86400


def rune_biome(name, picked):
    """The picked biome `name` is, or None."""
    squashed = base_biome(name).replace(' ', '')
    return next((b for b in picked if b.replace(' ', '') == squashed), None)


def is_rare(name):
    squashed = (name or '').upper().replace(' ', '')
    return any(p in squashed for p in PING)


class BiomeWatcher:
    """Tracks biome changes; rare ones alert once per cooldown, even at start."""

    def __init__(self, cooldown=300.0):
        self.cooldown = cooldown
        self.current = None
        self._last_alert = {}

    def feed(self, name, now=None):
        """(biome, alert) on a change, else None."""
        if not name:
            return None
        name = name.upper()
        if name == self.current:
            return None
        now = time.time() if now is None else now
        self.current = name
        last = self._last_alert.get(name)
        alert = is_rare(name) and (last is None or now - last >= self.cooldown)
        if alert:
            self._last_alert[name] = now
        return name, alert


def pretty(name):
    return ' '.join(w.capitalize() for w in (name or '').split())


def clean_url(text):
    return ''.join((text or '').split())


def _file_mime(name):
    ext = os.path.splitext(name or '')[1].lower()
    return {
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.webp': 'image/webp',
    }.get(ext, 'application/octet-stream')


def _build_webhook_request(webhook_url, payload, files=None):
    """Build a urllib Request for JSON or multipart Discord webhook delivery."""
    if not files:
        body, kind = json.dumps(payload).encode('utf-8'), 'application/json'
    else:
        b = uuid.uuid4().hex
        body = ('--%s\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                'Content-Type: application/json\r\n\r\n%s\r\n' % (b, json.dumps(payload))).encode('utf-8')
        for i, (name, png) in enumerate(files.items()):
            body += ('--%s\r\nContent-Disposition: form-data; name="files[%d]"; filename="%s"\r\n'
                     'Content-Type: %s\r\n\r\n' % (b, i, name, _file_mime(name))).encode('utf-8') + png + b'\r\n'
        body += ('--%s--\r\n' % b).encode('utf-8')
        kind = 'multipart/form-data; boundary=' + b
    return urllib.request.Request(webhook_url, data=body,
                                  headers={'Content-Type': kind, 'User-Agent': 'SolsBiomeLogger/1.0'})


def _post(webhook_url, payload, files=None, on_error=None):
    """Posts on a thread; `files` {name: png} go as attachment://name."""
    err_cb = on_error or (lambda m: note('webhook', m))
    payload.setdefault('components', [{
        'type': 1,
        'components': [{'type': 2, 'style': 5, 'label': 'Download Veyra', 'url': RELEASE_PAGE}],
    }])

    def run():
        try:
            req = _build_webhook_request(webhook_url, payload, files)
            urllib.request.urlopen(req, timeout=30).close()
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', errors='replace')[:200]
            except Exception:
                pass
            err_cb('webhook HTTP %s: %s %s' % (e.code, e.reason, detail))
        except (urllib.error.URLError, OSError, ValueError) as e:
            err_cb('webhook failed: %s: %s' % (type(e).__name__, e))
    threading.Thread(target=run, daemon=True).start()


def _timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())


_logo = []


def logo_png():
    if not _logo:
        try:
            with open(os.path.join(HERE_DIR, 'ui', 'brand_bh.png'), 'rb') as f:
                _logo.append(f.read())
        except OSError:
            _logo.append(None)
    return _logo[0]


_mark = []
_mark_gif = []


def mark_png():
    """Finished intro mark, shown beside the run-summary author name."""
    if not _mark:
        try:
            with open(os.path.join(HERE_DIR, 'ui', 'veyra-mark.png'), 'rb') as f:
                _mark.append(f.read())
        except OSError:
            _mark.append(None)
    return _mark[0]


def mark_gif():
    """Intro draw-on of the current mark. Discord animates this in the embed thumbnail."""
    if not _mark_gif:
        try:
            with open(os.path.join(HERE_DIR, 'ui', 'veyra-anim.gif'), 'rb') as f:
                _mark_gif.append(f.read())
        except OSError:
            _mark_gif.append(None)
    return _mark_gif[0]


def _summary_line(text, width=33):
    return '║' + str(text)[:width].ljust(width) + '║'


def _summary_stat(label, value, width=33):
    left = ('  ' + label)[:12].ljust(12)
    return _summary_line(left + str(value), width)


def run_summary_card(player, length, ended, fish, points, merchants, bosses):
    bar = '═' * 33
    lines = [
        '╔' + bar + '╗',
        _summary_line('VEYRA // RUN SUMMARY'.center(33)),
        '╠' + bar + '╣',
        _summary_stat('PLAYER', player or '—'),
        _summary_stat('LENGTH', length or '—'),
        _summary_stat('ENDED', ended or '—'),
        '╠' + bar + '╣',
        _summary_stat('FISH', '{:,}'.format(int(fish))),
        _summary_stat('POINTS', '{:,}'.format(int(points))),
        _summary_stat('MERCHANTS', '{:,}'.format(int(merchants))),
        _summary_stat('BOSSES', '{:,}'.format(int(bosses))),
        '╚' + bar + '╝',
    ]
    return '```\n' + '\n'.join(lines) + '\n```'


def post_run_summary(webhook_url, fish, points, merchants, bosses, player='', length='', ended='', server=''):
    """Sky-blue run card. The thumbnail plays the intro draw-on of the current mark."""
    if not webhook_url:
        return
    embed = {
        'author': {'name': 'Veyra · Run Summary', 'url': RELEASE_PAGE},
        'title': 'Run complete',
        'description': run_summary_card(player, length, ended, fish, points, merchants, bosses)[:4000],
        'color': 0x38BDF8,
        'fields': [{'name': 'Private server', 'value': server_field(server), 'inline': False}],
        'footer': {'text': 'Veyra · v%s' % VERSION},
        'timestamp': _timestamp(),
    }
    files = {}
    png = mark_png()
    gif = mark_gif()
    if png:
        files['logo.png'] = png
        embed['author']['icon_url'] = 'attachment://logo.png'
    if gif:
        files['veyra.gif'] = gif
        embed['thumbnail'] = {'url': 'attachment://veyra.gif'}
    elif png:
        files['brandthumb.png'] = png
        embed['thumbnail'] = {'url': 'attachment://brandthumb.png'}
    _post(webhook_url, {'embeds': [embed]}, files)


def server_field(link):
    link = (link or '').strip()
    if not link:
        return 'No private server inserted'
    if link.startswith(('https://', 'http://')) and ' ' not in link:
        return '[Join](%s)' % link[:900]
    return link[:1000]


def post_event(webhook_url, title, description='', colour=EMBED_INFO, area='Macro', fields=(), ping='', shot=None,
               footer=True, thumb=None, merchant=None):
    if not webhook_url:
        return
    radar = area == 'Merchant' or bool(merchant)
    embed = {'author': {'name': 'Veyra · %s' % area, 'url': RELEASE_PAGE},
             'title': title[:256], 'color': colour, 'timestamp': _timestamp()}
    files = {}
    if logo_png():
        files['logo.png'] = logo_png()
        embed['author']['icon_url'] = 'attachment://logo.png'
    if radar:
        description = '```diff\n- MERCHANT RADAR // EVENT LOG\n```\n' + (description or '')
    if description:
        embed['description'] = description[:4000]
    if footer:
        fields = list(fields) + [(' ', '-# Veyra · v%s' % VERSION, False)]
    if fields:
        embed['fields'] = [{'name': n, 'value': str(v)[:1024] or '-', 'inline': i} for n, v, i in fields]
    if thumb:
        files['biome.png'] = thumb
        embed['thumbnail'] = {'url': 'attachment://biome.png'}
    elif not radar and logo_png():
        files['brandthumb.png'] = logo_png()
        embed['thumbnail'] = {'url': 'attachment://brandthumb.png'}
    elif radar:
        icon = merchant_icon(merchant or '')
        if icon:
            files['merchant.png'] = icon
            embed['image' if not shot else 'thumbnail'] = {'url': 'attachment://merchant.png'}
    if shot:
        files['shot.png'] = shot
        embed['image'] = {'url': 'attachment://shot.png'}
    _post(webhook_url, {'content': ping, 'embeds': [embed]}, files)


def capital(text):
    text = text or ''
    return text[:1].upper() + text[1:]


def mention(user_id):
    user_id = ''.join(c for c in user_id or '' if c.isdigit())
    return '<@%s>' % user_id if user_id else ''


def span(seconds):
    m = int(max(0, seconds)) // 60
    return '%dh %dm' % divmod(m, 60) if m >= 60 else '%dm' % m if m else '<1m'


def post_biome(webhook_url, name, ping=True, server='', running=None, here=None, sync=False):
    """Posts a Biome Started embed with right-side biome emblem and @everyone rare pings."""
    if not webhook_url:
        return
    now_ts = int(time.time())
    srv = (server or '').strip() or 'No private server inserted'
    fields = [{'name': 'Private Server', 'value': srv[:1024], 'inline': False}]
    if running is not None:
        fields += [
            {'name': 'Running', 'value': span(running), 'inline': True},
            {'name': 'This server', 'value': span(here), 'inline': True},
        ]
    embed = {
        'title': 'Biome Started - %s' % base_biome(name),
        'description': '<t:%d:T> (<t:%d:R>)' % (now_ts, now_ts),
        'color': biome_colour(name),
        'fields': fields,
        'footer': {'text': '%s | Release v%s' % (TITLE, VERSION)},
    }
    files = {}
    thumb = biome_png(name)
    if thumb:
        files['biome.png'] = thumb
        embed['thumbnail'] = {'url': 'attachment://biome.png'}
    do_ping = bool(ping) or is_rare(name)
    payload = {
        'content': '@everyone' if do_ping else '',
        'allowed_mentions': {'parse': ['everyone']} if do_ping else {'parse': []},
        'embeds': [embed],
    }
    if sync:
        urllib.request.urlopen(_build_webhook_request(webhook_url, payload, files), timeout=30).close()
    else:
        _post(webhook_url, payload, files)


def post_merchant(webhook_url, name, server='', watch=None, collect=False, how=''):
    """Merchant arrival: the radar-terminal card with that merchant's own logo."""
    if not webhook_url:
        return
    badge = '' if name == 'Sinister S' else MERCHANT_BADGES.get(name, '')
    status = MERCHANT_STATUS.get(name, 'MERCHANT SIGNAL LOCKED')
    colour = MERCHANT_COLOURS.get(name, EMBED_GOLD)
    now = int(time.time())
    action = 'TELEPORT ARMED' if collect else 'ALERT ONLY'
    title = 'Sinister boss has arrived in Graveyard' if name == 'Sinister S' else 'Sinister boss has arrived in Pumpkin Moon' if name == 'Sinister P.' else '%s %s' % (badge, status)
    description = '\n'.join([
        merchant_card(name, 'IN YOUR SERVER', action),
        '',
        '%s**%s** is here  ·  <t:%d:R>  ·  <t:%d:t>' % ((badge + ' ') if badge else '', name, now, now),
        ('-# Detected via %s' % how) if how else '-# Detected by the Merchant Radar',
    ])
    fields = [('Signal', 'Locked onto the server', True),
              ('Route', 'Teleport → Shop → Buy' if collect else 'Alert only', True),
              ('Private Server', server_field(server), False)]
    embed = {
        'author': {'name': 'Veyra · Merchant Radar', 'url': RELEASE_PAGE},
        'title': title,
        'description': description[:4000],
        'color': colour,
        'fields': [{'name': n, 'value': str(v)[:1024] or '-', 'inline': i} for n, v, i in fields],
        'footer': {'text': 'Veyra · v%s' % VERSION},
        'timestamp': _timestamp(),
    }
    files = {}
    if logo_png():
        files['logo.png'] = logo_png()
        embed['author']['icon_url'] = 'attachment://logo.png'
    icon = merchant_icon(name)
    if icon:
        files['merchant.png'] = icon
        embed['image'] = {'url': 'attachment://merchant.png'}
    _post(webhook_url, {'embeds': [embed]}, files)


def post_shot(webhook_url, title, colour, ping='', detail='', area='Macro', fields=(), merchant=None):
    if not webhook_url:
        return
    buf = io.BytesIO()
    Image.fromarray(game.grab(game.client_box((0.0, 0.0, 1.0, 1.0)))).save(buf, 'PNG')
    post_event(webhook_url, title, detail, colour, area, fields, mention(ping), buf.getvalue(), merchant=merchant)


_asset_cache = {}


def asset_png(name):
    """Bytes of an icon under ui/items, cached after first read."""
    if name not in _asset_cache:
        try:
            with open(os.path.join(HERE_DIR, 'ui', 'items', name), 'rb') as f:
                _asset_cache[name] = f.read()
        except OSError:
            _asset_cache[name] = None
    return _asset_cache[name]


def _screen_shot():
    """Current Roblox screen as PNG bytes, or None when the capture is unavailable."""
    try:
        buf = io.BytesIO()
        Image.fromarray(game.grab(game.client_box((0.0, 0.0, 1.0, 1.0)))).save(buf, 'PNG')
        return buf.getvalue()
    except Exception:
        return None


def post_alert(webhook_url, title, detail, colour, area, icon, emoji=''):
    """Small alert card with its own icon and a screenshot of the failure."""
    if not webhook_url:
        return
    embed = {
        'author': {'name': 'Veyra · %s' % area, 'url': RELEASE_PAGE},
        'title': ('%s %s' % (emoji, title)) if emoji else title,
        'description': detail or '-',
        'color': colour,
        'footer': {'text': 'Veyra · v%s' % VERSION},
        'timestamp': _timestamp(),
    }
    files = {}
    if logo_png():
        files['logo.png'] = logo_png()
        embed['author']['icon_url'] = 'attachment://logo.png'
    blob = asset_png(icon)
    if blob:
        files['alert.png'] = blob
        embed['thumbnail'] = {'url': 'attachment://alert.png'}
    shot = _screen_shot()
    if shot:
        files['shot.png'] = shot
        embed['image'] = {'url': 'attachment://shot.png'}
    _post(webhook_url, {'embeds': [embed]}, files)


_STATUS_ICONS = {'Macro started': '▶️', 'Macro stopped': '⏹️', 'Reconnecting': '🔄',
                 'Rejoined the private server': '✅', 'Could not rejoin': '⚠️'}


def post_status(webhook_url, title, detail='', colour=EMBED_INFO, area='Macro', username='', logo=False):
    """Start/stop/reconnect get a status card instead of the plain text embed."""
    if not webhook_url:
        return
    lines = []
    if username:
        lines.append('👤 **%s**' % username)
    if detail:
        lines.append(detail)
    if not lines:
        lines.append('Veyra is %s.' % ('running now' if colour == EMBED_GOOD else 'no longer running'))
    embed = {
        'author': {'name': 'Veyra · %s' % area, 'url': RELEASE_PAGE},
        'title': '%s %s' % (_STATUS_ICONS.get(title, '🔔'), title),
        'description': '\n'.join(lines)[:4000],
        'color': colour,
        'footer': {'text': 'Veyra · v%s' % VERSION},
        'timestamp': _timestamp(),
    }
    files = {}
    if logo_png():
        files['logo.png'] = logo_png()
        embed['author']['icon_url'] = 'attachment://logo.png'
        if logo:
            files['veyra.png'] = logo_png()
            embed['image'] = {'url': 'attachment://veyra.png'}
    _post(webhook_url, {'embeds': [embed]}, files)


def append_history(biome):
    try:
        import local_db
        local_db.add_history(time.time(), time.strftime('%Y-%m-%d %H:%M:%S'), biome)
    except Exception:
        pass


# ================================================================ HTTP Server & Roblox Profile
COUNTS_ORDER = (
    'GLITCHED', 'DREAMSPACE', 'CYBERSPACE', 'THE RADIANT FOREST', 'WINDY', 'RAINY',
    'SNOWY', 'SAND STORM', 'HELL', 'HEAVEN', 'STARFALL',
    'CORRUPTION', 'NULL', 'LOST FOREST', 'GRAVEYARD', 'PUMPKIN MOON',
    'BLOOD RAIN', 'SINGULARITY', 'NORMAL',
)


def detect_roblox_profile():
    """Return (username, userid) from Roblox LocalStorage or latest log."""
    local = os.getenv('LOCALAPPDATA') or ''
    app_stor = os.path.join(local, 'Roblox', 'LocalStorage', 'appStorage.json')
    uname, uid = None, None
    try:
        if os.path.isfile(app_stor):
            with open(app_stor, encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            raw_u = str(data.get('Username') or '').strip()
            raw_id = str(data.get('UserId') or '').strip()
            if raw_u:
                uname = raw_u
            if raw_id.isdigit() and int(raw_id) > 0:
                uid = raw_id
    except Exception:
        pass
    if not uid:
        try:
            latest = game.newest_log()
            if latest and os.path.isfile(latest):
                with open(latest, encoding='utf-8', errors='replace') as f:
                    m = re.search(r'userid[:=]\s*(\d+)', f.read(65536), re.I)
                    if m:
                        uid = m.group(1)
        except Exception:
            pass
    return uname or os.environ.get('USERNAME') or 'Player', uid or ''


def resolve_avatar_file(uid=''):
    """Return path to cached/custom Roblox avatar PNG, or brand logo."""
    candidates = [os.path.join(game.APP_DIR, 'avatar_custom.png')]
    if uid:
        candidates.append(os.path.join(game.APP_DIR, 'avatar_%s.png' % uid))
    candidates.extend([
        os.path.join(game.APP_DIR, 'avatar.png'),
        os.path.join(HERE_DIR, 'ui', 'brand_bh.png'),
    ])
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def seed_and_refresh_avatar(uid, on_ready=None, stop_fn=None, sync=False):
    """Fetch true Roblox user avatar (synchronously if sync=True, or in background with retry)."""
    if not uid:
        return
    dst = os.path.join(game.APP_DIR, 'avatar_%s.png' % uid)

    def _try_once():
        urls = (
            'https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds=%s&size=150x150&format=Png&isCircular=false' % uid,
            'https://thumbnails.roproxy.com/v1/users/avatar-headshot?userIds=%s&size=150x150&format=Png&isCircular=false' % uid,
        )
        for api_url in urls:
            try:
                req = urllib.request.Request(api_url, headers={'User-Agent': 'MMacroHUD/1.0'})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.load(r)
                items = data.get('data') or []
                img_url = items[0].get('imageUrl') if items else None
                if not img_url:
                    continue
                img_req = urllib.request.Request(img_url, headers={'User-Agent': 'MMacroHUD/1.0'})
                with urllib.request.urlopen(img_req, timeout=10) as ir:
                    raw = ir.read()
                if raw.startswith(b'\x89PNG'):
                    os.makedirs(game.APP_DIR, exist_ok=True)
                    with open(dst, 'wb') as out:
                        out.write(raw)
                    if on_ready:
                        on_ready()
                    return True
            except Exception as e:
                note('ui', 'avatar fetch failed (%s): %s: %s' % (uid, type(e).__name__, e))
                continue
        return False

    def _run():
        if _try_once():
            return
        while not (stop_fn and stop_fn()):
            time.sleep(15)
            if stop_fn and stop_fn():
                return
            if _try_once():
                return

    if sync:
        if _try_once():
            return
    threading.Thread(target=_run, daemon=True).start()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    avatar_uid = ''

    def do_GET(self):
        clean_path = self.path.split('?', 1)[0]
        if clean_path == '/avatar.png':
            fpath = resolve_avatar_file(self.avatar_uid)
            if fpath:
                try:
                    with open(fpath, 'rb') as f:
                        raw = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                except OSError:
                    pass
        super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        super().end_headers()

    def log_message(self, format, *args):
        pass


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start_ui_server(avatar_uid=''):
    _QuietHandler.avatar_uid = avatar_uid or ''
    handler = functools.partial(_QuietHandler, directory=HERE_DIR)
    srv = _ThreadingServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _find_own_hwnd(title=TITLE):
    u32 = ctypes.windll.user32
    me = os.getpid()
    found = []

    def cb(h, _):
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == me and u32.IsWindowVisible(h):
            n = u32.GetWindowTextLengthW(h)
            if n > 0:
                found.append(h)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else u32.FindWindowW(None, title)


def _set_hwnd_topmost(hwnd, on_top):
    if not hwnd:
        return False
    u32 = ctypes.WinDLL('user32', use_last_error=True)
    u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u32.SetWindowPos.restype = ctypes.c_bool
    insert_after = ctypes.c_void_p(-1 if on_top else -2)
    return bool(u32.SetWindowPos(ctypes.c_void_p(hwnd), insert_after, 0, 0, 0, 0, 0x0013))


def _dark_titlebar_hwnd(hwnd):
    if not hwnd:
        return
    try:
        val = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attr, ctypes.byref(val), ctypes.sizeof(val)
            )
    except Exception:
        pass


def normalize_cfg(raw):
    cfg = dict(game.DEFAULTS)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg.setdefault('silent_merchant', False)
    cfg.setdefault('boss_on', False)
    cfg.setdefault('roblox_on_top', False)
    cfg.setdefault('skip_intro', True)
    # Older builds used one switch for merchants and Halloween bosses. Keep bosses
    # on for anyone who already had that switch on, until they turn the new one off.
    if isinstance(raw, dict) and 'boss_on' not in raw:
        cfg['boss_on'] = bool(raw.get('merchant_on'))
    cfg.setdefault('rune_on', False)
    cfg.setdefault('rune_biomes', list(RUNE_BIOMES))
    cfg.setdefault('rune_after', 0)
    cfg.setdefault('selector_on', False)
    cfg.setdefault('selector_biome', 'HEAVEN')
    cfg.setdefault('pop_on', False)
    cfg.setdefault('pop_items', {})
    cfg.setdefault('clip_key_on', False)
    cfg.setdefault('clip_keys', 'Win+Alt+G')
    cfg.setdefault('equip_on', False)
    cfg.setdefault('equip_aura', '')
    cfg.setdefault('roblox_username', '')
    cfg.setdefault('roblox_uid', '')
    cfg.setdefault('tutorial_seen', False)
    cfg.setdefault('auto_update', True)
    cfg.setdefault('perf_mode', 'auto')
    cfg.setdefault('fx_on', True)
    if cfg.get('perf_mode') not in ('auto', 'lite', 'full'):
        cfg['perf_mode'] = 'auto'
    cfg.setdefault('collect_merchants', ['Jester', 'Mari', 'Rin'])
    if not isinstance(cfg.get('buy_items'), dict):
        cfg['buy_items'] = {}
    if not isinstance(cfg.get('pop_items'), dict):
        cfg['pop_items'] = {}
    if not isinstance(cfg.get('rune_biomes'), list):
        cfg['rune_biomes'] = list(RUNE_BIOMES)
    if not isinstance(cfg.get('collect_merchants'), list):
        cfg['collect_merchants'] = ['Jester', 'Mari', 'Rin']
    return cfg


# ================================================================ Automation Engine & WebView API
class MacroEngine:
    def __init__(self):
        self.running = False
        self.paused = False
        self.phase_text = 'idle'
        self.biome_stop = None
        self._last_hotkey = {}
        self._closing = False
        self._capturing = False
        self.updating = False
        self.ran = False
        self._preview_biome = None
        self._support = None
        self._support_lock = threading.Lock()
        # Developer Mode: locked at the start of every session (session-only unlock).
        self.developer_locked = True
        self.update_info = {'state': 'idle', 'msg': 'Up to date (v%s)' % VERSION, 'progress': 0, 'latest': VERSION}
        self.url = ''
        self.use_items = False
        self.server_link = ''
        self.reconnect_on = False
        self.browser_rejoin = False
        self.slow_pc = False
        self.need_rejoin = None
        self.start_again = False
        self.item_due = {}
        self._key_stop = False
        self.played = False
        self.rune_biomes, self.rune_wait, self.rune_due, self.rune_tries = set(), 0, None, 0
        self.selector_biome, self.selector_due, self.selector_refused, self.selector_missed = None, None, 0, 0
        self.pop_biomes, self.pop_due, self.pop_list = set(), None, {}
        self.pop_left = None
        self.rejoining = False
        self.watch = None
        self.merchant_detect = False
        self.boss_detect = False
        self.block_on = False
        self.blocked = {}
        self.block_path = None
        self.block_lock = threading.Lock()
        self.got_past_block = False
        self.merchant_due = None
        self.merchant_at = 0.0
        self._merchant_posted = {}
        self._boss_seen = {}
        self._boss_here = {}                 # boss -> last confirmed presence (monotonic)
        self._stats_fish = 0                 # this session only; the webhook posts these
        self._stats_merchants = 0
        self._stats_bosses = 0
        self._stats_points = 0               # fish points/coins earned from sales this session
        self._life = {'fish': 0, 'points': 0, 'merchants': 0, 'bosses': 0}
        self._life_epoch = 0
        self._life_done_epoch = -1
        self._points_live = None          # on-screen fish points; the card shows this, not a sum of sales
        self._points_suspect = None
        self._load_lifetime()
        self.collect = False
        self.wanted = {}
        self.collect_names = set()
        self.clipper = None
        self.clip_min = 0
        self.equip_aura = None
        self.crack_on = False
        self.in_dimension = True
        self.equip_due = None
        self.equip_busy_at = 0.0
        self.aura_logged = (None, 0.0)
        self.run_wall = 0.0
        self.equip_fails = 0
        self.joined_at = 0.0
        self.biome_now = 'NORMAL'
        self._biome_since = time.time()
        self.presence = None
        self.rares_count = 0
        self.cfg = normalize_cfg(game.load_settings())
        auto_name, auto_uid = detect_roblox_profile()
        custom_name = self.cfg_str('roblox_username').strip()
        cached_uid = self.cfg_str('roblox_uid').strip()
        self.player_name = custom_name or auto_name
        if not custom_name or custom_name.lower() == auto_name.lower():
            self.roblox_uid = auto_uid
        else:
            self.roblox_uid = cached_uid
        _QuietHandler.avatar_uid = self.roblox_uid
        if custom_name and not self.roblox_uid:
            self._resolve_uid_and_avatar(custom_name, sync=False)
        self.fisher = None
        self.out_since = None
        self._look_at = self._played = 0.0
        self.beat = time.monotonic()
        self.restart_run = False
        self.run_id = 0
        self.cache_bust = 1
        self._history = []
        self._hist_ts = 0.0
        self._hist_biome = None
        self._visit_keys = set()
        self._log_hist = []
        self._log_hist_path = None
        self._log_hist_pos = 0
        self._biome_counts = Counter()
        self._lock = threading.RLock()
        self._window = None
        self._toast = ToastService()

        self._sync_detect()
        self.wanted = self.wanted_items()
        self.pop_list = self.pop_items()

        try:
            import local_db
            all_rows = local_db.history(200)
            if not all_rows:
                # one-time import of the old history.jsonl
                if os.path.exists(game.HISTORY_FILE):
                    with open(game.HISTORY_FILE, 'r', encoding='utf-8', errors='replace') as f:
                        for line in f:
                            s = line.strip()
                            if not s:
                                continue
                            try:
                                item = json.loads(s)
                            except Exception:
                                continue
                            local_db.add_history(item.get('ts') or 0, item.get('time', ''),
                                                  item.get('biome') or 'NORMAL', item.get('day') or '')
                    all_rows = local_db.history(200)
            for biome, n in local_db.history_counts().items():
                b = base_biome(biome or 'NORMAL') or 'NORMAL'
                self._biome_counts[b] += int(n)
            self._hist_ts, last_saved = local_db.latest_history()
            self._hist_biome = base_biome(last_saved) if last_saved else None
            for ts, biome in local_db.history_times():
                b = base_biome(biome or '') or ''
                try:
                    if b:
                        self._visit_keys.add((b, int(float(ts))))
                except (TypeError, ValueError):
                    pass
            for item in all_rows[-50:]:
                b = (item.get('biome') or 'NORMAL').upper()
                t_full = item.get('time', '')
                t_short = t_full.split(' ')[-1] if ' ' in t_full else (t_full or '--:--:--')
                self._history.append({'biome': b, 'time': t_short})
            if all_rows:
                self.biome_now = (all_rows[-1].get('biome') or 'NORMAL').upper()
            self.rares_count = sum(v for k, v in self._biome_counts.items() if is_rare(k))
        except Exception:
            pass
        self._sync_live_roblox_log(initial=True)
        self.presence = discord_presence.DiscordPresence(log=lambda msg: note('discord', msg))
        self.refresh_discord()
        seed_and_refresh_avatar(self.roblox_uid, on_ready=self._bump_cache_bust, stop_fn=lambda: self._closing)

        self.cache = merchant_cache.Tracker()
        self._keyboard = keyboard
        threading.Thread(target=self._watch_cache, daemon=True).start()
        threading.Thread(target=self._poll_hotkeys, daemon=True).start()
        threading.Thread(target=self._watch_user_pc, daemon=True).start()
        threading.Thread(target=self._poll_live_telemetry, daemon=True).start()
        threading.Thread(target=self._poll_fish_points, daemon=True).start()
        threading.Thread(target=self._autosave_loop, daemon=True).start()
        threading.Thread(target=self._fetch_auras, daemon=True).start()
        threading.Thread(target=self._watch_roblox_top, daemon=True).start()

    def _sync_detect(self):
        """Merchant detection and Halloween boss detection are separate switches."""
        self.merchant_detect = self.cfg_bool('merchant_on')
        self.boss_detect = self.cfg_bool('boss_on')
        self.block_on = self.merchant_detect or self.boss_detect

    def _watch_roblox_top(self):
        """While Keep Roblox on top is on, pin the game above other windows and pull it forward."""
        pinned = False
        while not self._closing:
            time.sleep(0.45)
            try:
                want = self.cfg_bool('roblox_on_top') and not self.paused and not self._closing
                if want:
                    if game.set_roblox_topmost(True):
                        pinned = True
                elif pinned:
                    game.set_roblox_topmost(False)
                    pinned = False
            except Exception:
                pass

    def _autosave_loop(self):
        last_snap = None
        while not self._closing:
            time.sleep(10)
            if self._closing:
                break
            try:
                snap = json.dumps(self.cfg, sort_keys=True)
                if snap != last_snap:
                    self.remember()
                    last_snap = snap
            except Exception:
                pass

    def _bump_cache_bust(self):
        with self._lock:
            self.cache_bust += 1

    def _resolve_uid_and_avatar(self, username, sync=False):
        def _work():
            body = json.dumps({'usernames': [username], 'excludeBannedUsers': False}).encode('utf-8')
            for url in ('https://users.roblox.com/v1/usernames/users',
                        'https://users.roproxy.com/v1/usernames/users'):
                try:
                    req = urllib.request.Request(
                        url, data=body,
                        headers={'Content-Type': 'application/json', 'User-Agent': 'MMacroHUD/1.0'},
                    )
                    with urllib.request.urlopen(req, timeout=6) as r:
                        rows = (json.load(r) or {}).get('data') or []
                    if rows and rows[0].get('id'):
                        uid = str(rows[0]['id'])
                        with self._lock:
                            self.roblox_uid = uid
                            self.cfg['roblox_uid'] = uid
                            _QuietHandler.avatar_uid = uid
                            self.cache_bust += 1
                        self.remember()
                        seed_and_refresh_avatar(uid, on_ready=self._bump_cache_bust, stop_fn=lambda: self._closing, sync=sync)
                        return uid
                except Exception:
                    continue
            return ''

        if sync:
            return _work()
        threading.Thread(target=_work, daemon=True).start()
        return ''

    def apply_roblox_username(self, raw_name, sync=False):
        name = (raw_name or '').strip()
        auto_name, auto_uid = detect_roblox_profile()
        custom_av = os.path.join(game.APP_DIR, 'avatar_custom.png')
        if os.path.isfile(custom_av):
            try:
                os.remove(custom_av)
            except OSError:
                pass
        with self._lock:
            self.cfg['roblox_username'] = name
            self.player_name = name or auto_name
            if not name or name.lower() == auto_name.lower():
                self.roblox_uid = auto_uid
                self.cfg['roblox_uid'] = auto_uid
                _QuietHandler.avatar_uid = auto_uid
                seed_and_refresh_avatar(auto_uid, on_ready=self._bump_cache_bust, stop_fn=lambda: self._closing, sync=sync)
            else:
                self.roblox_uid = ''
                self.cfg['roblox_uid'] = ''
                _QuietHandler.avatar_uid = ''
                self.cache_bust += 1
        self.remember()
        if name and name.lower() != auto_name.lower():
            self._resolve_uid_and_avatar(name, sync=sync)

    def _fetch_auras(self):
        try:
            note('clip', 'aura list from GitHub: %d auras' % clipper.refresh())
        except Exception as e:
            note('clip', 'aura list not fetched, using the saved one: %s: %s' % (type(e).__name__, e))

    # --------------------------------------------------------------- Config helpers
    def cfg_bool(self, key, default=False):
        return bool(self.cfg.get(key, default))

    def cfg_str(self, key, default=''):
        val = self.cfg.get(key, default)
        return '' if val is None else str(val)

    def cfg_int(self, key, default=0, min_val=0):
        try:
            return max(min_val, int(str(self.cfg.get(key, default) or default).strip() or default))
        except (ValueError, TypeError):
            return max(min_val, default)

    def wanted_items(self):
        out = {}
        for name, entry in (self.cfg.get('buy_items') or {}).items():
            if isinstance(entry, dict) and entry.get('on'):
                q = str(entry.get('qty') or '').strip()
                out[name] = int(q) if q.isdecimal() and int(q) > 0 else None
        return out

    def pop_items(self):
        out = {}
        for biome, items in (self.cfg.get('pop_items') or {}).items():
            if not isinstance(items, dict):
                continue
            got = {}
            for n, a in items.items():
                s = str(a).strip()
                got[n] = int(s) if s.isdecimal() and int(s) > 0 else 1
            if got:
                out[biome] = got
        return out

    def sell_every(self):
        return self.cfg_int('fish_sell', 0, 0)

    def rune_delay(self):
        return self.cfg_int('rune_after', 0, 0)

    def sell_amount(self):
        return self.cfg_int('fish_sell_amount', 10, 1)

    def _correct_aura(self, quiet=False):
        typed = self.cfg_str('equip_aura').strip()
        if not typed:
            return None
        name = equip.resolve(typed)
        if name is None:
            return None
        if name != typed:
            self.cfg['equip_aura'] = name
            self.remember()
            if not quiet:
                note('equip', '"%s" read as %s' % (typed, name))
        return name

    def remember(self):
        try:
            with self._lock:
                self.wanted = self.wanted_items()
                self.pop_list = self.pop_items()
                save = {
                    'auto_items': self.cfg_bool('auto_items'),
                    'rune_on': self.cfg_bool('rune_on'),
                    'rune_biomes': list(self.cfg.get('rune_biomes') or list(RUNE_BIOMES)),
                    'rune_after': self.rune_delay(),
                    'selector_on': self.cfg_bool('selector_on'),
                    'selector_biome': self.cfg_str('selector_biome', 'HEAVEN'),
                    'pop_on': self.cfg_bool('pop_on'),
                    'pop_items': self.pop_list,
                    'anti_afk': self.cfg_bool('anti_afk', True),
                    'yield_pc': self.cfg_bool('yield_pc', True),
                    'merchant_on': self.cfg_bool('merchant_on'),
                    'boss_on': self.cfg_bool('boss_on'),
                    'roblox_on_top': self.cfg_bool('roblox_on_top'),
                    'skip_intro': self.cfg_bool('skip_intro', True),
                    'silent_merchant': self.cfg_bool('silent_merchant'),
                    'webhook_url': clean_url(self.cfg_str('webhook_url')),
                    'private_server': game.tidy_link(self.cfg_str('private_server')),
                    'auto_reconnect': self.cfg_bool('auto_reconnect'),
                    'rejoin_browser': self.cfg_bool('rejoin_browser'),
                    'low_end': self.cfg_bool('low_end'),
                    'eden_on': self.cfg_bool('eden_on'),
                    'eden_path': self.cfg_bool('eden_path', True),
                    'eden_record': self.cfg_bool('eden_record'),
                    'eden_ping': self.cfg_str('eden_ping').strip(),
                    'fish_on': self.cfg_bool('fish_on'),
                    'fish_vip': self.cfg_bool('fish_vip', True),
                    'fish_sell': self.sell_every(),
                    'fish_sell_all': self.cfg_bool('fish_sell_all', True),
                    'fish_full_check': self.cfg_bool('fish_full_check', True),
                    'fish_sell_amount': self.sell_amount(),
                    'collect_on': self.cfg_bool('collect_on'),
                    'collect_merchants': list(self.cfg.get('collect_merchants') or ['Jester', 'Mari', 'Rin']),
                    'clip_on': self.cfg_bool('clip_on'),
                    'clip_min': self.cfg_str('clip_min', '1,000,000,000').strip(),
                    'clip_quality': self.cfg_str('clip_quality', '768p'),
                    'clip_key_on': self.cfg_bool('clip_key_on'),
                    'clip_keys': self.cfg_str('clip_keys', 'Win+Alt+G'),
                    'equip_on': self.cfg_bool('equip_on'),
                    'equip_aura': self.cfg_str('equip_aura').strip(),
                    'roblox_username': self.cfg_str('roblox_username').strip(),
                    'roblox_uid': self.cfg_str('roblox_uid').strip(),
                    'discord_rpc': self.cfg_bool('discord_rpc', True),
                    'theme': self.cfg_str('theme', 'dark') if self.cfg_str('theme', 'dark') in ('dark', 'white', 'midnight', 'ember', 'forest', 'bw') else 'dark',
                    'perf_mode': self.cfg_str('perf_mode', 'auto') if self.cfg_str('perf_mode', 'auto') in ('auto', 'lite', 'full') else 'auto',
                    'fx_on': self.cfg_bool('fx_on', True),
                    'tutorial_seen': self.cfg_bool('tutorial_seen'),
                    'auto_update': self.cfg_bool('auto_update', True),
                    'buy_items': self.cfg.get('buy_items') or {},
                    'buy_list': '',
                }
                game.save_settings(save)
        except Exception as e:
            note('config', 'save failed: %s: %s' % (type(e).__name__, e))

    # --------------------------------------------------------------- Telemetry & UI State
    def _sync_live_roblox_log(self, initial=False):
        try:
            latest = game.newest_log()
            if not latest or not os.path.exists(latest):
                return False
            with open(latest, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 131072), os.SEEK_SET)
                tail = f.read().decode('utf-8', errors='replace')
            found_b, found_a = None, None
            for line in reversed(tail.splitlines()):
                if found_b is None:
                    b = game.biome_from_line(line)
                    if b:
                        found_b = b.upper()
                if found_a is None:
                    a = game.aura_from_line(line)
                    if a and a != '_None_':
                        found_a = a
                if found_b and found_a:
                    break
            changed = False
            watching = self.biome_stop is not None and not self.biome_stop.is_set()
            if found_b and found_b != self.biome_now and (initial or not watching):
                self._touch_biome(found_b)
                changed = True
            if found_a and found_a != self.aura_logged[0]:
                self.aura_logged = (found_a, time.time())
                changed = True
            return changed
        except Exception:
            return False

    def _refresh_log_history(self):
        """Recent biomes from the Roblox log's own timestamps, not the last time the watcher saved one."""
        path = game.newest_log()
        if not path or not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        with self._lock:
            if path != self._log_hist_path or size < self._log_hist_pos:
                self._log_hist_path = path
                self._log_hist = []
                self._log_hist_pos = 0
            start = self._log_hist_pos
            last = self._log_hist[-1]['biome'] if self._log_hist else None
        if size == start:
            return
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                data = f.read()
                end = f.tell()
        except OSError:
            return
        if data and not data.endswith(b'\n'):
            cut = data.rfind(b'\n')
            if cut < 0:
                return
            end -= len(data) - cut - 1
            data = data[:cut + 1]
        added = []
        today = time.strftime('%Y-%m-%d')
        for line in data.decode('utf-8', errors='replace').splitlines():
            name = game.biome_from_line(line)
            if not name:
                continue
            name = name.upper()
            if name == last:
                continue
            when = game.time_from_line(line)
            shown = when or time.time()
            local = time.localtime(shown)
            day = time.strftime('%Y-%m-%d', local)
            added.append({
                'biome': name,
                'time': time.strftime('%H:%M:%S', local),
                'day': '' if day == today else time.strftime('%b %d', local),
                'ts': shown,
                'logged': when,
            })
            last = name
        saved = self._save_logged_biomes(added) if self.running else 0
        with self._lock:
            if path != self._log_hist_path:
                return
            self._log_hist_pos = end
            if added:
                self._log_hist.extend(added)
                if len(self._log_hist) > 120:
                    del self._log_hist[:60]
        if saved:
            note('biomes', 'synced %d biome visit%s from the Roblox log' % (saved, '' if saved == 1 else 's'))

    def _biomes_in_log(self, path):
        """Biome changes in one Roblox log, in order."""
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            return []
        added, last = [], None
        today = time.strftime('%Y-%m-%d')
        for line in data.decode('utf-8', errors='replace').splitlines():
            name = game.biome_from_line(line)
            if not name:
                continue
            name = name.upper()
            if name == last:
                continue
            when = game.time_from_line(line)
            shown = when or time.time()
            local = time.localtime(shown)
            day = time.strftime('%Y-%m-%d', local)
            added.append({
                'biome': name,
                'time': time.strftime('%H:%M:%S', local),
                'day': '' if day == today else time.strftime('%b %d', local),
                'ts': shown,
                'logged': when,
            })
            last = name
        return added

    def _sync_biomes_from_log(self):
        """Pull biome visits from the current Roblox log. Used when the macro starts."""
        path = game.newest_log()
        if not path:
            return 0
        saved = self._save_logged_biomes(self._biomes_in_log(path))
        if saved:
            note('biomes', 'synced %d biome visit%s from the Roblox log' % (saved, '' if saved == 1 else 's'))
        return saved

    def _save_logged_biomes(self, items):
        """Add Roblox-log biome visits that are not already saved. A visit saved by an
        older build has a timestamp a second or two off the log line, so those match too."""
        import local_db
        saved = 0
        for item in items:
            when = item.get('logged')
            if when is None:
                continue
            name = base_biome(item.get('biome') or '') or (item.get('biome') or '').upper()
            if not name:
                continue
            sec = int(when)
            with self._lock:
                if any((name, sec + delta) in self._visit_keys for delta in (-2, -1, 0, 1, 2)):
                    continue
            local = time.localtime(when)
            day = time.strftime('%Y-%m-%d', local)
            today = time.strftime('%Y-%m-%d')
            try:
                local_db.add_history(when, time.strftime('%Y-%m-%d %H:%M:%S', local), name,
                                     '' if day == today else time.strftime('%b %d', local))
            except Exception:
                continue
            with self._lock:
                self._visit_keys.add((name, sec))
                self._hist_ts = max(self._hist_ts, when)
                self._hist_biome = name
                self._biome_counts[name] += 1
                if is_rare(name):
                    self.rares_count += 1
            saved += 1
        return saved

    def _poll_live_telemetry(self):
        while not self._closing:
            try:
                self._refresh_log_history()
                if self._sync_live_roblox_log():
                    self.refresh_discord()
            except Exception:
                pass
            time.sleep(1.0)

    def _touch_biome(self, name):
        name = (name or '').upper()
        if not name or name == self.biome_now:
            return False
        self.biome_now = name
        self._biome_since = time.time()
        return True

    def presence_mode(self):
        """What the macro is doing right now, for Discord: Fishing / Eden / both / Idle."""
        if not self.running:
            return 'Idle'
        modes = []
        if getattr(self, 'fisher', None) is not None or self.cfg_bool('fish_on'):
            modes.append('Fishing')
        if self.cfg_bool('eden_on') or getattr(self, 'crack_on', False):
            modes.append('Eden')
        return ' + '.join(modes) if modes else 'Idle'

    def refresh_discord(self):
        """Push the current biome, mode and run status to Discord Rich Presence."""
        presence = getattr(self, 'presence', None)
        if presence is None:
            return
        try:
            enabled = self.cfg_bool('discord_rpc', True)
            presence.configure(enabled, DISCORD_APP_ID)
            if not enabled:
                presence.set_activity(None)
                return
            if not getattr(self, 'developer_locked', True):
                presence.set_activity({
                    'details': 'Veyra Macro\nMode: Developer',
                    'state': 'Status: Developer Mode\nPasskey-locked session',
                    'buttons': [{'label': '🌌 Download Veyra Macro', 'url': RELEASE_PAGE}],
                    'assets': {
                        'large_image': 'macro_logo',
                        'large_text': 'Developer Mode',
                        'small_image': 'macro_logo',
                        'small_text': 'Developer',
                    },
                })
                return
            if not self.running:
                presence.set_activity({
                    'details': 'Veyra Macro\nMode: Idle',
                    'state': 'Status: Ready\nPress Start in the app to begin',
                    'buttons': [{'label': '🌌 Download Veyra Macro', 'url': RELEASE_PAGE}],
                    'assets': {
                        'large_image': 'macro_logo',
                        'large_text': 'Veyra Macro',
                        'small_image': 'macro_logo',
                        'small_text': 'Idle',
                    },
                })
                return
            biome = base_biome(self.biome_now or 'NORMAL') or 'NORMAL'
            aura = self.aura_logged[0] if self.aura_logged and self.aura_logged[0] not in (None, '', '_None_') else ''
            slug = biome_slug(biome) or 'normal'
            if slug == 'sand_storm':
                slug = 'sandstorm'
            details = 'Mode: %s' % self.presence_mode()
            state_lines = []
            if aura:
                state_lines.append('Aura: %s' % aura)
            state_lines.append('Server: %d/%d players' % (
                self.cfg_int('server_players', 1, 1), self.cfg_int('server_slots', 20, 1)))
            state_lines.append('Status: %s' % ('Paused' if self.paused else 'Running'))
            presence.set_activity({
                'details': details,
                'state': '\n'.join(state_lines),
                'timestamps': {'start': int(self.run_wall or time.time())},
                'buttons': [{'label': '🌌 Download Veyra Macro', 'url': RELEASE_PAGE}],
                'assets': {
                    'large_image': slug,
                    'large_text': 'Biome: %s' % (pretty(biome) or 'Normal'),
                    'small_image': 'macro_logo',
                    'small_text': ('Aura: %s' % aura) if aura else 'Veyra Macro',
                },
            })
        except Exception as e:
            note('discord', 'presence update failed: %s: %s' % (type(e).__name__, e))

    def wants_lite(self):
        """Light HUD and slower clicks. Auto follows RAM and CPU unless Settings overrides it."""
        mode = self.cfg_str('perf_mode', 'auto')
        if mode == 'full':
            return False
        if mode == 'lite':
            return True
        prof = game.machine_profile()
        return bool(prof.get('lite')) or self.cfg_bool('low_end')

    def pc_info(self):
        prof = game.machine_profile()
        lite = self.wants_lite()
        return {
            'ram_gb': prof.get('ram_gb') or 0,
            'cores': prof.get('cores') or 0,
            'cpu': prof.get('cpu') or '',
            'hardware_lite': bool(prof.get('lite')),
            'lite': lite,
            'fx': self.cfg_bool('fx_on', True) and not lite,
        }

    def _lifetime_path(self):
        return os.path.join(game.APP_DIR, 'lifetime.json')

    def _load_lifetime(self):
        try:
            with open(self._lifetime_path(), encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        for key in self._life:
            try:
                self._life[key] = max(0, int(raw.get(key) or 0))
            except (TypeError, ValueError):
                self._life[key] = 0

    def _save_lifetime(self, payload=None):
        try:
            os.makedirs(game.APP_DIR, exist_ok=True)
            with open(self._lifetime_path(), 'w', encoding='utf-8') as f:
                json.dump(self._life if payload is None else payload, f)
        except OSError as e:
            note('stats', 'could not save all-time stats: %s' % e)

    def _flush_shown(self):
        """Write the all-time total, including this open session, without folding it twice."""
        with self._lock:
            shown = self._shown_stats()
        self._save_lifetime(shown)

    def _session_totals(self):
        """Counts for the open monitoring session. The webhook sends these, not the all-time totals."""
        return {
            'fish': self._stats_fish + (self.fisher.caught if self.fisher else 0),
            'points': self._stats_points + (self.fisher.points_earned if self.fisher else 0),
            'merchants': self._stats_merchants,
            'bosses': self._stats_bosses,
        }

    def _shown_stats(self):
        sess = self._session_totals()
        shown = {key: int(self._life.get(key, 0)) + int(sess[key]) for key in self._life}
        # Fish points is the balance in the corner of the game. Sale deltas miss catches
        # the reader skipped, so adding them drifts away from the number on screen.
        if self._points_live is not None:
            shown['points'] = int(self._points_live)
        return shown

    def _set_fish_points(self, balance):
        """Remember the fish-points balance read off the game window."""
        try:
            balance = int(balance)
        except (TypeError, ValueError):
            return
        if balance < 0:
            return
        with self._lock:
            prev = self._points_live if self._points_live is not None else int(self._life.get('points') or 0)
            # A read of the coins line, or the small counter under fish points, is a different number.
            if prev > 0 and (balance > prev * 3 or balance * 3 < prev):
                if self._points_suspect != balance:
                    self._points_suspect = balance
                    return
            if self._points_live == balance:
                self._points_suspect = None
                return
            self._points_suspect = None
            self._points_live = balance
            self._life['points'] = balance
            self._save_lifetime()

    def _poll_fish_points(self):
        while not self._closing:
            try:
                got = fishing.read_fish_points()
                if got is not None:
                    self._set_fish_points(got)
            except Exception:
                pass
            for _ in range(8):
                if self._closing:
                    return
                time.sleep(0.5)

    def _commit_lifetime(self, epoch=None):
        """Add this session onto the saved all-time totals once the session is over."""
        with self._lock:
            if epoch is None:
                epoch = self._life_epoch
            if epoch != self._life_epoch or epoch == self._life_done_epoch:
                return
            live = self._points_live
            for key, value in self._session_totals().items():
                if key == 'points' and live is not None:
                    self._life['points'] = int(live)
                    continue
                self._life[key] = int(self._life.get(key, 0)) + int(value)
            self._stats_fish = self._stats_merchants = self._stats_bosses = self._stats_points = 0
            if self.fisher is not None:
                self.fisher.caught = 0
                self.fisher.points_earned = 0
            self._life_done_epoch = epoch
            self._save_lifetime()

    def build_state(self):
        with self._lock:
            if self.running and self.run_wall > 0:
                elapsed = max(0, int(time.time() - self.run_wall))
                h, rem = divmod(elapsed, 3600)
                m, s = divmod(rem, 60)
                uptime = '%02d:%02d:%02d' % (h, m, s)
            else:
                uptime = '00:00:00'

            r_up = game.find_roblox_window() is not None
            prev = self._preview_biome
            if prev and time.monotonic() < prev[1]:
                bnow = prev[0]
            else:
                self._preview_biome = None
                bnow = base_biome(self.biome_now or 'NORMAL') or 'NORMAL'
            r, g, b = biome_rgb(bnow)
            biomes_meta = {
                k: {
                    'pretty': pretty(k),
                    'rate': BIOME_RATES.get(k, 'Event Biome'),
                    'img': biome_web_path(k),
                }
                for k in BIOME_COLOURS
            }
            source = self._log_hist if self._log_hist else self._history
            hist = [
                {
                    'biome': h_item['biome'],
                    'pretty': pretty(h_item['biome']),
                    'time': h_item['time'],
                    'day': h_item.get('day') or '',
                    'rare': is_rare(h_item['biome']),
                    'img': biome_web_path(h_item['biome']),
                }
                for h_item in reversed(source[-40:])
            ]
            biome_counts = [
                {
                    'key': k,
                    'name': pretty(k),
                    'count': int(self._biome_counts.get(k, 0)),
                    'color': '#%06x' % biome_colour(k),
                    'rate': BIOME_RATES.get(k, 'Event Biome'),
                    'img': biome_web_path(k),
                    'rare': is_rare(k),
                }
                for k in COUNTS_ORDER
            ]
            biome_counts_total = int(sum(item['count'] for item in biome_counts))
            with _log_lock:
                logs = [{'ts': ts, 'tag': tag, 'msg': msg} for ts, tag, msg in _recent_logs[-100:]]
            supp = self.support_status()

            return {
                'cfg': json.loads(json.dumps(self.cfg)),
                'running': self.running,
                'paused': self.paused,
                'phase': self.phase_text,
                'uptime': uptime,
                'player_name': self.player_name,
                'roblox_uid': self.roblox_uid,
                'avatar_url': '/avatar.png',
                'roblox_up': r_up,
                'biome_now': bnow,
                'biome_pretty': pretty(bnow),
                'biome_rate': BIOME_RATES.get(bnow, 'Event Biome'),
                'biome_rgb': '%d, %d, %d' % (r, g, b),
                'biome_img': biome_web_path(bnow),
                'rares_count': self.rares_count,
                'run_stats': self._shown_stats(),
                'biome_counts': biome_counts,
                'biome_counts_total': biome_counts_total,
                'aura_now': (self.aura_logged[0] if self.aura_logged[0] and self.aura_logged[0] != '_None_' else ''),
                'cache_bust': self.cache_bust,
                'drives': dict(selector.DRIVES),
                'potions': list(potions.POTIONS),
                'known_items': list(shop.KNOWN_ITEMS),
                'testers': list(TESTERS),
                'biomes': biomes_meta,
                'history': hist,
                'logs': logs,
                'support': supp,
                'version': VERSION,
                'developer_locked': self.developer_locked,
                'update_info': dict(self.update_info),
                'discord_status': self.presence.status() if self.presence is not None else {'status': 'off', 'detail': ''},
                'pc': self.pc_info(),
            }

    # --------------------------------------------------------------- Plumbing / Notices
    def phase(self, text, colour=TEXT):
        clean = (text or '').replace(' -- ', ', ').replace(' - ', ', ')
        self.phase_text = clean or 'idle'
        if self._window is not None:
            try:
                title = TITLE if not clean or clean == 'idle' else '%s  |  %s' % (TITLE, clean)
                self._window.set_title(title)
            except Exception:
                pass

    def _set(self, **kw):
        if 'toast' in kw:
            self.notice(kw['toast'], BAD, 12000)
        if 'phase' in kw:
            self.phase(kw['phase'], kw.get('colour', TEXT))
        if 'biome' in kw:
            b_text, b_col = kw['biome']
            self.phase(b_text, b_col)
        if kw.get('key'):
            self._key_notice(kw)
        act = kw.get('action')
        if act == 'start' and self.running and game.ABORT:
            self.start_again = True
            self.phase('stopping - starting again after it', DIM)
        elif act == 'start' and not self.running:
            self.phase('starting', GOOD)
            self.start()
        elif act == 'stop' and self.running:
            self.stop()
        elif act == 'quit':
            self.close()
        elif act == 'idle':
            self.phase('idle', DIM)
            if self._key_stop:
                self._key_stop = False
                self.notice('Veyra: stopped', DIM)
            if self.start_again:
                self.start_again = False
                threading.Timer(0.2, self.start).start()

    def notice(self, text, colour=TEXT, ms=2200):
        try:
            self._toast.show(text, colour, ms)
        except Exception as e:
            note('ui', 'notice: %s: %s' % (type(e).__name__, e))

    def _key_notice(self, kw):
        if kw.get('action') == 'start':
            if self.running and game.ABORT:
                self.notice('Veyra: stopping - starts again right after', DIM)
            elif self.running:
                self.notice('Veyra is already running  (F2 stops it)', ACCENT)
            else:
                self.notice('Veyra: starting', GOOD, 1200)
        elif kw.get('action') == 'stop':
            self._key_stop = self.running
            self.notice('Veyra: stopping' if self.running else 'Veyra is not running  (F1 starts it)',
                        BAD if self.running else DIM)

    def _poll_hotkeys(self):
        state, was = ctypes.windll.user32.GetAsyncKeyState, {}
        while not self._closing:
            for vk, key in ((0x70, keyboard.Key.f1), (0x71, keyboard.Key.f2)):
                held = bool(state(vk) & 0x8000)
                if held and not was.get(vk) and not self._capturing:
                    self.on_key(key)
                was[vk] = held
            time.sleep(0.01)

    def on_key(self, key, *_):
        try:
            kb = self._keyboard
            if key is not kb.Key.f1 and key is not kb.Key.f2:
                return True
            now = time.perf_counter()
            which = 'start' if key is kb.Key.f1 else 'stop'
            if now - self._last_hotkey.get(which, 0.0) < 0.75:
                return True
            self._last_hotkey[which] = now
            note('run', 'F%d pressed' % (1 if which == 'start' else 2))
            if which == 'stop':
                game.ABORT = True
            self._set(action=which, key=True)
        except Exception:
            pass
        return True

    # --------------------------------------------------------------- Biome & Merchant watchers
    def start_biomes(self, url):
        if self.biome_stop is not None and not self.biome_stop.is_set():
            return
        if not (url.startswith('https://') or self.merchant_detect or self.boss_detect or self.clipper
                or self.equip_aura or self.rune_biomes or self.selector_biome or self.pop_biomes):
            return
        self.biome_stop = threading.Event()
        threading.Thread(target=self._watch_biomes, args=(url,), daemon=True).start()

    def _watch_biomes(self, url):
        cfg = game.load_settings()
        stop = self.biome_stop
        failed = None
        post_status(url, 'Macro started', colour=EMBED_GOOD,
                    username=self.player_name or self.cfg_str('roblox_username'), logo=True)
        self._set(biome=('watching', GOOD))
        try:
            watcher = BiomeWatcher(float(cfg.get('cooldown_seconds') or 300))
            self._merchant_posted = {}
            last_aura = None
            for line in game.follow(stop.is_set):
                try:
                    last_aura = self._read_line(line, url, watcher, last_aura)
                except Exception as e:
                    note('biomes', 'a log line was not read: %s: %s' % (type(e).__name__, e))
        except Exception as e:
            failed = '%s: %s' % (type(e).__name__, e)
        finally:
            if failed:
                post_event(url, 'Biome detection stopped', '`%s`' % failed, EMBED_BAD, 'Biome')
            else:
                post_status(url, 'Macro stopped', colour=EMBED_INFO)
            if self.biome_stop is stop:
                self.biome_stop = None
            self._set(biome=(failed, BAD) if failed else ('idle', DIM))

    def boss_present(self):
        """A boss confirmed in-game is still around in its own biome, so skip the Cursed Rune."""
        now = time.monotonic()
        self._boss_here = {b: at for b, at in self._boss_here.items()
                           if now - at <= BOSS_HERE_S}
        if not self._boss_here:
            return False
        here = base_biome(self.biome_now or '').replace(' ', '').upper()
        if not here:
            return True
        return any(BOSS_BIOMES.get(b, '').replace(' ', '').upper() == here
                   for b in self._boss_here)

    def merchant_arrived(self, merchant, how, url=None):
        posted = self._merchant_posted
        if time.time() - posted.get(merchant, 0.0) <= MERCHANT_REPEAT_S:
            return
        need = BOSS_BIOMES.get(merchant)
        if need:
            # A cache hit alone is not a spawn: Roblox touches these animation
            # files while the game is still loading. Only count a boss once we
            # are really in the game, and only in its own biome. A hit just
            # before the biome starts is posted by _on_biome_change instead.
            if not self.connected():
                self._boss_seen[merchant] = time.monotonic()
                return
            if self.biome_now and not rune_biome(self.biome_now, {need}):
                if time.monotonic() - self._boss_seen.get(merchant, -1e9) > BOSS_EARLY_S:
                    note('merchant', '%s seen outside %s: not posted unless %s starts' % (merchant, pretty(need), pretty(need)))
                self._boss_seen[merchant] = time.monotonic()
                return
        posted[merchant] = time.time()
        if need:
            self._boss_here[merchant] = time.monotonic()
        note('merchant', '%s arrived (%s)' % (merchant, how))
        try:
            import local_db
            local_db.add_merchant(merchant, how, self.server_link or game.load_settings().get('private_server', ''),
                                  posted[merchant])
        except Exception:
            pass
        if need:
            self._stats_bosses += 1
        else:
            self._stats_merchants += 1
        # Bosses always post in Discord webhooks even when Silent Merchant is on.
        # Silent Merchant suppresses only regular merchant spawn alerts.
        if not self.cfg_bool('silent_merchant') or need:
            post_merchant(url or self.url, merchant, self.server_link or game.load_settings().get('private_server', ''),
                          watch=list(self.wanted), collect=self.collect and merchant in self.collect_names, how=how)
        else:
            note('merchant', 'silent mode: no spawn alert sent')
        self._set(phase='%s arrived' % merchant, colour=GOOD)
        if self.collect and merchant in self.collect_names:
            self.merchant_due, self.merchant_at, self.merchant_tries = merchant, time.monotonic(), 0

    def _watch_cache(self):
        said = posted = False
        while not self._closing:
            try:
                came = self.cache.poll()
                said = posted = False
                if came and self.running:
                    bosses = set(BOSS_BIOMES)
                    for name in came:
                        if name in bosses:
                            if self.boss_detect:
                                self.merchant_arrived(name, 'seen in Roblox\'s cache')
                        elif self.merchant_detect:
                            self.merchant_arrived(name, 'seen in Roblox\'s cache')
                self._keep_block()
            except Exception as e:
                if not said:
                    path = self.cache.cache() or ''
                    bad = merchant_cache.unreadable(path) if path else []
                    note('merchant', 'Roblox\'s cache could not be read: %s: %s (%s%s)' % (
                        type(e).__name__, e, path.replace(merchant_cache.LOCAL, '%LOCALAPPDATA%'),
                        '; cannot open ' + ', '.join(bad) if bad else ''))
                    said = True
                if not posted and self.running and (self.merchant_detect or self.boss_detect):
                    boss_only = self.boss_detect and not self.merchant_detect
                    post_event(self.url, 'Boss detection is not working' if boss_only else 'Merchant detection is not working',
                               CACHE_UNREADABLE, EMBED_BAD, 'Boss' if boss_only else 'Merchant')
                    posted = True
            time.sleep(2)

    def _tracked_files(self):
        """Cache keys for whichever of merchants and Halloween bosses the user is watching."""
        names = []
        if self.cfg_bool('merchant_on'):
            names.extend(('Mari', 'Jester', 'Rin'))
        if self.cfg_bool('boss_on'):
            names.extend(BOSS_BIOMES)
        return {k for name in names for k in merchant_cache.FILES.get(name, ())}

    def _keep_block(self):
        want = self._tracked_files()
        with self.block_lock:
            if self._closing:
                return
            if want:
                path = self.cache.cache()
                if not path:
                    return
                wrote = merchant_cache.block(want, path)
                if wrote:
                    self.cache.forget(wrote)
                    past = self.loaded_already(wrote)
                    note('merchant', '%s %d animation(s) in Roblox\'s cache%s' % (
                        'blocked again' if self.blocked else 'blocked', len(wrote),
                        ': this Roblox already loaded a real one' if past else ''))
                    self.got_past_block = self.got_past_block or past
                    self.blocked = {**self.blocked, **wrote}
                    self.block_path = path
                extra = set(self.blocked) - want
                if extra:
                    n = merchant_cache.unblock(extra, self.block_path or path)
                    for key in extra:
                        self.blocked.pop(key, None)
                    if n:
                        note('merchant', 'block taken out of Roblox\'s cache (%d)' % n)
            elif self.blocked or self.cache.ours:
                self._unblock({k for ks in merchant_cache.FILES.values() for k in ks})

    def _unblock(self, keys):
        path = self.block_path or self.cache.cache()
        note('merchant', 'block taken out of Roblox\'s cache (%d)' % merchant_cache.unblock(keys, path))
        self.blocked, self.block_path = {}, None

    def loaded_already(self, blocked=None):
        blocked = self.blocked if blocked is None else blocked
        starts = []
        for p in game.roblox_processes():
            try:
                if p.name().lower() == 'robloxplayerbeta.exe':
                    starts.append(p.create_time())
            except Exception:
                pass
        return bool(starts) and any(t and t / 1000.0 > min(starts) for t in blocked.values())

    def _read_line(self, line, url, watcher, last_aura):
        biome = game.biome_from_line(line) if line else None
        if biome:
            self._touch_biome(biome.upper())
        aura = game.aura_from_line(line) if line and (self.clipper or self.equip_aura) else None
        if aura == '_None_' and (last_aura is None or time.monotonic() - self.joined_at < JOIN_NONE_S):
            aura = None
        if aura:
            self.aura_logged = (aura, time.time())
        if aura and aura != last_aura:
            ours = (self.equip_aura and clipper.norm(aura) == clipper.norm(self.equip_aura)
                    and time.monotonic() - self.equip_busy_at < 120)
            if self.clipper and last_aura is not None and aura != '_None_' and not ours:
                self.clip_aura(aura, line)
            if self.equip_aura and clipper.norm(aura) != clipper.norm(self.equip_aura):
                note('equip', 'Roblox\'s log says %s is equipped now: %s goes back on'
                     % (aura, self.equip_aura))
                if self.equip_due is None:
                    self.equip_due = max(time.monotonic(), self.equip_busy_at + EQUIP_GAP_S)
            last_aura = aura
        change = watcher.feed(biome)
        if change:
            name, alert = change
            self.rune_biome_changed(name)
            self.pop_biome_changed(name)
            if self.boss_detect:
                for boss, at in list(self._boss_seen.items()):
                    if time.monotonic() - at <= BOSS_EARLY_S and rune_biome(name, {BOSS_BIOMES[boss]}):
                        self.merchant_arrived(boss, 'seen just before %s started' % pretty(name))
            self._boss_seen.clear()
            with self._lock:
                self._history.append({'biome': name, 'time': time.strftime('%H:%M:%S')})
                if len(self._history) > 100:
                    del self._history[:50]
            server = self.server_link or game.load_settings().get('private_server', '')
            times = (time.time() - self.run_wall, time.monotonic() - self.joined_at) if self.running else (None, None)
            post_biome(url, name, ping=alert, server=server, running=times[0], here=times[1])
            self._set(biome=(pretty(name), ACCENT if alert else DIM))
        self.refresh_discord()
        return last_aura

    def clip_aura(self, aura, line):
        n = clipper.rarity(aura, self.biome_now, line)
        if n is not None and n < self.clip_min:
            return
        odds = '1 in {:,}'.format(n) if n else 'rarity unknown'
        note('clip', '%s rolled (%s) - saving a clip' % (aura, odds))
        self._set(phase='%s rolled - clipping' % aura, colour=GOOD)

        colour = next((c for least, c in CLIP_COLOURS if (n or 0) >= least), EMBED_INFO)
        shot = self.clipper.shot(time.time())

        def done(path):
            if path:
                how = 'Clipped using hotkey' if path is True else 'clips\\%s' % os.path.basename(path)
                post_event(self.url, 'Aura Clipped - %s' % aura, '', colour, 'Clip',
                           [('Rarity', odds, True), ('Biome', pretty(self.biome_now) or '-', True), ('Clip', how, True)],
                           shot=shot, thumb=asset_png('aura_clip_logo.png'))
        self.clipper.clip('%s' % aura, done)

    # --------------------------------------------------------------- Actions
    def start(self):
        if self.running or self.updating:
            return None
        self.ran = True
        self.server_link = game.tidy_link(self.cfg_str('private_server'))
        self.cfg['private_server'] = self.server_link
        self.reconnect_on = self.cfg_bool('auto_reconnect')
        self.played = False
        self.item_due = {}
        self.browser_rejoin = self.cfg_bool('rejoin_browser')
        self.slow_pc = self.wants_lite()
        if self.reconnect_on and not game.deeplink(self.server_link):
            self.phase('auto reconnect needs a private server link', BAD)
            msg = 'Auto reconnect is on - paste a Roblox private server link on the Settings tab.'
            self.notice('Cannot start: ' + msg, BAD, 8000)
            return msg
        if game.roblox_elevated():
            self.phase('Roblox is running as administrator', BAD)
            msg = 'Roblox is running as administrator, so Windows will not let the macro click in it. Reopen Roblox normally.'
            self.notice('Cannot start: ' + msg, BAD, 8000)
            return msg
        self._sync_detect()
        if self.block_on and not merchant_cache.cache_found():
            self.phase("Roblox's cache was not found", BAD)
            self.notice('Cannot start: ' + STORE_ROBLOX.split('\n')[0], BAD, 8000)
            return STORE_ROBLOX.split('\n')[0]
        self.collect = self.cfg_bool('collect_on')
        if self.collect and not self.merchant_detect:
            self.phase('auto merchant collection needs merchant detection', BAD)
            msg = 'Auto merchant collection needs Merchant Detection enabled.'
            self.notice('Cannot start: ' + msg, BAD, 8000)
            return msg

        self.wanted = self.wanted_items()
        self.collect_names = set(self.cfg.get('collect_merchants') or ['Jester', 'Mari', 'Rin'])
        self.merchant_due = None
        self.remember()
        self.url = clean_url(self.cfg_str('webhook_url'))
        self.use_items = self.cfg_bool('auto_items')
        self.rune_biomes = set(self.cfg.get('rune_biomes') or RUNE_BIOMES) if self.cfg_bool('rune_on') else set()
        self.rune_wait, self.rune_due = self.rune_delay(), None
        self.selector_biome = self.cfg_str('selector_biome', 'HEAVEN') if self.cfg_bool('selector_on') else None
        self.selector_due = time.monotonic() if self.selector_biome else None
        self.selector_refused = self.selector_missed = 0
        self.pop_list, self.pop_due, self.pop_left = self.pop_items(), None, None
        self.pop_biomes = set(self.pop_list) if self.cfg_bool('pop_on') else set()
        self.clip_min = clip_floor(self.cfg_str('clip_min', '1,000,000,000'))
        self.clipper = None
        if self.cfg_bool('clip_on') and self.cfg_bool('clip_key_on'):
            try:
                self.clipper = clipper.KeyClipper(self.cfg_str('clip_keys', 'Win+Alt+G'), lambda text: note('clip', text))
            except ValueError as e:
                note('clip', 'the clip key cannot be pressed (%s): no clips this run' % e)
        elif self.cfg_bool('clip_on'):
            self.clipper = clipper.Clipper(lambda text: note('clip', text),
                                           height=clipper.HEIGHTS.get(self.cfg_str('clip_quality', '768p'), clipper.HEIGHT))
        if self.clipper:
            self.clipper.start()
        self.equip_aura = self._correct_aura() if self.cfg_bool('equip_on') else None
        if self.cfg_bool('equip_on') and not self.equip_aura:
            note('equip', 'no aura in the list to keep equipped: Keep an aura equipped is off this run')
        self.equip_due = time.monotonic() if self.equip_aura else None
        self.crack_on, self.in_dimension = self.cfg_bool('eden_on'), True
        self.run_wall, self.equip_busy_at, self.equip_fails = time.time(), -EQUIP_GAP_S, 0
        self._stats_fish = self._stats_merchants = self._stats_bosses = self._stats_points = 0
        self._life_epoch += 1
        self.fisher = None
        self.start_biomes(self.url)
        self._sync_biomes_from_log()
        self.paused = False
        game.PAUSED.clear()
        self.running = True
        self.phase('running', GOOD)
        try:
            self._toast.banner('start', 2000)
        except Exception:
            pass
        self._guard_thread = threading.Thread(target=self._guard, daemon=True)
        self._guard_thread.start()
        return None

    def sync_runtime_cfg(self, patch=None):
        """Refresh fields cached at start so UI toggles apply live, without a restart."""
        patch = patch or {}
        try:
            self.url = clean_url(self.cfg_str('webhook_url'))
            self.wanted = self.wanted_items()
            self.collect_names = set(self.cfg.get('collect_merchants') or ['Jester', 'Mari', 'Rin'])
            self.use_items = self.cfg_bool('auto_items')
            self.reconnect_on = self.cfg_bool('auto_reconnect')
            self.rune_biomes = set(self.cfg.get('rune_biomes') or RUNE_BIOMES) if self.cfg_bool('rune_on') else set()
            self.rune_wait = self.rune_delay()
            self.selector_biome = self.cfg_str('selector_biome', 'HEAVEN') if self.cfg_bool('selector_on') else None
            if self.selector_biome and self.selector_due is None:
                self.selector_due = time.monotonic()
            elif not self.selector_biome:
                self.selector_due = None
            self.pop_list = self.pop_items()
            self.pop_biomes = set(self.pop_list) if self.cfg_bool('pop_on') else set()
            self.clip_min = clip_floor(self.cfg_str('clip_min', '1,000,000,000'))
            self.equip_aura = self._correct_aura() if self.cfg_bool('equip_on') else None
            if self.equip_aura and self.equip_due is None:
                self.equip_due = time.monotonic()
            elif not self.equip_aura:
                self.equip_due = None
            # new aura to keep equipped: schedule it promptly
            if 'equip_on' in patch or 'equip_aura' in patch:
                self.equip_fails = 0
                if self.equip_aura:
                    self.equip_due = time.monotonic()
            if 'rune_on' in patch or 'rune_biomes' in patch or 'rune_after' in patch:
                self.rune_due = (time.monotonic() + self.rune_wait) if self.rune_biomes else None
                self.rune_tries = 0
            if 'pop_on' in patch or 'pop_items' in patch:
                self.pop_due = time.monotonic() if self.pop_biomes else None
            if 'fish_on' in patch or 'eden_on' in patch:
                self.crack_on = self.cfg_bool('eden_on')
            note('cfg', 'settings applied live')
        except Exception as e:
            note('cfg', 'live apply failed: %s: %s' % (type(e).__name__, e))

    def tell(self, title, detail='', colour=None, area='Macro'):
        thumb = (asset_png('rune_icon.png') if area == 'Rune'
                 else biome_png(self.biome_now) if area in ('Biome', 'Pop', 'Selector') and self.biome_now else None)
        shade = colour if colour is not None else EMBED_INFO
        # failures carry a screenshot so you can see what the macro saw
        shot = _screen_shot() if shade == EMBED_BAD else None
        post_event(self.url, title, detail, shade, area, thumb=thumb, shot=shot)
        self._set(phase='%s%s' % (title, ' - ' + detail if detail else ''))

    def _guard(self):
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
        epoch = self._life_epoch
        try:
            for k in itertools.count():
                try:
                    self.play()
                    if not self.restart_run:
                        return
                except Exception as e:
                    try:
                        game.release_all()
                    except Exception:
                        pass
                    why = '%s: %s' % (type(e).__name__, e)
                    note('crash', '%s\n%s' % (why, traceback.format_exc()[-1500:]))
                    if game.ABORT and not self.restart_run:
                        self.tell('Macro crashed', '`%s`' % why, EMBED_BAD)
                        return
                    if not self.restart_run:
                        wait = CRASH_WAITS[min(k, len(CRASH_WAITS) - 1)]
                        self.tell('Macro crashed', '`%s`\nStarting again in %ds' % (why, wait), EMBED_BAD)
                        if not self.nap(wait):
                            return
                if self.restart_run:
                    self.restart_run = False
                    try:
                        game.release_all()
                    except Exception:
                        pass
                    note('run', 'run restarted after a stall')
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            self._commit_lifetime(epoch)
            self.running = False
            self.paused = False
            try:
                self._toast.banner('stop', 2000)
            except Exception:
                pass
            game.PAUSED.clear()
            if self.biome_stop is not None:
                self.biome_stop.set()
            if self.clipper is not None:
                self.clipper.stop()
            self._set(action='idle')

    def nap(self, seconds, halt=None):
        halt = halt or (lambda: game.ABORT)
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            self.beat = time.monotonic()
            if halt():
                return False
            if self.paused and not game.ABORT:
                # hold the wait while paused, so nothing runs and the paused time does not count
                t0 = time.perf_counter()
                time.sleep(0.2)
                end += time.perf_counter() - t0
                continue
            time.sleep(0.2)
        return not halt()

    def _pause_wait(self):
        """Hold the run while paused (releasing held keys); nothing is driven again until resume()."""
        try:
            game.release_all(focus=False)
        except Exception:
            pass
        self.phase('paused', WARN)
        while self.paused and not game.ABORT and not self._closing:
            self.beat = time.monotonic()
            time.sleep(0.2)
        if not game.ABORT and self.running:
            self.phase('running', GOOD)

    def play(self):
        game.ABORT = False
        game.forget_user_input()
        game.arm()
        game.fresh_session()
        game.set_pace(2.0 if self.slow_pc else 1.0)
        note('run', 'started: %s' % device_line())
        self.joined_at = time.monotonic()
        self.need_rejoin = self.out_since = None
        self.watch = game.Watch()
        self.watch.poll()
        if not game.roblox_running():
            self.need_rejoin = 'Roblox is not running'
        elif self.block_on and self.reconnect_on and self.loaded_already():
            note('merchant', 'this Roblox loaded a merchant or boss before the block: reopening it so every visit shows')
            game.close_roblox()
            self.need_rejoin = 'a merchant or boss was loaded before the block'
        self.got_past_block = False
        self.run_id += 1
        self.beat = time.monotonic()
        threading.Thread(target=self._watch_connection, args=(self.run_id,), daemon=True).start()
        if not self.ready_to_play():
            game.release_all()
            return
        self.fit_roblox()
        if self.need_rejoin is None:
            self.daily_later()
            self._claim_daily_now()

        seer = eden.Watcher() if self.cfg_bool('eden_on') else None
        afk_due = time.monotonic() + AFK_EVERY_S
        stray_due = time.monotonic() + STRAY_EVERY_S
        self.daily_at = next_daily()
        halt = lambda: game.ABORT or self.need_rejoin is not None
        busy = lambda: halt() or self.merchant_due is not None or self.pop_due is not None
        if self.fisher is not None:
            self._stats_fish += self.fisher.caught
            self._stats_points += self.fisher.points_earned
        self.fisher = fishing.Fisher(
            vip=self.cfg_bool('fish_vip', True),
            sell_every=self.sell_every(),
            sell_amount=self.sell_amount(),
            sell_all=self.cfg_bool('fish_sell_all', True),
            full_check=self.cfg_bool('fish_full_check', True),
            say=lambda m: (note('fishing', m), self._ai_observe_msg(m)),
            report=self.post_fishing,
            on_balance=self._set_fish_points,
        ) if self.cfg_bool('fish_on') else None
        self.check_chat_on_start()
        self.ai_preflight()
        self._set(phase='running', colour=GOOD)
        while not game.ABORT:
            if self.paused:
                self._pause_wait()
                if game.ABORT:
                    break
                continue
            if self.need_rejoin:
                self.merchant_due = None
                self._merchant_posted = {}
                if self.fisher:
                    self.fisher.hold = self.fisher.pause = False
                if not self.reconnect():
                    note('run', 'stopped: could not get back in')
                    self.tell('Could not get back in', 'Macro stopped', EMBED_BAD, 'Connection')
                    break
                self.joined_at = time.monotonic()
                self.got_past_block = False
                self.in_dimension = False
                self.fit_roblox()
                self.check_chat_on_start()
                if self.fisher:
                    self.fisher.lost()
                if self.crack_on and not self.need_rejoin:
                    self.use_crack(halt, 'back to Eden\'s dimension after the rejoin')
            if (self.got_past_block and self.block_on and self.reconnect_on and not self.merchant_due
                    and not (self.fisher and self.fisher.line_out) and not is_rare(self.biome_now)):
                self.got_past_block = False
                note('merchant', 'Roblox loaded a merchant or boss animation and keeps it: reopening it so every '
                                 'visit shows again')
                game.close_roblox()
                self.need_rejoin = 'a merchant or boss was loaded past the block'
                continue
            self.check_in_game()
            if self.need_rejoin:
                continue
            if time.monotonic() >= stray_due and not busy():
                stray_due = time.monotonic() + STRAY_EVERY_S
                self.close_strays()
            if self.pop_due and not self.need_rejoin:
                if self.fisher:
                    self.drop_fishing(halt, '%s started: stopping to pop' % pretty(self.pop_due))
                self.pop_turn(halt)
                if self.fisher:
                    self.fisher.lost()
                continue
            if self.merchant_due and time.monotonic() - self.merchant_at > MERCHANT_STALE_S:
                note('merchant', '%s came %d s ago: gone by now, not teleporting' % (self.merchant_due, MERCHANT_STALE_S))
                self.merchant_due = None
            if self.merchant_due and not self.need_rejoin:
                if self.fisher:
                    self.drop_fishing(halt, '%s arrived: stopping to go buy' % self.merchant_due)
                self.merchant_tries = getattr(self, 'merchant_tries', 0) + 1
                if self.collect_merchant(self.merchant_due, halt) is not False or self.merchant_tries >= MERCHANT_TRIES:
                    self.merchant_due, self.merchant_tries = None, 0
                if self.fisher:
                    self.fisher.lost()
                continue
            if self.use_items and not game.ABORT and self.items_due():
                self.items_turn(busy)
            if (self.rune_due is not None and time.monotonic() >= self.rune_due and not busy()):
                self.between_fish(busy, 'rune', 'Cursed Rune Fragment due', self.rune_turn)
            if (self.selector_due is not None and time.monotonic() >= self.selector_due and not busy()
                    and not rune_biome(self.biome_now, HOLD_BIOMES)
                    and not self.selector_landed()):
                self.between_fish(busy, 'selector', 'Biome Selector due', self.selector_turn)
            if time.time() >= self.daily_at and not busy():
                self.between_fish(busy, 'daily', 'daily rewards reset', self.daily_reset_turn)
            if (time.monotonic() < getattr(self, '_daily_until', 0)
                    and time.monotonic() >= getattr(self, 'daily_by', 0) and not busy()):
                self._claim_daily_now()
                if time.monotonic() < getattr(self, '_daily_until', 0):
                    self.daily_by = time.monotonic() + 20
            elif (getattr(self, '_daily_until', 0) and time.monotonic() >= self._daily_until
                    and not getattr(self, '_daily_noted', False)):
                self._daily_noted = True
                game.before_respawn = None
                note('daily', 'no daily rewards window')
            if (self.equip_due is not None and time.monotonic() >= self.equip_due and not busy()):
                self.between_fish(busy, 'equip', 'putting %s back on' % self.equip_aura, self.equip_turn)
            if seer is not None and not self.need_rejoin and seer.seen():
                self.eden_found()
                afk_due = time.monotonic() + EDEN_AFK_REST_S
                self.nap(eden.COOLDOWN_S, busy)
                if self.fisher:
                    self.fisher.lost()
            if self.fisher is not None and not busy():
                if self.fisher.pause and not self.fisher.line_out:
                    self.fisher.pause, self.fisher.recast = False, self.fisher.at_spot
                step_ok = False
                try:
                    step_ok = bool(self.fisher.step(busy, LOOK_EVERY_S * 4))
                except Exception as e:
                    note('fishing', 'failed - %s: %s' % (type(e).__name__, e))
                    self.ai_record_issue('sell', '%s: %s' % (type(e).__name__, e))
                    self.ai_heal_screen('sell')
                    self.fisher.lost()
                if self.fisher.rejoin and not is_rare(self.biome_now):
                    why, self.fisher.rejoin = self.fisher.rejoin, False
                    if 'Collection' in (why or ''):
                        self.ai_record_issue('collection', why)
                        self.ai_heal_screen('collection')
                    if self.reconnect_on:
                        game.close_roblox()
                        self.need_rejoin = why
                    else:
                        note('fishing', 'not rejoining: Auto reconnect is off')
                self.beat = time.monotonic()
                if not step_ok and not busy():
                    self.ai_heal_screen('fishing')
                    self.nap(LOOK_EVERY_S)
                continue
            if self.cfg_bool('anti_afk', True) and time.monotonic() >= afk_due and not self.need_rejoin:
                self.jump()
                afk_due = time.monotonic() + AFK_EVERY_S
            self.nap(LOOK_EVERY_S)

        game.release_all()
        if self.restart_run:
            note('run', 'run ended by the stall watchdog')
            return
        note('run', 'run ended%s' % (' (Stop pressed)' if game.ABORT else ''))
        # Post macro stats to Discord webhook on stop
        if self.url:
            try:
                sess = self._session_totals()
                total_fish = sess['fish']
                points = sess['points']
                merchants = sess['merchants']
                boss_alerts = sess['bosses']
                ended = 'Stop pressed' if game.ABORT else 'Run finished'
                length = span(time.time() - self.run_wall) if self.run_wall else ''
                post_run_summary(
                    self.url, total_fish, points, merchants, boss_alerts,
                    player=self.player_name or self.cfg_str('roblox_username'),
                    length=length, ended=ended,
                    server=self.server_link or self.cfg_str('private_server'),
                )
                note('webhook', 'stats: %d fish, %s points, %d merchants, %d bosses'
                     % (total_fish, format(points, ','), merchants, boss_alerts))
            except Exception as e:
                note('webhook', 'stats post failed: %s: %s' % (type(e).__name__, e))

    def jump(self):
        try:
            if game.take_foreground(tries=2) is not None:
                note('run', 'anti-AFK: two jumps')
                game.tap(' ', gap=0.2)
                game.tap(' ')
        except Exception as e:
            note('run', 'anti-AFK skipped: %s: %s' % (type(e).__name__, e))

    def eden_found(self):
        halt = lambda: game.ABORT or self.need_rejoin is not None
        ping = self.cfg_str('eden_ping')
        note('eden', 'Eden on screen')
        self._set(phase='Eden on screen', colour=GOOD)
        game.take_foreground(tries=2)
        if self.cfg_bool('eden_record'):
            threading.Thread(target=eden.record, args=(eden.RECORD_S, halt), daemon=True).start()
        post_shot(self.url, 'Eden Detected', EMBED_GOOD, ping, area='Eden')
        if not self.nap(eden.SETTLE_S, halt):
            return
        path, began, go = self.cfg_bool('eden_path', True), time.monotonic(), 0
        while not halt():
            go += 1
            tried = time.monotonic()
            if self.eden_try(path, go, halt):
                note('eden', 'contract signed (go %d)' % go)
                post_shot(self.url, 'Eden Obtained', EMBED_GOLD, ping, area='Eden')
                return
            if halt():
                return
            if path and time.monotonic() - began >= EDEN_GIVE_UP_S:
                note('eden', 'no contract in %d minutes: giving up' % (EDEN_GIVE_UP_S // 60))
                self.tell('Eden Contract not seen', 'Tried for %d minutes, giving up' % (EDEN_GIVE_UP_S // 60), EMBED_BAD,
                          'Eden')
                return
            note('eden', 'the contract never showed (go %d): trying again' % go)
            if go == 1:
                self.tell('Eden Contract not seen', 'Trying again every %ds' % EDEN_RETRY_S, EMBED_BAD, 'Eden')
            if not self.nap(max(0.0, EDEN_RETRY_S - (time.monotonic() - tried)), halt):
                return

    def eden_try(self, path, go, halt):
        try:
            if path:
                if go > 1:
                    if not fishing.respawn(halt):
                        return False
                    self.in_dimension = False
                if not self.in_dimension and not self.use_crack(halt, 'the Eden walk starts at its spawn (go %d)' % go):
                    return False
                if not game.align_camera(halt):
                    note('eden', 'Collection did not square the camera up: not walking')
                    return False
                eden.tilt()
                note('eden', 'walking the path (go %d)' % go)
                eden.walk(halt)
                time.sleep(0.5)
            if halt():
                return False
            game.down('e')
            time.sleep(0.1)
            game.up('e')
        except game.NotFocused as e:
            note('eden', 'not this go: %s' % e)
            return False
        if not self.nap(eden.TALK_S, halt):
            return False
        if not game.press_point('contract', settle=0.2):
            note('eden', 'the Contract button was not pressed: press Set Contract on the Eden tab')
            return False
        return eden.signed(halt)

    def _watch_connection(self, run_id=0):
        last = time.time()
        while self.running and not self._closing and run_id == self.run_id:
            gap, last = time.time() - last, time.time()
            if gap > SLEPT_S:
                self.beat = time.monotonic()
                note('run', 'the PC was asleep for %d minutes: nothing ran' % round(gap / 60))
                self.tell('PC was asleep', 'Nothing ran for %d minutes. Veyra keeps the PC awake, but not through a '
                          'closed lid, Sleep or a flat battery' % round(gap / 60), EMBED_BAD)
            watch = self.watch
            if watch is not None and not self.rejoining and self.need_rejoin is None:
                try:
                    why = watch.trouble()
                except Exception:
                    why = None
                if why:
                    self.need_rejoin = why
                    try:
                        game.release_all()
                    except Exception:
                        pass
            if time.monotonic() - self.beat > STALL_S and not self.restart_run and not game.ABORT:
                note('run', 'the run has not moved for %d minutes: restarting it' % (STALL_S // 60))
                self.tell('Macro stuck', 'No progress for %d minutes, restarting the run' % (STALL_S // 60), EMBED_BAD)
                self.restart_run = True
                game.ABORT = True
            time.sleep(WATCH_EVERY)

    def connected(self, watch=None):
        watch = watch or game.Watch()
        watch.poll()
        return game.roblox_running() and watch.state == 'joined' and game.in_game(watch)

    def ready_to_play(self):
        watch = self.watch or game.Watch()
        why = watch.trouble(quiet_s=float('inf'))
        if why and game.roblox_running() and game.find_play() is None:
            note('run', 'the log says "%s", but the client is in a game: starting anyway' % why)
            why = watch.state = watch.dropped = None
        if why:
            self.need_rejoin = why
            if not self.reconnect():
                self.tell('Roblox is not ready', capital(why), EMBED_BAD, 'Connection')
                return False
            self.joined_at = time.monotonic()
        return True

    def fit_roblox(self):
        try:
            def say(m):
                note('window', m)
                if 'F11' in m:
                    self._set(toast=m)
            game.fit_window(say=say)
        except Exception as e:
            note('window', 'sizing the Roblox window failed: %s: %s' % (type(e).__name__, e))

    def check_chat_on_start(self):
        """Double-check Roblox chat at start: if open, close it and confirm it stayed closed."""
        try:
            if game.roblox_window() is None:
                note('chat', 'Roblox is not open: skipping the chat check')
                return None
            game.paced(0.4)
            if game.chat_open() is False:
                game.paced(0.4)                       # double check before trusting the first read
                if game.chat_open() is False:
                    note('chat', 'chat is OFF (checked twice)')
                    return True
            note('chat', 'chat is ON at start: turning it off')
            for attempt in range(3):
                game.close_chat()
                game.paced(0.5)
                if game.chat_open() is False:
                    game.paced(0.4)                   # confirm again after closing
                    if game.chat_open() is False:
                        note('chat', 'chat turned OFF and confirmed (attempt %d)' % (attempt + 1))
                        return True
                note('chat', 'chat still open after attempt %d, retrying' % (attempt + 1))
            note('chat', 'chat is still ON after 3 attempts')
            return False
        except Exception as e:
            note('chat', 'chat check failed: %s: %s' % (type(e).__name__, e))
            return None

    def _ai_memory_path(self):
        return os.path.join(game.APP_DIR, 'ai_memory.json')

    def _load_ai_memory(self):
        try:
            with open(self._ai_memory_path(), encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_ai_memory(self, data):
        try:
            os.makedirs(game.APP_DIR, exist_ok=True)
            with open(self._ai_memory_path(), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def _scan_previous_run_logs(self):
        """Read the previous run slice in logs.txt and classify issues it hit."""
        path = os.path.join(game.APP_DIR, 'logs.txt')
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()[-250:]
        except OSError:
            return set()
        starts = [i for i, ln in enumerate(lines) if '[run] started:' in ln]
        if len(starts) >= 2:
            prev = lines[starts[-2]:starts[-1]]
        elif len(starts) == 1:
            prev = lines[:starts[0]]
        else:
            prev = lines
        issues = set()
        for ln in prev:
            if 'Collection would not open' in ln or 'Collection did not open' in ln or 'Collection would not shut' in ln:
                issues.add('collection')
            if 'Sell Alls went unsold' in ln or 'sale did not go through' in ln or '[fishing] failed' in ln:
                issues.add('sell')
            if 'No bite in 30 s' in ln or 'still no bite' in ln or "Flarg's message never came up" in ln:
                issues.add('route')
            if 'NotFocused' in ln or 'would not come forward' in ln:
                issues.add('focus')
            if 'chat is ON' in ln or 'chat is open' in ln:
                issues.add('chat')
        return issues

    def ai_record_issue(self, kind, detail=''):
        """Persist a runtime failure lesson so the next run auto-corrects for it."""
        mem = self._load_ai_memory()
        counts = dict(mem.get('counts') or {})
        counts[kind] = int(counts.get(kind, 0)) + 1
        mem['counts'] = counts
        mem['pending'] = sorted(set(mem.get('pending') or []) | {kind})
        mem['last_detail'] = (detail or '')[:160]
        mem['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        self._save_ai_memory(mem)

    def _ai_observe_msg(self, msg):
        m = msg or ''
        if 'Collection would not open' in m or 'Collection would not shut' in m:
            self.ai_record_issue('collection', m)
        elif 'Sell Alls went unsold' in m or 'sale did not go through' in m:
            self.ai_record_issue('sell', m)
        elif 'still no bite' in m or "Flarg's message never came up" in m:
            self.ai_record_issue('route', m)
        elif 'fish ' in m and 'caught' in m:
            # Successful catches decay pending warnings once the correction worked
            mem = self._load_ai_memory()
            if mem.get('pending'):
                mem['pending'] = []
                self._save_ai_memory(mem)

    def ai_heal_screen(self, reason=''):
        """Clear stuck chat, menus, shop, dialogue, and keys before resuming."""
        try:
            game.release_all()
        except Exception:
            pass
        if game.roblox_window() is None:
            return
        try:
            if game.chat_open():
                note('ai', 'auto-correct (%s): closing open chat' % (reason or 'recover'))
                game.close_chat()
            if game.collection_open() is True:
                note('ai', 'auto-correct (%s): closing stuck Collection' % (reason or 'recover'))
                game.press_point('exit_collection', settle=0.4)
            if any(k in (reason or '').lower() for k in ('shop', 'sell', 'fishing', 'collection')):
                points.click('fishingShopCloseButton', 0.2)
            points.leave_dialogue(skip=True)
            self.close_guis()
        except Exception as e:
            note('ai', 'screen cleanup note: %s: %s' % (type(e).__name__, e))

    def ai_preflight(self):
        """Check mistakes from the previous run and apply automatic fixes before starting."""
        mem = self._load_ai_memory()
        issues = set(mem.get('pending') or []) | self._scan_previous_run_logs()
        if not issues:
            note('ai', 'previous run finished cleanly (no errors to correct)')
            game.COLLECTION_EXTRA_S = 0.0
            return
        actions = []
        if 'collection' in issues:
            game.COLLECTION_EXTRA_S = 0.3
            actions.append('added +0.3s Collection hover/exit settle')
        else:
            game.COLLECTION_EXTRA_S = 0.0
        if 'sell' in issues:
            fishing.slow = min(fishing.SLOW_MAX, max(fishing.slow, 0.15))
            actions.append('set Sell All guard delay +%.2fs' % fishing.slow)
        if 'focus' in issues:
            game.take_foreground(tries=3)
            actions.append('re-focused Roblox')
        if issues & {'collection', 'sell', 'route', 'chat'}:
            self.ai_heal_screen('last run had: ' + ', '.join(sorted(issues)))
            actions.append('cleared chat/shop/menus')
        note('ai', 'auto-corrected from last run (%s): %s' % (', '.join(sorted(issues)), '; '.join(actions)))
        mem['last_applied'] = sorted(issues)
        mem['pending'] = []
        self._save_ai_memory(mem)

    def reconnect(self):
        self.rejoining = True
        try:
            back = self._reconnect()
        finally:
            self.rejoining = False
        return back

    def _reconnect(self):
        why, self.need_rejoin = self.need_rejoin, None
        self.out_since = None
        if 'by itself' in (why or '') and self.connected():
            note('connection', 'already back in the game (%s)' % why)
            self.daily_later()
            self.watch = game.Watch()
            return True
        # read live so toggling Auto Reconnect in the UI takes effect immediately
        if not self.cfg_bool('auto_reconnect'):
            return self.wait_for_game(why or '')
        note('connection', 'reconnecting: %s' % (why or ''))
        post_status(self.url, 'Reconnecting', capital(why), EMBED_BAD, 'Connection')
        k = -1
        while True:
            k += 1
            self.beat = time.monotonic()
            if game.ABORT:
                return False
            failed = ''
            try:
                ok = game.rejoin(self.server_link, stop=lambda: game.ABORT, browser=self.browser_rejoin,
                                 frozen=k > 0 or (why or '').startswith(game.FROZEN),
                                 say=lambda m: (note('connection', m), self._set(phase='reconnecting - %s' % m, colour=DIM)))
                self.beat = time.monotonic()
                if ok and not self.wait_in_game():
                    ok, failed = False, 'not in the game %d s after joining' % IN_GAME_S
            except Exception as e:
                ok, failed = False, '%s: %s' % (type(e).__name__, e)
            finally:
                self.watch = game.Watch()
            if game.ABORT:
                return False
            if ok:
                note('connection', 'rejoined')
                post_status(self.url, 'Rejoined the private server', '', EMBED_GOOD, 'Connection')
                self.daily_later()
                return True
            wait = REJOIN_WAITS[min(k, len(REJOIN_WAITS) - 1)]
            note('connection', 'rejoin %d failed (%s), again in %d s' % (k + 1, failed or 'see above', wait))
            post_status(self.url, 'Could not rejoin',
                        '%sTrying again in %ds' % (capital(failed) + '\n' if failed else '', wait),
                        EMBED_BAD, 'Connection')
            if not self.nap(wait):
                return False

    def wait_for_game(self, why):
        note('connection', 'auto reconnect is off: waiting for the game to be back (%s)' % why)
        self.tell('Disconnected', 'Auto Reconnect is disabled', EMBED_BAD, 'Connection')
        clicked, watch = 0.0, game.Watch()
        while self.nap(5):
            if not game.roblox_running():
                continue
            if self.connected(watch):
                note('connection', 'back in the game')
                self.tell('Back in the game', '', EMBED_GOOD, 'Connection')
                self.daily_later()
                self.watch = game.Watch()
                return True
            if game.find_play() is not None and time.monotonic() - clicked > PLAY_EVERY_S:
                self.press_play()
                clicked = time.monotonic()
        return False

    def wait_in_game(self, limit=IN_GAME_S):
        end, clicked, clear_since = time.monotonic() + limit, 0.0, None
        watch = game.Watch()
        while time.monotonic() < end and not game.ABORT:
            if not game.roblox_running():
                return False
            watch.poll()
            if watch.state == 'joined' and game.in_game(watch):
                return True
            # skip the FLASH WARNING / intro cutscene so the rejoin doesn't sit on it
            try:
                if self.cfg_bool('skip_intro', True) and game.flash_warning_up():
                    game.skip_intro(stop=lambda: game.ABORT,
                                    say=lambda m: note('connection', m))
            except Exception:
                pass
            if game.find_play() is not None or (watch.in_menu and time.time() - watch.menu_at < game.MENU_S):
                clear_since = None
                if time.monotonic() - clicked > PLAY_EVERY_S:
                    self.press_play()
                    clicked = time.monotonic()
            elif watch.path is None or watch.state == 'joined':
                clear_since = clear_since or time.monotonic()
                if time.monotonic() - clear_since > 20:
                    return True
            time.sleep(2)
        return False

    def press_play(self):
        try:
            at = game.find_play()
            if at is not None:
                game.click(*at)
            elif not game.press_point('play', settle=1.0):
                return
            note('connection', 'pressed Play (%s)' % ('seen' if at else 'Set Play' if 'play' in game.load_points()
                                                       else 'built-in spot'))
        except Exception as e:
            note('connection', 'pressing Play failed: %s: %s' % (type(e).__name__, e))

    def check_in_game(self):
        now, mono = time.time(), time.monotonic()
        if mono < self._look_at or self.need_rejoin:
            return
        self._look_at = mono + 5
        watch = self.watch
        logged_in = watch is not None and watch.path is not None and game.in_game(watch)
        menu = watch is not None and watch.in_menu and now - watch.menu_at < game.MENU_S
        out = game.roblox_running() and not logged_in and (menu or game.find_play() is not None)
        if not out:
            self.out_since = None
            if self.played and logged_in:
                self.played = False
                self.daily_later()
            return
        self.out_since = self.out_since or mono
        if mono - self._played > PLAY_EVERY_S:
            self.press_play()
            self._played = mono
            self.played = True
        if mono - self.out_since > OUT_S:
            note('connection', 'not in a game for %d minutes' % (OUT_S // 60))
            self.out_since = None
            self.need_rejoin = 'not in a game for %d minutes' % (OUT_S // 60)

    def daily_reset_turn(self):
        self.daily_at = next_daily()
        self.daily_later()
        self.claim_daily()

    def daily_later(self):
        """Watch for the login reward for a few minutes. Claim it before a reset or Collection closes it."""
        game.after_align = self.claim_daily
        game.before_respawn = self._claim_daily_now
        self._daily_until = time.monotonic() + 180
        self._daily_noted = False
        self.daily_by = time.monotonic() + 12

    def _claim_daily_now(self):
        """One look. Used before Esc and between fish so a visible panel is claimed, not dismissed."""
        if game.ABORT or getattr(self, '_daily_busy', False):
            return
        try:
            up = game.daily_state() is not None or game._daily_x()
        except Exception:
            return
        if up:
            self.claim_daily(wait=0)

    def claim_daily(self, wait=None):
        if game.ABORT or getattr(self, '_daily_busy', False):
            return
        self._daily_busy = True
        try:
            try:
                if wait is None:
                    game.paced(game.DAILY_WAIT_S)
                done = game.daily_by_clicks(wait=game.DAILY_SHOW_S if wait is None else wait)
            except Exception as e:
                done = None
                note('daily', 'crashed: %s: %s' % (type(e).__name__, e))
        finally:
            self._daily_busy = False
        if done not in (False, None):
            self._daily_until = 0
            self._daily_noted = True
            game.before_respawn = None
            if game.after_align == self.claim_daily:
                game.after_align = None
        if done == 'stuck':
            note('daily', 'the Daily Rewards window would not close: clearing it before the next action')
            try:
                game.close_guis()
            except Exception:
                pass
            return
        if done is False and wait == 0:
            return
        if done is False and time.monotonic() < getattr(self, '_daily_until', 0):
            return
        note('daily', {'claim': 'daily rewards claimed', 'claimed': 'daily rewards already claimed',
                       'closed': 'daily rewards window closed', False: 'no daily rewards window'}.get(
                           done, 'daily rewards check failed'))

    def post_fishing(self, detail):
        if not self.url:
            return
        try:
            post_alert(self.url, 'Fishing failsafe', detail, EMBED_BAD, 'Fishing', 'fishing_icon.png', '🎣')
        except Exception as e:
            note('fishing', 'webhook: %s: %s' % (type(e).__name__, e))

    def collect_merchant(self, name, halt):
        silent = self.cfg_bool('silent_merchant')
        say = lambda m: note('merchant', m)
        self._set(phase='going to %s' % name, colour=GOOD)
        moved, got = True, None
        try:
            if self.fisher is not None and not fishing.respawn(halt):
                return
            game.tilt_up(halt)
            got = shop.collect(name, self.wanted, halt, say)
            moved = got is not False
            if not got:
                if not halt() and getattr(self, 'merchant_tries', MERCHANT_TRIES) >= MERCHANT_TRIES:
                    if not silent:
                        self.tell("Could not open %s's shop" % name, shop.why or 'The shop did not open',
                                  EMBED_BAD, 'Merchant')
                elif not halt():
                    say('%s: trying again while %s is here' % (shop.why or 'the shop did not open', name))
                self.close_guis()
                return False
            found, bought = got
            # Rin always has the same stock: only announce that it spawned, no manifest.
            if name != 'Rin' and not silent:
                items = ', '.join(found[s] for s in sorted(found)) or 'Could not read the items'
                post_event(self.url, '%s Shop Manifest — %s' % (MERCHANT_BADGES.get(name, '🛰️'), name),
                           '-# Radar scan complete · shop inventory read',
                           MERCHANT_COLOURS.get(name, EMBED_GOLD), area='Merchant', merchant=name,
                           fields=[('📦 Stock read', items, False),
                                   ('🛍️ Bought', '\n'.join(bought) or 'Nothing', False)])
            time.sleep(1.0)
            shop.close_shop()
        except Exception as e:
            say('failed - %s: %s' % (type(e).__name__, e))
            if not silent:
                self.tell('Merchant collection failed', '`%s: %s`' % (type(e).__name__, e), EMBED_BAD, 'Merchant')
            self.merchant_tries = MERCHANT_TRIES
            try:
                shop.close_shop()
            except Exception:
                pass
            self.close_guis()
        finally:
            if not halt():
                if moved:
                    fishing.respawn(halt)
                game.align_camera(halt)
            if moved:
                self.in_dimension = False
                if self.crack_on and not halt() and (got or self.merchant_tries >= MERCHANT_TRIES):
                    self.use_crack(halt, 'back to Eden\'s dimension after %s' % name)

    def use_crack(self, halt, why):
        say = lambda m: note('eden', m)
        say('using a Portable Crack: %s' % why)
        try:
            ok = shop.use_item(CRACK, halt, say) and self.nap(CRACK_S, halt)
        except Exception as e:
            say('Portable Crack failed - %s: %s' % (type(e).__name__, e))
            self.close_guis()
            ok = False
        self.in_dimension = bool(ok)
        if not ok and not halt():
            self.tell('Portable Crack not used', 'Ensure you have a Portable Crack', EMBED_BAD, 'Eden')
        return self.in_dimension

    def drop_fishing(self, halt, why):
        note('fishing', why)
        self.fisher.pause = self.fisher.hold = False
        try:
            if not self.fisher.at_spot:
                fishing.points.click('fishingShopCloseButton', 0.2)
                fishing.points.leave_dialogue(skip=True)
            else:
                if self.fisher.line_out:
                    self.fisher.reel_in(halt)
                end = time.monotonic() + LATE_RESULT_S
                while time.monotonic() < end and not halt():
                    st = fishing.window_state()
                    if st in ('catch', 'failed'):
                        fishing.close_catch(halt)
                        break
                    if st == 'fish':
                        break
                    time.sleep(0.2)
        except Exception as e:
            note('merchant', 'reeling in failed - %s: %s' % (type(e).__name__, e))
        self.fisher.line_out = False

    def dropped(self):
        return 'merchant' if self.merchant_due is not None else 'pop' if self.pop_due is not None else ''

    def items_turn(self, halt):
        self.between_fish(halt, 'items', 'SC/BR due', self.spend_items)

    def between_fish(self, halt, tag, why, work):
        if self.fisher and self.fisher.at_spot and self.fisher.line_out:
            if not self.fisher.pause:
                self.fisher.pause = True
                note(tag, '%s: landing the fish on the line first' % why)
            return
        if self.fisher and fishing.window_state() == 'catch':
            fishing.close_catch(halt)
        game.INTERRUPT = self.dropped
        try:
            work()
            # double-check: clear any Inventory/menu left open by a swallowed click
            try:
                self.close_guis()
                game.release_all()
            except Exception:
                pass
        finally:
            game.INTERRUPT = None
            if self.dropped():
                note(tag, 'stopped for the %s' % self.dropped())
            if self.fisher:
                self.fisher.pause = False
                self.fisher.recast = self.fisher.at_spot and not self.fisher.line_out

    def equip_turn(self):
        aura, say = self.equip_aura, lambda m: note('equip', m)
        halt = lambda: (game.ABORT or self.need_rejoin is not None or self.merchant_due is not None
                        or self.pop_due is not None)

        def logged(since):
            name, at = self.aura_logged
            return bool(name) and clipper.norm(name) == clipper.norm(aura) and at >= max(since, self.run_wall)

        def other(since):
            name, at = self.aura_logged
            same = clipper.norm(name or '') == clipper.norm(aura)
            return name if name and name != '_None_' and not same and at >= max(since, self.run_wall) else None

        self.equip_due = None
        self.equip_busy_at = time.monotonic()
        try:
            on = equip.equip(aura, halt, say, logged, other)
        except Exception as e:
            say('failed - %s: %s' % (type(e).__name__, e))
            self.close_guis()
            on = False
        self.equip_busy_at = time.monotonic()
        if on:
            self.equip_fails = 0
        elif not halt():
            self.equip_fails += 1
            wait = min(EQUIP_RETRY_S * 2 ** (self.equip_fails - 1), 60 * 60)
            self.equip_due = time.monotonic() + wait
            say('trying again in %d minutes' % (wait // 60))
            if self.equip_fails == 1:
                post_alert(self.url, 'Could not equip %s' % aura,
                           'Trying again in %d minutes' % (wait // 60), EMBED_BAD, 'Aura', 'aura_icon.png', '⚠️')
        else:
            self.equip_due = time.monotonic()

    def rune_biome_changed(self, name):
        biome = rune_biome(name, self.rune_biomes)
        if biome is None:
            self.rune_due = None
            return
        if self.boss_present():
            self.rune_due = None
            note('rune', '%s started, but a boss is here: no Cursed Rune Fragment used' % pretty(biome))
            return
        self.rune_due, self.rune_tries = time.monotonic() + self.rune_wait, 0
        note('rune', '%s started: a Cursed Rune Fragment in %d s' % (pretty(biome), self.rune_wait))

    def rune_turn(self):
        say = lambda m: note('rune', m)
        biome = rune_biome(self.biome_now, self.rune_biomes)
        if biome is None:
            say('the biome is over (%s now): no Cursed Rune Fragment used' % (pretty(self.biome_now) or 'unknown'))
            self.rune_due = None
            return
        if self.boss_present():
            say('a boss is here: no Cursed Rune Fragment used')
            self.rune_due = None
            return
        try:
            used = game.item_pass(say=say, terms=(RUNE,))
        except Exception as e:
            used = []
            say('failed - %s: %s' % (type(e).__name__, e))
            self.close_guis()
        if not used and self.dropped():
            return
        self.rune_tries += 1
        if used:
            self.rune_due = None
            self.tell('Used a Cursed Rune Fragment', pretty(biome), EMBED_GOOD, 'Rune')
        elif self.rune_tries < RUNE_TRIES:
            self.rune_due = time.monotonic() + RUNE_RETRY_S
        else:
            self.rune_due = None
            self.tell('Cursed Rune Fragment not used', 'Ensure you have one', EMBED_BAD, 'Rune')

    def pop_biome_changed(self, name):
        biome = rune_biome(name, self.pop_biomes)
        self.pop_due = biome if biome and self.pop_list.get(biome) else None
        self.pop_left = None
        if self.pop_due:
            note('pop', '%s started: popping %s' % (pretty(biome), ', '.join(
                '%s x%d' % (n, a) for n, a in self.pop_list[biome].items())))

    def pop_turn(self, halt):
        biome, self.pop_due = self.pop_due, None
        left = getattr(self, 'pop_left', None)
        left = dict(left[1]) if left and left[0] == biome else dict(self.pop_list.get(biome, {}))
        self.pop_left = None
        say = lambda m: note('pop', m)
        over = lambda: halt() or rune_biome(self.biome_now, {biome}) is None
        used = []
        try:
            for name, amount in list(left.items()):
                if over():
                    break
                n = potions.use(name, amount, over, say)
                if n:
                    used.append('%s x%d' % (name, n))
                left[name] = amount - n if halt() else 0
                if left[name] <= 0:
                    del left[name]
        except Exception as e:
            say('failed - %s: %s' % (type(e).__name__, e))
        finally:
            self.close_guis()
        if left and rune_biome(self.biome_now, {biome}) is None:
            say('%s is over (%s now): stopping' % (pretty(biome), pretty(self.biome_now) or 'unknown'))
        elif left and halt() and not game.ABORT and self.pop_due is None:
            self.pop_left, self.pop_due = (biome, left), biome
            say('interrupted: %s left for after the rejoin' % ', '.join(left))
        if used:
            self.tell('Popped in %s' % pretty(biome), ', '.join(used), biome_colour(biome), 'Pop')
        elif not halt():
            self.tell('Nothing popped in %s' % pretty(biome), 'None of the listed potions were found', EMBED_BAD, 'Pop')

    def selector_landed(self):
        return base_biome(self.biome_now).replace(' ', '') == self.selector_biome.replace(' ', '')

    def selector_turn(self):
        say = lambda m: note('selector', m)
        halt = lambda: game.ABORT or self.need_rejoin is not None or self.merchant_due is not None or self.pop_due
        biome = self.selector_biome
        drive = selector.DRIVES[biome]
        try:
            outcome, wait = selector.use(biome, halt, say)
        except Exception as e:
            outcome, wait = 'refused', None
            say('failed - %s: %s' % (type(e).__name__, e))
            self.close_guis()
        now = time.monotonic()
        self.selector_refused = self.selector_refused + 1 if outcome == 'refused' else 0
        missed = outcome in ('no selector', 'no drive')
        self.selector_missed = getattr(self, 'selector_missed', 0) + 1 if missed else 0
        if missed:
            wait = min(SELECTOR_RETRY_S * 2 ** (self.selector_missed - 1), 60 * 60)
            self.selector_due = now + wait
            what = 'No Biome Selector found in the Inventory' if outcome == 'no selector' else \
                '%s is not in its list (not unlocked yet?)' % drive
            say('%s (miss %d): looking again in %d min' % (what, self.selector_missed, wait // 60))
            if self.selector_missed == SELECTOR_MISSES:
                self.tell('Biome Selector not used', '%s. Trying again every hour at most' % what, EMBED_BAD,
                          'Selector')
        elif self.selector_refused >= SELECTOR_REFUSALS:
            self.selector_due = None
            self.tell('Biome Selector stopped', 'Refused %d times in a row: out of %s?' % (SELECTOR_REFUSALS, drive),
                      EMBED_BAD, 'Selector')
        elif outcome == 'used':
            self.selector_due = now + selector.EVERY_S
            self.tell('Biome Selector used', '%s (%s)' % (pretty(biome), drive), EMBED_GOOD, 'Selector')
        elif outcome == 'recharging':
            self.selector_due = now + wait + ITEM_SPARE_S
            say('next try in %d min' % ((wait + ITEM_SPARE_S) // 60))
        elif outcome == 'stopped':
            self.selector_due = now
        elif outcome == 'refused':
            self.selector_due = now + SELECTOR_REFUSED_S
        elif outcome == 'unstable':
            self.selector_due = now + SELECTOR_RETRY_S
        else:
            self.selector_due = None
            self.tell('Biome Selector stopped', 'Unknown outcome: %s' % outcome, EMBED_BAD, 'Selector')

    def items_due(self):
        now = time.monotonic()
        return (not rune_biome(self.biome_now, HOLD_BIOMES)
                and any(now >= self.item_due.get(t, 0.0) for t in (game.BR, game.SC)))

    def spend_items(self):
        say = lambda m: note('items', m)
        now = time.monotonic()
        due = tuple(t for t in (game.BR, game.SC) if now >= self.item_due.get(t, 0.0))
        if not due:
            return False
        try:
            got = game.item_pass(say=say, terms=due)
            done = time.monotonic()
            cut = self.dropped()
            tried = [t for t in due if t in got or not cut]
            for t in tried:
                self.item_due[t] = done + (ITEM_EVERY_S[t] + ITEM_SPARE_S if t in got else ITEM_RETRY_S)
            for t in set(due) - set(tried):
                say('%s: left for after the %s' % (t, cut))
        except Exception as e:
            for t in due:
                self.item_due[t] = now + ITEM_RETRY_S
            say('failed - %s: %s' % (type(e).__name__, e))
            self.tell('Auto SC + BR failed', '`%s: %s`' % (type(e).__name__, e), EMBED_BAD, 'Auto SC + BR')
            self.close_guis()
            return True
        if not tried:
            return False
        missed = ', '.join(pretty(t) for t in tried if t not in got)
        if got:
            self.tell('Used a ' + ' and a '.join(pretty(t) for t in got), missed and "%s wasn't used" % missed,
                      EMBED_INFO, 'Auto SC + BR')
        else:
            self.tell('Found nothing to use', missed, EMBED_INFO, 'Auto SC + BR')
        if missed:
            self.close_guis()
        return True

    def close_strays(self):
        try:
            if game.daily_open():
                note('menus', 'the Daily Rewards window was open: claiming it')
                game.close_daily()
                return
            if not game.inventory_open():
                return
        except Exception:
            return
        note('menus', 'a window was left open (a cutscene hid it?): closing it')
        self.close_guis()
        if self.fisher:
            self.fisher.line_out = False
            self.fisher.recast = self.fisher.at_spot

    def close_guis(self):
        try:
            if self.fisher and fishing.window_state() in ('catch', 'failed'):
                fishing.close_catch(lambda: game.ABORT)
            game.close_guis()
        except Exception as e:
            note('menus', 'closing menus failed: %s: %s' % (type(e).__name__, e))

    def pause(self, why='You used the PC'):
        """Pause the run without stopping it. resume() continues where it left off."""
        if not self.running or self.paused:
            return False
        self.paused = True
        game.PAUSED.set()
        try:
            game.release_all(focus=False)       # let go of held keys, but keep the user's window focused
        except Exception:
            pass
        note('run', 'paused: %s' % why)
        self.notice('Paused - resumes when you stop using the PC', ACCENT, 5000)
        self.phase('paused', WARN)
        return True

    def resume(self):
        """Continue a paused run; the input that woke it is not counted as the user taking over."""
        if not self.paused:
            return False
        self.paused = False
        game.PAUSED.clear()
        game.forget_user_input()
        note('run', 'resumed')
        self.notice('Resumed', GOOD, 3000)
        self.phase('running', GOOD)
        return True

    def _watch_user_pc(self):
        """Pause the run when the person at the PC moves the mouse or presses a key, and resume once idle."""
        while not self._closing:
            try:
                if self.running and not game.ABORT and self.cfg_bool('yield_pc', True):
                    if game.user_took_over():
                        if not self.paused:
                            self.pause()
                    elif self.paused and game.user_idle_seconds() >= PC_RESUME_IDLE_S:
                        self.resume()
                elif self.paused:
                    self.resume()
            except Exception:
                pass
            time.sleep(0.15)

    def stop(self):
        note('run', 'Stop pressed')
        self.start_again = False
        self.restart_run = False
        self.paused = False
        game.PAUSED.clear()
        game.ABORT = True
        if self.biome_stop is not None:
            self.biome_stop.set()
        self.phase('stopping', BAD)
        threading.Thread(target=game.release_all, daemon=True).start()

    def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            game.set_roblox_topmost(False)
        except Exception:
            pass
        self.start_again = False
        self.restart_run = False
        self.paused = False
        game.PAUSED.clear()
        self.stop_support_session()
        game.ABORT = True
        if self.biome_stop is not None:
            self.biome_stop.set()
        self.remember()
        guard = getattr(self, '_guard_thread', None)
        if guard is not None and guard.is_alive() and guard is not threading.current_thread():
            self._flush_shown()
            guard.join(timeout=8)
        if guard is None or not guard.is_alive():
            self._commit_lifetime()
        try:
            if self.presence is not None:
                self.presence.close()
        except Exception:
            pass
        self.block_on = False
        try:
            with self.block_lock:
                if self.blocked or self.cache.ours:
                    self._unblock({k for ks in merchant_cache.FILES.values() for k in ks})
        except Exception:
            pass
        try:
            game.disarm()
            game.release_all(focus=False)
        except Exception:
            pass
        os._exit(0)


class Api:
    """JavaScript <-> Python bridge exposed to Edge WebView2 (`window.pywebview.api`)."""

    def __init__(self, engine: MacroEngine):
        self.engine = engine

    def get_state(self):
        return self.engine.build_state()

    def update_cfg(self, patch):
        if isinstance(patch, dict):
            if 'roblox_username' in patch:
                self.engine.apply_roblox_username(patch['roblox_username'])
            with self.engine._lock:
                for k, v in patch.items():
                    if k != 'roblox_username':
                        self.engine.cfg[k] = v
                if 'merchant_on' in patch or 'boss_on' in patch:
                    self.engine._sync_detect()
                self.engine.remember()
            if any(k in patch for k in ('perf_mode', 'low_end', 'fx_on')):
                self.engine.slow_pc = self.engine.wants_lite()
                game.set_pace(2.0 if self.engine.slow_pc else 1.0)
            if self.engine.running:
                self.engine.sync_runtime_cfg(patch)
            self.engine.refresh_discord()
        return self.engine.build_state()

    def verify_passkey(self, key):
        """Unlock Developer Mode with the unified passkey. Session-only: relocks on restart."""
        key = (key or '').strip()
        ok = bool(key) and key == DEVELOPER_PASSKEY
        with self.engine._lock:
            if ok:
                self.engine.developer_locked = False
        return {'ok': ok, 'developer_locked': self.engine.developer_locked}

    def lock_developer(self):
        """Re-lock Developer Mode (session ends / user clicks Lock)."""
        with self.engine._lock:
            self.engine.developer_locked = True
        return {'ok': True, 'developer_locked': self.engine.developer_locked}

    def sync_roblox_profile(self, username):
        self.engine.apply_roblox_username(username, sync=True)
        return self.engine.build_state()

    def resolve_aura(self, typed):
        matched = equip.resolve((typed or '').strip()) if (typed or '').strip() else None
        if matched:
            with self.engine._lock:
                self.engine.cfg['equip_aura'] = matched
                self.engine.remember()
        elif not (typed or '').strip():
            with self.engine._lock:
                self.engine.cfg['equip_aura'] = ''
                self.engine.remember()
        return {'matched': matched}

    def capture_clip_key(self):
        if self.engine._capturing:
            return {'ok': False, 'keys': None}
        self.engine._capturing = True
        try:
            got = clipper.capture_keys()
            if got:
                try:
                    clipper.parse_keys(got)          # validate before saving
                except ValueError as e:
                    note('clip', 'ignored invalid combo %s: %s' % (got, e))
                    return {'ok': False, 'keys': None}
                text = got
                with self.engine._lock:
                    self.engine.cfg['clip_keys'] = text
                    self.engine.remember()
                note('clip', 'clip hotkey set to %s' % text)
                return {'ok': True, 'keys': text}
            return {'ok': False, 'keys': None}
        finally:
            self.engine._capturing = False

    def add_buy_item(self, raw):
        raw = (raw or '').strip()
        if raw:
            name = shop.identify(raw, shop.KNOWN_ITEMS) or raw
            with self.engine._lock:
                buy = dict(self.engine.cfg.get('buy_items') or {})
                if name not in buy:
                    buy[name] = {'on': True, 'qty': ''}
                else:
                    buy[name] = {'on': True, 'qty': str(buy[name].get('qty', ''))}
                self.engine.cfg['buy_items'] = buy
                self.engine.remember()
        return self.engine.build_state()

    def toggle_run(self):
        if self.engine.running:
            self.engine.stop()
            return {'ok': True}
        err = self.engine.start()
        if err:
            return {'ok': False, 'error': err}
        return {'ok': True}

    def pick_avatar_photo(self):
        win = self.engine._window
        if win is None:
            return {'ok': False}
        pic_dir = os.path.join(os.path.expanduser('~'), 'Pictures')
        dl_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        start_dir = pic_dir if os.path.isdir(pic_dir) else (dl_dir if os.path.isdir(dl_dir) else '')
        paths = win.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=start_dir,
            allow_multiple=False,
            file_types=('Image files (*.png;*.jpg;*.jpeg;*.webp;*.bmp)', 'All files (*.*)'),
        )
        if not paths:
            return {'ok': False}
        os.makedirs(game.APP_DIR, exist_ok=True)
        dst = os.path.join(game.APP_DIR, 'avatar_custom.png')
        try:
            Image.open(paths[0]).convert('RGBA').save(dst, 'PNG')
            self.engine._bump_cache_bust()
            return {'ok': True}
        except Exception as e:
            note('ui', 'avatar import failed: %s: %s' % (type(e).__name__, e))
            return {'ok': False}

    def test_webhook(self, url_override=None, server_override=None):
        with self.engine._lock:
            if url_override is not None:
                self.engine.cfg['webhook_url'] = clean_url(url_override)
            if server_override is not None:
                self.engine.cfg['private_server'] = game.tidy_link(server_override)
            if url_override is not None or server_override is not None:
                self.engine.remember()
            url = clean_url(self.engine.cfg_str('webhook_url'))

        if not url.startswith('https://'):
            return {'ok': False, 'error': 'Enter a valid Discord webhook URL first'}
        try:
            embed = {
                'title': 'Webhook Test Successful',
                'description': 'Your Discord webhook is connected and ready to receive alerts.',
                'color': 0xFFFFFF,
                'footer': {'text': '%s | Release v%s' % (TITLE, VERSION)},
            }
            files = {}
            mark = logo_png()
            if mark:
                files['veyra.png'] = mark
                embed['thumbnail'] = {'url': 'attachment://veyra.png'}
            payload = {
                'content': '',
                'allowed_mentions': {'parse': []},
                'embeds': [embed],
            }
            urllib.request.urlopen(_build_webhook_request(url, payload, files), timeout=20).close()
            note('webhook', 'webhook test succeeded (no biome/server link sent)')
            return {'ok': True}
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', errors='replace')[:180]
            except Exception:
                pass
            msg = 'Discord rejected (%s): %s' % (e.code, detail or e.reason)
            note('webhook', msg)
            return {'ok': False, 'error': msg}
        except Exception as e:
            msg = '%s: %s' % (type(e).__name__, e)
            note('webhook', 'webhook test failed: ' + msg)
            return {'ok': False, 'error': msg}

    def set_on_top(self, val):
        hwnd = getattr(self.engine, '_hwnd', None) or _find_own_hwnd()
        if hwnd:
            self.engine._hwnd = hwnd
            ok = _set_hwnd_topmost(hwnd, bool(val))
            return {'ok': ok}
        return {'ok': False}

    def open_calibrator(self, mode='all'):
        try:
            mode_arg = str(mode or 'all')
            if getattr(sys, 'frozen', False):
                cmd = [sys.executable, '--calibrate', mode_arg]
                cwd = os.path.dirname(sys.executable)
            else:
                cmd = [sys.executable, os.path.join(HERE_DIR, 'calibrator_ui.py'), mode_arg]
                cwd = HERE_DIR
            subprocess.Popen(cmd, cwd=cwd)
            return {'ok': True}
        except Exception as e:
            note('ui', 'failed to open calibrator: %s: %s' % (type(e).__name__, e))
            return {'ok': False}

    def open_clips(self):
        os.makedirs(clipper.CLIP_DIR, exist_ok=True)
        os.startfile(clipper.CLIP_DIR)
        return {'ok': True}

    def open_data_folder(self):
        os.makedirs(game.APP_DIR, exist_ok=True)
        os.startfile(game.APP_DIR)
        return {'ok': True}

    def open_discord(self):
        webbrowser.open(INVITE)
        return {'ok': True}

    def open_my_discord(self):
        webbrowser.open('https://discord.com/users/1165362974601510942')
        return {'ok': True}

    def clear_logs(self):
        with _log_lock:
            _recent_logs.clear()
        return {'ok': True}

    def open_dev_console(self):
        try:
            for cand in (r'D:\mmacro-dev-tool\dev_monitor.py', os.path.join(HERE_DIR, 'dev_monitor.py')):
                if os.path.isfile(cand):
                    pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
                    runner = pyw if os.path.isfile(pyw) else sys.executable
                    subprocess.Popen([runner, cand], cwd=os.path.dirname(cand))
                    return {'ok': True}
        except Exception as e:
            note('ui', 'failed to open Dev Console: %s: %s' % (type(e).__name__, e))
        return {'ok': False}

    def preview_biome(self, key='GLITCHED', duration=25.0, send_webhook=False):
        k = (key or 'NORMAL').strip().upper()
        if k == 'NORMAL' or k not in BIOME_COLOURS:
            self.engine._preview_biome = None
            note('biomes', 'FX preview reset to live biome')
        else:
            self.engine._preview_biome = (k, time.monotonic() + max(5.0, float(duration or 25.0)))
            note('biomes', 'FX preview activated: %s' % pretty(k))
            if send_webhook:
                url = clean_url(self.engine.cfg_str('webhook_url'))
                if url.startswith('https://'):
                    srv = self.engine.cfg_str('private_server')
                    post_biome(url, k, ping=True, server=srv, running=0, here=0, sync=False)
        return self.engine.build_state()

    def check_updates_now(self):
        self.engine.check_update(manual=True)
        return self.engine.build_state()

    def start_support(self, relay_url='', allow_input=False):
        return self.engine.start_support_session(relay_url=relay_url, allow_input=allow_input)

    def stop_support(self):
        return self.engine.stop_support_session()

    def set_support_input(self, allow_input):
        with self.engine._support_lock:
            if self.engine._support:
                self.engine._support['allow_input'] = bool(allow_input)
        return self.engine.build_state()

    def reset_all_data(self):
        """Wipe user settings, saved coordinates, custom avatar, and biome history for a clean fresh start."""
        if self.engine.running:
            self.engine.stop()
        with self.engine._lock:
            for fname in ('config.json', 'history.jsonl', 'gui_points.json', 'ai_memory.json', 'lifetime.json', 'avatar_custom.png', 'avatar.png'):
                p = os.path.join(game.APP_DIR, fname)
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            for av in glob.glob(os.path.join(game.APP_DIR, 'avatar_*.png')):
                try:
                    os.remove(av)
                except OSError:
                    pass
            self.engine.cfg = normalize_cfg({})
            self.engine.block_on = False
            self.engine.wanted = {}
            self.engine.pop_list = {}
            self.engine._history.clear()
            self.engine._biome_counts.clear()
            self.engine.rares_count = 0
            self.engine._life = {'fish': 0, 'points': 0, 'merchants': 0, 'bosses': 0}
            self.engine._life_done_epoch = self.engine._life_epoch
            self.engine._points_live = None
            self.engine._points_suspect = None
            self.engine._stats_fish = self.engine._stats_merchants = self.engine._stats_bosses = self.engine._stats_points = 0
            if self.engine.fisher is not None:
                self.engine.fisher.caught = 0
                self.engine.fisher.points_earned = 0
            self.engine.biome_now = 'NORMAL'
            auto_name, auto_uid = detect_roblox_profile()
            self.engine.player_name = auto_name
            self.engine.roblox_uid = auto_uid
            _QuietHandler.avatar_uid = auto_uid
            self.engine.cache_bust += 1
            self.engine.remember()
        note('config', 'all settings and history reset to factory defaults')
        return self.engine.build_state()


# ================================================================ Single Instance, Auto-Updater & Entrypoint
SWAP_BAT = r"""@echo off
set sys=%SystemRoot%\System32
set n=0
:wait
"%sys%\tasklist.exe" /FI "PID eq %MMACRO_PID%" 2>nul | "%sys%\find.exe" "%MMACRO_PID%" >nul || goto swap
set /a n+=1
if %n% geq 30 goto swap
"%sys%\PING.EXE" -n 2 127.0.0.1 >nul
goto wait
:swap
set n=0
:again
if "%MMACRO_ZIP%"=="1" (
  "%sys%\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Expand-Archive -Force -LiteralPath $env:MMACRO_NEW -DestinationPath $env:MMACRO_DIR" >nul 2>&1
) else (
  move /y "%MMACRO_NEW%" "%MMACRO_EXE%" >nul 2>&1
)
if not errorlevel 1 goto start
set /a n+=1
if %n% geq 30 goto start
"%sys%\PING.EXE" -n 2 127.0.0.1 >nul
goto again
:start
if "%MMACRO_ZIP%"=="1" del "%MMACRO_NEW%"
start "" "%MMACRO_EXE%"
del "%~f0"
"""

SWAP_SRC_BAT = r"""@echo off
set sys=%SystemRoot%\System32
set n=0
:wait
"%sys%\tasklist.exe" /FI "PID eq %MMACRO_PID%" 2>nul | "%sys%\find.exe" "%MMACRO_PID%" >nul || goto copyfiles
set /a n+=1
if %n% geq 30 goto copyfiles
"%sys%\PING.EXE" -n 2 127.0.0.1 >nul
goto wait
:copyfiles
xcopy "%MMACRO_SRC%\*" "%MMACRO_DIR%\" /E /Y /I /Q >nul 2>&1
rmdir /S /Q "%MMACRO_TMP%" >nul 2>&1
start "" /D "%MMACRO_DIR%" "%MMACRO_PY%" "%MMACRO_APP%"
del "%~f0"
"""


def _version_tuple(text):
    nums = [int(n) for n in re.findall(r'\d+', text or '')[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _folder_build():
    return getattr(sys, 'frozen', False) and os.path.isdir(os.path.join(os.path.dirname(sys.executable), '_internal'))


def _can_write_dir(folder):
    probe = os.path.join(folder, '.mmacro_write_test')
    try:
        with open(probe, 'wb') as f:
            f.write(b'0')
        os.remove(probe)
        return True
    except OSError:
        return False


def _release_asset_time(asset):
    """Epoch seconds from a GitHub asset's updated_at, or 0 when unavailable."""
    stamp = (asset or {}).get('updated_at') or (asset or {}).get('created_at') or ''
    try:
        return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0


def _release_source_zip(tag):
    """GitHub source archive for a release tag. The main branch lags published releases."""
    tag = (tag or '').strip()
    if tag and not tag.startswith('v') and tag[:1].isdigit():
        tag = 'v' + tag
    if not tag or any(c in tag for c in ' /\\?#%'):
        return SOURCE_ZIP_URL
    return 'https://github.com/%s/archive/refs/tags/%s.zip' % (REPO_SLUG, tag)


def _preserve_source_updater(src_root):
    """v1.0.3 and v1.0.4 still read VERSION from main. Patch that copy before it replaces this folder."""
    path = os.path.join(src_root, 'app.py')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read().replace('\r\n', '\n')
    except OSError:
        return
    if 'zip_url = _release_source_zip(' in text and 'def _release_source_zip(' in text:
        return
    old_check = '\n'.join([
        '                        if frozen:',
        '                            found = _fetch_latest_release()',
        '                            remote_ver = found[0] if found else VERSION',
        '                        else:',
        '                            remote_ver = _fetch_remote_source_version()',
        '                            if not remote_ver:',
        '                                rel = _fetch_latest_release()',
        '                                remote_ver = rel[0] if rel else VERSION',
    ])
    new_check = '\n'.join([
        '                        found = _fetch_latest_release()',
        '                        if found:',
        '                            remote_ver = found[0]',
        '                        elif frozen:',
        '                            remote_ver = VERSION',
        '                        else:',
        '                            remote_ver = _fetch_remote_source_version() or VERSION',
    ])
    old_dl = '\n'.join([
        "                    note('update', 'downloading source update %s' % SOURCE_ZIP_URL)",
        '                    _download_file(',
        '                        SOURCE_ZIP_URL, zip_path,',
    ])
    new_dl = '\n'.join([
        '                    zip_url = _release_source_zip(found[0] if found else remote_ver)',
        "                    note('update', 'downloading source update %s' % zip_url)",
        '                    _download_file(',
        '                        zip_url, zip_path,',
    ])
    slash = '\\'
    guard = "    if not tag or any(c in tag for c in ' /" + slash + slash + "?#%'):"
    helper = '\n'.join([
        'def _release_source_zip(tag):',
        '    """GitHub source archive for a release tag. The main branch lags published releases."""',
        "    tag = (tag or '').strip()",
        "    if tag and not tag.startswith('v') and tag[:1].isdigit():",
        "        tag = 'v' + tag",
        guard,
        '        return SOURCE_ZIP_URL',
        "    return 'https://github.com/%s/archive/refs/tags/%s.zip' % (REPO_SLUG, tag)",
        '',
        '',
    ])
    changed = False
    if old_check in text:
        text = text.replace(old_check, new_check, 1)
        changed = True
    old_refresh = 'if found and not newer_ver and found[3]:'
    new_refresh = 'if frozen and found and not newer_ver and found[3]:'
    if old_refresh in text and new_refresh not in text:
        text = text.replace(old_refresh, new_refresh, 1)
        changed = True
    if old_dl in text:
        text = text.replace(old_dl, new_dl, 1)
        changed = True
    if 'def _release_source_zip(' not in text and 'def _fetch_latest_release():' in text:
        text = text.replace('def _fetch_latest_release():', helper + 'def _fetch_latest_release():', 1)
        changed = True
    if not changed:
        return
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
    except OSError as e:
        note('update', 'could not keep updater fix in download: %s' % e)
        return
    note('update', 'updater in the download now follows release tags')


def _fetch_latest_release():
    req = urllib.request.Request(RELEASES, headers={'User-Agent': TITLE, 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.load(r)
    releases = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    ext = '.zip' if _folder_build() else '.exe'
    candidates = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get('draft'):
            continue
        assets = rel.get('assets', []) or []
        asset = next((a for a in assets if a.get('name', '').lower().endswith(ext)), None)
        if asset is None and ext == '.exe':
            asset = next((a for a in assets if 'mmacro' in a.get('name', '').lower() and a.get('name', '').lower().endswith('.exe')), None)
        if asset:
            tag = rel.get('tag_name', '')
            candidates.append((_version_tuple(tag), tag, asset['browser_download_url'],
                               int(asset.get('size') or 0), _release_asset_time(asset)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[4]), reverse=True)
    _, tag, url, size, asset_time = candidates[0]
    return (tag, url, size, asset_time)


def _fetch_remote_source_version():
    req = urllib.request.Request(SOURCE_VERSION_URL, headers={'User-Agent': TITLE})
    with urllib.request.urlopen(req, timeout=12) as r:
        head = r.read(24000).decode('utf-8', errors='replace')
    m = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]", head, re.M)
    return m.group(1) if m else None


def _download_file(url, dest, expected_size=0, progress_cb=None, min_bytes=0):
    got = 0
    req = urllib.request.Request(url, headers={'User-Agent': TITLE})
    try:
        with urllib.request.urlopen(req, timeout=45) as r, open(dest, 'wb') as f:
            total = expected_size or int(r.headers.get('Content-Length') or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and progress_cb:
                    progress_cb(min(1.0, got / total))
        if expected_size and got != expected_size:
            raise IOError('download incomplete (%d of %d bytes)' % (got, expected_size))
        if min_bytes and got < min_bytes:
            raise IOError('download too small (%d bytes)' % got)
    except Exception:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        raise
    try:
        zi = dest + ':Zone.Identifier'
        if os.path.exists(zi):
            os.remove(zi)
    except OSError:
        pass


def _install_exe_update(new_path, exe_path, pid, zipped):
    bat = os.path.join(tempfile.gettempdir(), 'mmacro_update_%d.bat' % pid)
    with open(bat, 'w', encoding='ascii') as f:
        f.write(SWAP_BAT)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('_PYI_', '_MEIPASS'))}
    env.update(
        PYINSTALLER_RESET_ENVIRONMENT='1',
        MMACRO_NEW=new_path,
        MMACRO_EXE=exe_path,
        MMACRO_DIR=os.path.dirname(exe_path),
        MMACRO_PID=str(pid),
        MMACRO_ZIP='1' if zipped else '0',
    )
    subprocess.Popen(['cmd', '/c', bat], env=env, creationflags=0x08000000 | 0x00000200, close_fds=True)


def _install_source_update(src_root, dest_dir, tmp_dir, pid):
    bat = os.path.join(tempfile.gettempdir(), 'mmacro_src_update_%d.bat' % pid)
    with open(bat, 'w', encoding='ascii') as f:
        f.write(SWAP_SRC_BAT)
    env = dict(os.environ)
    env.update(
        MMACRO_SRC=src_root,
        MMACRO_DIR=dest_dir,
        MMACRO_TMP=tmp_dir,
        MMACRO_PY=sys.executable,
        MMACRO_APP=os.path.join(dest_dir, 'app.py'),
        MMACRO_PID=str(pid),
    )
    subprocess.Popen(['cmd', '/c', bat], env=env, creationflags=0x08000000 | 0x00000200, close_fds=True)


def _attach_auto_updater():
    """Attach update checker & auto-installer method cleanly onto MacroEngine."""

    def _set_upd(self, state, msg, progress=0, latest=''):
        self.update_info = {
            'state': state,
            'msg': msg,
            'progress': int(progress),
            'latest': latest or VERSION,
        }

    def check_update(self, manual=False):
        if os.environ.get('MMACRO_SKIP_AUTO_UPDATE') == '1':
            return
        if getattr(self, '_checking_update', False) or self.updating:
            return
        self._checking_update = True
        self.update_info = {'state': 'checking', 'msg': 'Checking for updates...', 'progress': 0, 'latest': VERSION}

        def worker():
            try:
                tries = 1 if manual else 3
                frozen = getattr(sys, 'frozen', False)
                found = None
                remote_ver = None
                for attempt in range(tries):
                    try:
                        found = _fetch_latest_release()
                        if found:
                            remote_ver = found[0]
                        elif frozen:
                            remote_ver = VERSION
                        else:
                            remote_ver = _fetch_remote_source_version() or VERSION
                        break
                    except Exception as e:
                        note('update', 'check failed (try %d of %d): %s: %s' % (attempt + 1, tries, type(e).__name__, e))
                        if attempt + 1 < tries:
                            time.sleep(10)
                        else:
                            _set_upd(self, 'error', 'Update check failed (%s)' % type(e).__name__)
                            return

                newer_ver = bool(remote_ver) and _version_tuple(remote_ver) > _version_tuple(VERSION)
                refreshed_build = False
                if frozen and found and not newer_ver and found[3]:
                    # A release asset can be replaced without changing its tag.
                    # Pick that up by comparing the asset time with the running file.
                    try:
                        local_file = sys.executable if frozen else os.path.join(HERE_DIR, 'app.py')
                        local_mtime = os.path.getmtime(local_file)
                    except OSError:
                        local_mtime = 0
                    refreshed_build = found[3] > local_mtime + 60

                if not remote_ver or (not newer_ver and not refreshed_build):
                    note('update', 'up to date (v%s)' % VERSION)
                    _set_upd(self, 'uptodate', 'Up to date (v%s)' % VERSION, 100, remote_ver or VERSION)
                    return

                clean_tag = remote_ver.lstrip('v')
                if newer_ver:
                    note('update', 'new update v%s found (current v%s)' % (clean_tag, VERSION))
                else:
                    note('update', 'newer build for v%s found' % clean_tag)
                if not manual and (not self.cfg_bool('auto_update', True) or self.running or self.ran):
                    _set_upd(self, 'available', 'Update v%s ready' % clean_tag, 0, clean_tag)
                    return

                target_dir = os.path.dirname(sys.executable) if frozen else HERE_DIR
                if not _can_write_dir(target_dir):
                    note('update', 'folder is read-only (%s): open %s' % (target_dir, RELEASE_PAGE))
                    _set_upd(self, 'readonly', 'v%s ready — folder locked' % clean_tag, 0, clean_tag)
                    return

                self.updating = True
                if frozen:
                    tag, url, size, _asset_ts = found
                    zipped = url.lower().endswith('.zip')
                    new_path = os.path.join(tempfile.gettempdir(), 'MMacro_update.zip') if zipped else sys.executable + '.new'
                    _set_upd(self, 'downloading', 'Updating to v%s (0%%)...' % clean_tag, 0, clean_tag)
                    self.phase('updating to v%s...' % clean_tag, ACCENT)
                    note('update', 'downloading %s' % url)
                    _download_file(
                        url, new_path, expected_size=size,
                        progress_cb=lambda f: _set_upd(self, 'downloading', 'Updating to v%s (%d%%)...' % (clean_tag, round(f * 100)), round(f * 100), clean_tag),
                        min_bytes=100_000,
                    )
                    if self.running or self.ran:
                        self.updating = False
                        _set_upd(self, 'available', 'v%s downloaded — restart to apply' % clean_tag, 100, clean_tag)
                        return
                    _set_upd(self, 'restarting', 'Installing v%s & restarting...' % clean_tag, 100, clean_tag)
                    note('update', 'installing v%s and restarting...' % clean_tag)
                    _install_exe_update(new_path, sys.executable, os.getpid(), zipped)
                    self.close()
                else:
                    import zipfile
                    tmp_dir = tempfile.mkdtemp(prefix='mmacro_src_upd_')
                    zip_path = os.path.join(tmp_dir, 'update.zip')
                    _set_upd(self, 'downloading', 'Updating source to v%s...' % clean_tag, 10, clean_tag)
                    self.phase('updating to v%s...' % clean_tag, ACCENT)
                    zip_url = _release_source_zip(found[0] if found else remote_ver)
                    note('update', 'downloading source update %s' % zip_url)
                    _download_file(
                        zip_url, zip_path,
                        progress_cb=lambda f: _set_upd(self, 'downloading', 'Updating to v%s (%d%%)...' % (clean_tag, round(f * 100)), round(f * 100), clean_tag),
                        min_bytes=10_000,
                    )
                    extract_dir = os.path.join(tmp_dir, 'ext')
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        zf.extractall(extract_dir)
                    roots = [os.path.join(extract_dir, d) for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
                    src_root = roots[0] if roots and os.path.isfile(os.path.join(roots[0], 'app.py')) else extract_dir
                    if not os.path.isfile(os.path.join(src_root, 'app.py')):
                        raise RuntimeError('Downloaded archive missing app.py')
                    _preserve_source_updater(src_root)
                    if self.running or self.ran:
                        self.updating = False
                        _set_upd(self, 'available', 'v%s ready — restart to apply' % clean_tag, 100, clean_tag)
                        return
                    _set_upd(self, 'restarting', 'Installing v%s & restarting...' % clean_tag, 100, clean_tag)
                    note('update', 'applying source update v%s and restarting...' % clean_tag)
                    _install_source_update(src_root, HERE_DIR, tmp_dir, os.getpid())
                    self.close()
            except Exception as e:
                note('update', 'auto-update failed: %s: %s' % (type(e).__name__, e))
                self.updating = False
                _set_upd(self, 'error', 'Update failed (%s)' % type(e).__name__)
            finally:
                self._checking_update = False

        threading.Thread(target=worker, daemon=True).start()

    MacroEngine.check_update = check_update


_attach_auto_updater()


def _attach_support_host():
    """Attach consent-gated live Support Code session (logs, screen & macro controls) onto MacroEngine."""

    def support_status(self):
        with self._support_lock:
            s = self._support
            if not s or not s.get('active'):
                return {
                    'active': False, 'code': '', 'local_url': '', 'lan_url': '',
                    'relay_url': os.environ.get('MMACRO_RELAY_URL', ''),
                    'allow_input': False, 'connected_dev': None, 'expires_in': 0,
                }
            exp_in = max(0, int(support_link.SESSION_TTL_S - (time.time() - s['started_at'])))
            return {
                'active': True,
                'code': s['code'],
                'local_url': s['local_url'],
                'lan_url': s['lan_url'],
                'relay_url': s['relay_url'],
                'allow_input': bool(s['allow_input']),
                'connected_dev': s.get('connected_dev'),
                'expires_in': exp_in,
            }

    def _macro_window_bbox(self):
        try:
            hwnd = getattr(self, '_hwnd', None) or _find_own_hwnd()
            if not hwnd:
                return None
            rect = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return None
            # keep on-screen-ish bounds
            sw = ctypes.windll.user32.GetSystemMetrics(78) or ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(79) or ctypes.windll.user32.GetSystemMetrics(1)
            l = max(-50, int(rect.left)); t = max(-50, int(rect.top))
            r = min(int(sw) + 50, int(rect.right)); b = min(int(sh) + 50, int(rect.bottom))
            if r - l < 80 or b - t < 80:
                return None
            return (l, t, r, b)
        except Exception:
            return None

    def _support_allowed_roots(self):
        roots = []
        try:
            roots.append(os.path.realpath(HERE_DIR))
        except Exception:
            pass
        try:
            roots.append(os.path.realpath(game.APP_DIR))
        except Exception:
            pass
        # dedupe while preserving order
        out = []
        seen = set()
        for r in roots:
            k = os.path.normcase(r)
            if r and k not in seen:
                seen.add(k)
                out.append(r)
        return out or [os.path.realpath('.')]

    def _support_resolve_path(self, raw, for_write=False):
        roots = self._support_allowed_roots()
        if not roots:
            return None, 'No allowed roots'
        base = roots[0]
        raw = (raw or '').strip()
        # allow passing an absolute path only if it falls inside an allowed root
        if raw and os.path.isabs(raw):
            target = os.path.realpath(raw)
        else:
            # treat empty/relative as relative to macro project root (HERE_DIR)
            target = os.path.realpath(os.path.join(base, raw or '.'))

        def _inside(path, root):
            try:
                return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
            except Exception:
                return False

        if not any(_inside(target, r) for r in roots):
            return None, 'Access denied (outside allowed macro folders)'
        # block symlink/junction escapes where possible
        try:
            cur = target
            stop = None
            for r in roots:
                if _inside(cur, r):
                    stop = os.path.normcase(r)
                    break
            probe = cur
            while probe:
                pk = os.path.normcase(probe)
                if stop and pk == stop:
                    break
                if os.path.islink(probe):
                    realp = os.path.realpath(probe)
                    if not any(_inside(realp, r) for r in roots):
                        return None, 'Access denied (symlink outside allowed folders)'
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
        except Exception:
            pass
        if for_write:
            # require parent directory to be inside roots (creation of new files allowed)
            parent = os.path.dirname(target) or target
            if not any(_inside(os.path.realpath(parent), r) for r in roots):
                return None, 'Access denied (outside allowed macro folders)'
        return target, None

    def _safe_snapshot(self, sess):
        with self._lock:
            cfg_safe = json.loads(json.dumps(self.cfg))
            if cfg_safe.get('webhook_url'):
                cfg_safe['webhook_url'] = 'https://discord.com/api/webhooks/[REDACTED]'
            if cfg_safe.get('private_server'):
                cfg_safe['private_server'] = _sanitize_log_text(cfg_safe['private_server'])
            snap_meta = {
                'version': VERSION,
                'player_name': self.player_name,
                'running': self.running,
                'phase': self.phase_text,
                'biome_now': self.biome_now,
                'roblox_up': game.find_roblox_window() is not None,
                'allow_input': bool(sess.get('allow_input')),
                'cfg': cfg_safe,
            }
        with _log_lock:
            snap_meta['logs'] = [{'ts': ts, 'tag': tag, 'msg': _sanitize_log_text(msg)} for ts, tag, msg in _recent_logs[-120:]]
        snap_meta['ai_alerts'] = []       # AI Log Doctor removed; field kept for dev-tool compatibility
        # Remote view/control is limited to the Roblox client or the Veyra window (no unrelated desktop capture).
        rb_box = None
        try:
            if game.roblox_window() is not None:
                b = game.client_box((0.0, 0.0, 1.0, 1.0))
                if b and b[2] > b[0] and b[3] > b[1]:
                    rb_box = b
        except Exception:
            rb_box = None
        macro_box = None if rb_box else _macro_window_bbox(self)
        target_box = rb_box or macro_box
        scope = 'roblox' if rb_box else ('macro' if macro_box else 'none')
        dev_on = bool(sess.get('connected_dev'))
        snap_meta['screen_b64'] = support_link.capture_screen_b64(
            max_w=800 if dev_on else 640,
            quality=42 if dev_on else 32,
            bbox=target_box,
        ) if target_box else ''
        snap_meta['screen_scope'] = scope
        snap_meta['control_target'] = scope
        snap_meta['ts'] = time.strftime('%H:%M:%S')
        return snap_meta

    def _exec_support_cmd(self, sess, item):
        cmd = item.get('cmd', '')
        args = item.get('args') or {}
        dev = item.get('dev', 'dev')
        if cmd not in support_link.ALLOWED_CMDS:
            return {'ok': False, 'error': 'Command not allowed'}
        note('support', 'dev %s executed %s' % (dev, cmd))
        try:
            if cmd == 'start_macro':
                self.start()
                return {'ok': True}
            elif cmd == 'stop_macro':
                self.stop()
                return {'ok': True}
            elif cmd == 'heal_screen':
                self.ai_heal_screen('dev:%s' % dev)
                return {'ok': True}
            elif cmd == 'check_chat':
                self.check_chat_on_start()
                return {'ok': True}
            elif cmd == 'preview_biome':
                k = str(args.get('key') or 'NORMAL').upper()
                self._preview_biome = None if (k == 'NORMAL' or k not in BIOME_COLOURS) else (k, time.monotonic() + 25.0)
                return {'ok': True}
            elif cmd == 'update_cfg':
                note('support', 'dev %s attempted disabled command update_cfg' % dev)
                return {'ok': False, 'error': 'update_cfg disabled'}
            elif cmd == 'roblox_click':
                if not sess.get('allow_input'):
                    return {'ok': False, 'error': 'Remote click/key not enabled by user'}
                # Prefer Roblox client when available; fall back to Veyra window for macro-only control.
                if game.roblox_window() is not None:
                    box = game.client_box((0.0, 0.0, 1.0, 1.0))
                    if box and box[2] > box[0] and box[3] > box[1]:
                        nx = max(0.0, min(1.0, float(args.get('nx', 0.5))))
                        ny = max(0.0, min(1.0, float(args.get('ny', 0.5))))
                        px = int(box[0] + nx * (box[2] - box[0]))
                        py = int(box[1] + ny * (box[3] - box[1]))
                        game.take_foreground(tries=1)
                        game.click(px, py)
                        return {'ok': True, 'target': 'roblox'}
                box = _macro_window_bbox(self)
                if box and box[2] > box[0] and box[3] > box[1]:
                    nx = max(0.0, min(1.0, float(args.get('nx', 0.5))))
                    ny = max(0.0, min(1.0, float(args.get('ny', 0.5))))
                    px = int(box[0] + nx * (box[2] - box[0]))
                    py = int(box[1] + ny * (box[3] - box[1]))
                    hwnd = getattr(self, '_hwnd', None) or _find_own_hwnd()
                    try:
                        if hwnd:
                            game.force_foreground(hwnd)
                            time.sleep(0.08)
                    except Exception:
                        pass
                    try:
                        game.move_to(px, py, 0.02)
                        game.button('left', True); time.sleep(0.05); game.button('left', False)
                    except Exception:
                        game._mouse(game.BTN_DOWN['left'])
                        time.sleep(0.05)
                        game._mouse(game.BTN_UP['left'])
                    return {'ok': True, 'target': 'macro'}
                return {'ok': False, 'error': 'No Roblox or Veyra window found'}
            elif cmd == 'roblox_key':
                if not sess.get('allow_input'):
                    return {'ok': False, 'error': 'Remote click/key not enabled by user'}
                key = str(args.get('key') or '').strip()[:24]
                if not key:
                    return {'ok': False, 'error': 'No key provided'}
                if game.roblox_window() is not None:
                    game.take_foreground(tries=1)
                    game.tap(key)
                    return {'ok': True, 'target': 'roblox'}
                hwnd = getattr(self, '_hwnd', None) or _find_own_hwnd()
                if hwnd:
                    try:
                        game.force_foreground(hwnd)
                        time.sleep(0.08)
                    except Exception:
                        pass
                    # Map simple key names to pynput Key when possible, else send as char(s)
                    try:
                        from pynput.keyboard import Key
                        kmap = {
                            'esc': Key.esc, 'escape': Key.esc, 'enter': Key.enter, 'return': Key.enter,
                            'tab': Key.tab, 'space': Key.space, 'backspace': Key.backspace,
                            'up': Key.up, 'down': Key.down, 'left': Key.left, 'right': Key.right,
                            'home': Key.home, 'end': Key.end, 'pageup': Key.page_up, 'pagedown': Key.page_down,
                            'delete': Key.delete, 'f1': Key.f1, 'f2': Key.f2, 'f3': Key.f3, 'f4': Key.f4,
                            'f5': Key.f5, 'f6': Key.f6, 'f7': Key.f7, 'f8': Key.f8, 'f9': Key.f9,
                            'f10': Key.f10, 'f11': Key.f11, 'f12': Key.f12,
                        }
                        k = kmap.get(key.lower(), key if len(key) == 1 else getattr(Key, key.lower(), key))
                        with game.INPUT_LOCK:
                            game.kb.press(k); time.sleep(0.03); game.kb.release(k)
                        return {'ok': True, 'target': 'macro'}
                    except Exception as ex:
                        return {'ok': False, 'error': 'Failed to send key: %s' % ex}
                return {'ok': False, 'error': 'No Roblox or Veyra window found'}

            # --- Remote macro file operations (sandboxed to macro dir + app data) ---
            elif cmd == 'list_dir':
                p, err = self._support_resolve_path(args.get('path', '.'))
                if err:
                    return {'ok': False, 'error': err}
                if not os.path.isdir(p):
                    return {'ok': False, 'error': 'Not a directory'}
                root0 = self._support_allowed_roots()[0]
                items = []
                try:
                    with os.scandir(p) as it:
                        for e in it:
                            try:
                                st = e.stat(follow_symlinks=False)
                                items.append({
                                    'name': e.name,
                                    'type': 'dir' if e.is_dir(follow_symlinks=False) else 'file',
                                    'size': st.st_size,
                                    'mtime': int(st.st_mtime),
                                })
                            except Exception:
                                pass
                except Exception as ex:
                    return {'ok': False, 'error': str(ex)}
                items.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
                rel = os.path.relpath(p, root0)
                if rel == '.':
                    rel = ''
                return {'ok': True, 'path': p, 'rel': rel, 'items': items, 'roots': self._support_allowed_roots()}
            elif cmd == 'read_file':
                p, err = self._support_resolve_path(args.get('path', ''))
                if err:
                    return {'ok': False, 'error': err}
                if not os.path.isfile(p):
                    return {'ok': False, 'error': 'Not a file'}
                max_bytes = int(args.get('max_bytes') or 300000)
                max_bytes = max(1024, min(max_bytes, 1000000))
                try:
                    with open(p, 'rb') as f:
                        raw = f.read(max_bytes + 1)
                except Exception as ex:
                    return {'ok': False, 'error': str(ex)}
                truncated = len(raw) > max_bytes
                if truncated:
                    raw = raw[:max_bytes]
                is_text = b'\x00' not in raw[:4096]
                if is_text:
                    content = raw.decode('utf-8', errors='replace')
                    enc = 'utf-8'
                else:
                    content = base64.b64encode(raw).decode('ascii')
                    enc = 'base64'
                try:
                    st = os.stat(p)
                    size = st.st_size
                    mtime = int(st.st_mtime)
                except Exception:
                    size = len(raw)
                    mtime = 0
                return {'ok': True, 'path': p, 'content': content, 'encoding': enc, 'truncated': truncated, 'size': size, 'mtime': mtime}
            elif cmd == 'write_file':
                p, err = self._support_resolve_path(args.get('path', ''), for_write=True)
                if err:
                    return {'ok': False, 'error': err}
                content = args.get('content')
                if content is None:
                    return {'ok': False, 'error': 'No content'}
                encoding = (args.get('encoding') or 'utf-8').lower()
                try:
                    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
                    if encoding == 'base64':
                        data = base64.b64decode(content)
                    else:
                        data = str(content).replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
                    with open(p, 'wb') as f:
                        f.write(data)
                except Exception as ex:
                    return {'ok': False, 'error': str(ex)}
                return {'ok': True, 'path': p, 'bytes': len(data)}
            elif cmd == 'make_dir':
                p, err = self._support_resolve_path(args.get('path', ''), for_write=True)
                if err:
                    return {'ok': False, 'error': err}
                try:
                    os.makedirs(p, exist_ok=True)
                except Exception as ex:
                    return {'ok': False, 'error': str(ex)}
                return {'ok': True, 'path': p}
            elif cmd == 'delete_path':
                p, err = self._support_resolve_path(args.get('path', ''), for_write=True)
                if err:
                    return {'ok': False, 'error': err}
                roots = self._support_allowed_roots()
                if any(os.path.normcase(p) == os.path.normcase(r) for r in roots):
                    return {'ok': False, 'error': 'Cannot delete a root folder'}
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    elif os.path.exists(p):
                        os.remove(p)
                    else:
                        return {'ok': False, 'error': 'Not found'}
                except Exception as ex:
                    return {'ok': False, 'error': str(ex)}
                return {'ok': True, 'path': p}

            return {'ok': True}
        except Exception as e:
            note('support', 'command %s error: %s: %s' % (cmd, type(e).__name__, e))
            return {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}

    def start_support_session(self, relay_url='', allow_input=False):
        self.stop_support_session()
        code = support_link.generate_support_code()
        host_secret = secrets.token_hex(12)
        store = support_link.RelayStore()
        stop_evt = threading.Event()
        relay = (relay_url or os.environ.get('MMACRO_RELAY_URL') or '').strip().rstrip('/')

        # The room code is a bearer secret; throttle it so a LAN caller cannot brute-force it.
        attempts = {}

        def rate_ok(key, limit=12, window=30.0):
            now = time.monotonic()
            recent = [t for t in attempts.get(key, ()) if now - t < window]
            attempts[key] = recent
            if len(recent) >= limit:
                return False
            recent.append(now)
            return True

        class _LocalSupportHandler(http.server.BaseHTTPRequestHandler):
            def _send(self_h, status, data):
                body = json.dumps(data).encode('utf-8')
                self_h.send_response(status)
                self_h.send_header('Content-Type', 'application/json')
                self_h.send_header('Content-Length', str(len(body)))
                self_h.end_headers()
                self_h.wfile.write(body)

            def _read_json(self_h):
                n = int(self_h.headers.get('Content-Length') or 0)
                if n <= 0 or n > 2_000_000:
                    return {}
                raw = self_h.rfile.read(n).decode('utf-8', errors='replace')
                return json.loads(raw) if raw else {}

            def do_GET(self_h):
                path = self_h.path.split('?', 1)[0]
                m = re.match(r'^/room/([A-Za-z0-9-]+)/pull$', path)
                if m:
                    if not rate_ok('pull:%s' % self_h.client_address[0], limit=120):
                        return self_h._send(429, {'ok': False, 'error': 'Too many requests'})
                    res = store.pull_dev(m.group(1))
                    return self_h._send(200 if res.get('ok') else 404, res)
                m = re.match(r'^/room/([A-Za-z0-9-]+)/result/([A-Za-z0-9]+)$', path)
                if m:
                    if not rate_ok('res:%s' % self_h.client_address[0], limit=240):
                        return self_h._send(429, {'ok': False, 'error': 'Too many requests'})
                    res = store.get_result(m.group(1), m.group(2))
                    return self_h._send(200 if res.get('ok') or res.get('pending') else 404, res)
                self_h._send(404, {'ok': False})

            def do_POST(self_h):
                path = self_h.path.split('?', 1)[0]
                m = re.match(r'^/room/([A-Za-z0-9-]+)/(connect|cmd|close)$', path)
                if not m:
                    return self_h._send(404, {'ok': False})
                if self_h.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    return self_h._send(415, {'ok': False, 'error': 'JSON required'})
                if not rate_ok('%s:%s' % (self_h.client_address[0], m.group(2))):
                    return self_h._send(429, {'ok': False, 'error': 'Too many attempts'})
                data = self_h._read_json()
                rc, act = m.group(1), m.group(2)
                if act == 'connect':
                    res = store.connect_dev(rc, data.get('dev_username', 'dev'))
                    return self_h._send(200 if res.get('ok') else 404, res)
                if act == 'cmd':
                    res = store.enqueue_cmd(rc, data.get('dev_username', 'dev'), data.get('cmd', ''), data.get('args', {}))
                    return self_h._send(200 if res.get('ok') else 400, res)
                return self_h._send(200, store.close_room(rc))

            def log_message(self_h, *args):
                pass

        class _LocalSrv(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        # Loopback by default; opt in to LAN with MMACRO_SUPPORT_LAN=1.
        lan_ok = os.environ.get('MMACRO_SUPPORT_LAN') == '1'
        srv = _LocalSrv(('0.0.0.0' if lan_ok else '127.0.0.1', 0), _LocalSupportHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ip = support_link.local_ip()
        local_url = 'http://127.0.0.1:%d' % port
        lan_url = 'http://%s:%d' % (ip, port) if lan_ok else ''

        sess = {
            'active': True,
            'code': code,
            'host_secret': host_secret,
            'relay_url': relay,
            'local_url': local_url,
            'lan_url': lan_url,
            'allow_input': bool(allow_input),
            'connected_dev': None,
            'started_at': time.time(),
            'srv': srv,
            'store': store,
            'stop_evt': stop_evt,
        }
        with self._support_lock:
            self._support = sess

        support_link.register_local_session(code, {
            'code': code, 'local_url': local_url, 'lan_url': lan_url, 'relay_url': relay, 'updated_at': time.time(),
        })
        note('support', 'Support session started · code=%s (%s)' % (code, lan_url))

        def _loop():
            last_dev = None
            while not stop_evt.is_set() and not self._closing:
                if time.time() - sess['started_at'] >= support_link.SESSION_TTL_S:
                    note('support', 'Support code %s expired after 30 min' % code)
                    break
                try:
                    snap = _safe_snapshot(self, sess)
                    local_res = store.open_or_push(code, host_secret, snap)
                    cmds = list(local_res.get('commands') or [])
                    dev = local_res.get('connected_dev')
                    if local_res.get('closed'):
                        break
                    if relay:
                        try:
                            r_res = support_link.http_json(
                                '%s/room/%s/push' % (relay, code),
                                method='POST',
                                payload={'host_secret': host_secret, 'snapshot': snap},
                                timeout=6,
                            )
                            cmds.extend(r_res.get('commands') or [])
                            dev = r_res.get('connected_dev') or dev
                            if r_res.get('closed'):
                                break
                        except Exception:
                            pass
                    if dev != last_dev:
                        last_dev = dev
                        sess['connected_dev'] = dev
                        if dev:
                            note('support', "Developer '%s' connected to your Support Session (%s)" % (dev, code))
                            self.notice("Dev '%s' connected (Support %s)" % (dev, code), ACCENT, 6000)
                    results_out = {}
                    for item in cmds:
                        try:
                            r = _exec_support_cmd(self, sess, item)
                        except Exception as ex:
                            r = {'ok': False, 'error': str(ex)}
                        cid = item.get('id')
                        if cid:
                            results_out[cid] = r if r is not None else {'ok': True}
                    if results_out:
                        try:
                            store.open_or_push(code, host_secret, {}, results_out)
                        except Exception:
                            pass
                        if relay:
                            try:
                                support_link.http_json(
                                    '%s/room/%s/push' % (relay, code),
                                    method='POST',
                                    payload={'host_secret': host_secret, 'snapshot': {}, 'results': results_out},
                                    timeout=6,
                                )
                            except Exception:
                                pass
                    support_link.register_local_session(code, {
                        'code': code, 'local_url': local_url, 'lan_url': lan_url, 'relay_url': relay, 'updated_at': time.time(),
                    })
                except Exception:
                    pass
                # Faster stream when a dev is connected; slower idle polling to reduce load.
                stop_evt.wait(0.35 if sess.get('connected_dev') else 1.0)
            self.stop_support_session()

        threading.Thread(target=_loop, daemon=True).start()
        return self.build_state()

    def stop_support_session(self):
        with self._support_lock:
            sess = self._support
            self._support = None
        if not sess:
            return self.build_state()
        try:
            sess['stop_evt'].set()
        except Exception:
            pass
        support_link.unregister_local_session(sess['code'])
        try:
            sess['store'].close_room(sess['code'])
        except Exception:
            pass
        if sess.get('relay_url'):
            try:
                support_link.http_json('%s/room/%s/close' % (sess['relay_url'], sess['code']), method='POST', payload={}, timeout=3)
            except Exception:
                pass
        try:
            sess['srv'].shutdown()
            sess['srv'].server_close()
        except Exception:
            pass
        note('support', 'Support session stopped (%s)' % sess['code'])
        return self.build_state()

    MacroEngine.support_status = support_status
    MacroEngine.start_support_session = start_support_session
    MacroEngine.stop_support_session = stop_support_session


_attach_support_host()

UNPACK_MARKS = ('auras.json', 'eden.png', 'gui_points.json')


def clear_old_unpacks(temp=None, current=None, fresh_s=120):
    current = current or getattr(sys, '_MEIPASS', None)
    temp = temp or tempfile.gettempdir()
    gone = 0
    for d in glob.glob(os.path.join(temp, '_MEI*')):
        if current and os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.abspath(current)):
            continue
        if not d.endswith('-old'):
            if not all(os.path.isfile(os.path.join(d, f)) for f in UNPACK_MARKS):
                continue
            core = sorted(glob.glob(os.path.join(d, 'python3*.dll')), key=len, reverse=True)[:1]
            try:
                if not core or time.time() - os.path.getmtime(d) < fresh_s:
                    continue
                os.remove(core[0])
                os.rename(d, d + '-old')
            except OSError:
                continue
            d += '-old'
        shutil.rmtree(d, ignore_errors=True)
        gone += not os.path.exists(d)
    return gone


def _clear_old_unpacks():
    try:
        n = clear_old_unpacks()
        if n:
            note('cleanup', 'deleted %d old unpacked cop%s of Veyra from %%TEMP%%' % (n, 'y' if n == 1 else 'ies'))
    except Exception as e:
        note('cleanup', '%s: %s' % (type(e).__name__, e))


def _other_instance_pids():
    """Other Veyra processes for this install. Calibrator windows are not copies of the app."""
    me = os.getpid()
    app = os.path.normcase(os.path.abspath(os.path.join(HERE_DIR, 'app.py')))
    exe = os.path.normcase(os.path.abspath(sys.executable))
    frozen = getattr(sys, 'frozen', False)
    found = []
    try:
        import psutil
    except Exception:
        return found
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            pid = int(proc.info['pid'] or 0)
            if not pid or pid == me:
                continue
            cmd = proc.info['cmdline'] or []
            joined = ' '.join(cmd).lower()
            if '--calibrate' in joined:
                continue
            if frozen:
                if cmd and os.path.normcase(os.path.abspath(cmd[0])) == exe:
                    found.append(pid)
                continue
            for arg in cmd:
                if not arg.lower().endswith('app.py'):
                    continue
                if os.path.normcase(os.path.abspath(arg)) == app:
                    found.append(pid)
                    break
        except (OSError, ValueError, psutil.Error):
            continue
    return found


def _release_instance_lock():
    global _one
    k = ctypes.windll.kernel32
    for h in list(_one or []):
        try:
            if h:
                k.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            pass
    _one = []


def _visible_windows(pids):
    """Visible top-level windows owned by these processes."""
    u32 = ctypes.windll.user32
    wanted = set(int(p) for p in pids)
    found = []

    def cb(h, _):
        if not wanted:
            return True
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in wanted and u32.IsWindowVisible(h):
            found.append(h)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def _end_stuck_instances(pids):
    """Close leftover Veyra processes that have no window, so a new launch can open."""
    try:
        import psutil
    except Exception:
        return
    for pid in pids:
        try:
            psutil.Process(int(pid)).kill()
        except (OSError, ValueError, psutil.Error):
            pass


def only_one(_retry=True):
    """One Veyra per Windows session. A second launch must not open another copy."""
    global _one, _existing_pids
    others = _other_instance_pids()
    visible = _visible_windows(others)
    stuck = []
    if others and not visible:
        now = time.time()
        try:
            import psutil
            for pid in others:
                try:
                    age = now - psutil.Process(int(pid)).create_time()
                except (OSError, ValueError, psutil.Error):
                    age = 999
                if age >= 8:
                    stuck.append(pid)
        except Exception:
            stuck = list(others)
    _existing_pids = [pid for pid in others if pid not in stuck]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    k.CreateFileW.restype = ctypes.c_void_p
    invalid = ctypes.c_void_p(-1).value
    ctypes.set_last_error(0)
    local = k.CreateMutexW(None, False, 'Local\\VeyraSingle')
    local_busy = (not local) or ctypes.get_last_error() == 183
    lock_path = os.path.join(os.getenv('LOCALAPPDATA') or os.path.expanduser('~'), 'Veyra', 'instance.lock')
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    except OSError:
        pass
    ctypes.set_last_error(0)
    file_h = k.CreateFileW(lock_path, 0x80000000 | 0x40000000, 0, None, 4, 0x80, None)
    file_busy = (not file_h) or file_h == invalid
    _one = []
    if not local_busy and local:
        _one.append(local)
    elif local:
        k.CloseHandle(ctypes.c_void_p(local))
    if not file_busy and file_h:
        _one.append(file_h)
    elif file_h and file_h != invalid:
        k.CloseHandle(ctypes.c_void_p(file_h))
    if stuck:
        _end_stuck_instances(stuck)
        if local_busy or file_busy:
            _release_instance_lock()
            if _retry:
                time.sleep(0.4)
                return only_one(_retry=False)
            return 'Veyra is already open.'
    if local_busy or file_busy or _existing_pids:
        if _one and not (local_busy or file_busy):
            _release_instance_lock()
        return 'Veyra is already open.'
    user = re.sub(r'[^\w.-]', '_', os.environ.get('USERDOMAIN', '') + '.' + os.environ.get('USERNAME', ''))
    ctypes.set_last_error(0)
    glob = k.CreateMutexW(None, False, 'Global\\VeyraSingle-' + user)
    glob_busy = (not glob) or ctypes.get_last_error() == 183
    if glob_busy:
        if glob:
            k.CloseHandle(ctypes.c_void_p(glob))
        _release_instance_lock()
        return ('Veyra is already running for this Windows account in another session (another RDP).\n\n'
                'Two macros on one account share Roblox\'s logs and Veyra\'s settings, so each RDP needs its own '
                'Windows account. Log the second RDP into a different account, then open Veyra there.')
    _one.append(glob)
    return None


def _focus_existing_mmacro():
    """Bring the Veyra that is already running to the front. Do not open a second one."""
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    wanted = set(_existing_pids or [])
    found = []

    def cb(h, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        title = ''
        n = u32.GetWindowTextLengthW(h)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, buf, n + 1)
            title = buf.value or ''
        title_ok = title == TITLE or title.startswith(TITLE + '  |')
        if u32.IsWindowVisible(h) and (pid.value in wanted or title_ok):
            found.append((0 if title_ok else 1, h))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    if not found:
        return False
    found.sort()
    hwnd = found[0][1]
    u32.ShowWindow(hwnd, 9)
    fg = u32.GetForegroundWindow()
    this_tid = k32.GetCurrentThreadId()
    fg_tid = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    if fg_tid and fg_tid != this_tid:
        u32.AttachThreadInput(this_tid, fg_tid, True)
    u32.BringWindowToTop(hwnd)
    u32.SetForegroundWindow(hwnd)
    if fg_tid and fg_tid != this_tid:
        u32.AttachThreadInput(this_tid, fg_tid, False)
    return True


def _installed_icon():
    """Path the shell should show, and a token that changes when that icon file changes."""
    if getattr(sys, 'frozen', False):
        path = os.path.abspath(sys.executable)
        location = path + ',0'
    else:
        path = os.path.join(HERE_DIR, 'icon.ico')
        if not os.path.isfile(path):
            return '', '', ''
        path = os.path.abspath(path)
        location = path + ',0'
    try:
        st = os.stat(path)
    except OSError:
        return '', '', ''
    token = '%s|%s|%s' % (path.lower(), st.st_size, int(st.st_mtime))
    if path.lower().endswith('.ico') and st.st_size < 5_000_000:
        try:
            import hashlib
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                h.update(f.read())
            token = path.lower() + '|' + h.hexdigest()
        except OSError:
            pass
    return path, location, token


def _apply_window_icon(hwnd):
    """Put the current icon.ico on this window so the title bar and taskbar match the update."""
    ico = os.path.join(HERE_DIR, 'icon.ico')
    if not hwnd or not os.path.isfile(ico):
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    big = user32.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
    small = user32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    WM_SETICON = 0x0080
    if big:
        user32.SendMessageW(hwnd, WM_SETICON, 1, big)
    if small:
        user32.SendMessageW(hwnd, WM_SETICON, 0, small)


def _notify_shell_icon(*paths):
    shell32 = ctypes.windll.shell32
    SHCNE_UPDATEITEM = 0x00002000
    SHCNE_ASSOCCHANGED = 0x08000000
    SHCNF_PATHW = 0x0005
    SHCNF_FLUSH = 0x1000
    SHCNF_IDLIST = 0x0000
    for p in paths:
        if p and os.path.exists(p):
            shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSH, ctypes.c_wchar_p(os.path.abspath(p)), None)
    shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


def _sync_installed_icon():
    """When an update replaces the icon, point shortcuts at it and drop the old Windows cache."""
    path, location, token = _installed_icon()
    if not path or not location or not token:
        return
    stamp_dir = os.path.join(os.getenv('LOCALAPPDATA') or os.path.expanduser('~'), 'Veyra')
    stamp_path = os.path.join(stamp_dir, 'icon.stamp')
    try:
        with open(stamp_path, encoding='utf-8') as f:
            prev = f.read().strip()
    except OSError:
        prev = ''
    if prev == token:
        return
    app_py = '' if getattr(sys, 'frozen', False) else os.path.abspath(os.path.join(HERE_DIR, 'app.py'))
    exe = os.path.abspath(sys.executable) if getattr(sys, 'frozen', False) else ''
    ps1 = os.path.join(tempfile.gettempdir(), 'veyra_icon_refresh.ps1')
    script = r'''param($IconLoc, $Exe, $AppPy, $AppDir)
$sh = New-Object -ComObject WScript.Shell
$dirs = @(
  [Environment]::GetFolderPath('Desktop'),
  [Environment]::GetFolderPath('CommonDesktopDirectory'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu'),
  (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu'),
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'),
  $AppDir
)
$n = 0
foreach ($d in $dirs) {
  if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $lnk = $sh.CreateShortcut($_.FullName)
      $target = [string]$lnk.TargetPath
      $args = [string]$lnk.Arguments
      $match = $false
      if ($Exe -and ($target -ieq $Exe)) { $match = $true }
      if ($AppPy -and $args -and ($args -like ('*' + $AppPy + '*'))) { $match = $true }
      if ($match) {
        $lnk.IconLocation = $IconLoc
        $lnk.Save()
        $n++
      }
    } catch {}
  }
}
Write-Output $n
'''
    try:
        with open(ps1, 'w', encoding='utf-8') as f:
            f.write(script)
        subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps1,
             '-IconLoc', location, '-Exe', exe, '-AppPy', app_py, '-AppDir', os.path.abspath(HERE_DIR)],
            capture_output=True, text=True, timeout=25,
        )
    except Exception as e:
        note('update', 'icon refresh failed: %s' % e)
        return
    _notify_shell_icon(path, exe)
    try:
        os.makedirs(stamp_dir, exist_ok=True)
        with open(stamp_path, 'w', encoding='utf-8') as f:
            f.write(token)
    except OSError:
        pass
    note('update', 'app icon updated')


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--calibrate':
        import calibrator_ui
        calibrator_ui.run(sys.argv[2] if len(sys.argv) > 2 else 'all')
        sys.exit(0)
    why = only_one()
    if why:
        if why == 'Veyra is already open.':
            _focus_existing_mmacro()
            sys.exit(0)
        dark_message(TITLE, why)
        sys.exit(0)
    threading.Thread(target=_clear_old_unpacks, daemon=True).start()

    srv = start_ui_server()
    port = srv.server_address[1]
    engine = MacroEngine()
    url = 'http://127.0.0.1:%d/ui/index.html%s' % (port, '?lite=1' if engine.wants_lite() else '')
    api = Api(engine)

    window = webview.create_window(
        TITLE,
        url,
        js_api=api,
        width=1160,
        height=740,
        min_size=(960, 620),
        background_color='#050810',
        on_top=False,
    )
    engine._window = window

    def on_shown():
        try:
            hwnd = _find_own_hwnd()
            engine._hwnd = hwnd
            _dark_titlebar_hwnd(hwnd)
            _apply_window_icon(hwnd)
        except Exception:
            pass
        threading.Thread(target=_sync_installed_icon, daemon=True).start()
        engine.check_update()

    window.events.shown += on_shown
    window.events.closing += lambda: engine.remember()
    window.events.closed += engine.close

    ico = os.path.join(HERE_DIR, 'icon.ico')
    try:
        webview.start(gui='edgechromium', icon=ico if os.path.isfile(ico) else None, debug=False)
    except Exception:
        try:
            webview.start(debug=False)
        except Exception as e2:
            dark_message(TITLE, f'Veyra could not open its WebView window.\n\n{e2}\n\n'
                         'Make sure Microsoft Edge WebView2 Runtime is installed and .NET Framework 4.8 is enabled.')
            sys.exit(1)


if __name__ == '__main__':
    main()
