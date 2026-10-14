#!/usr/bin/env python3
"""
Water Reminder - an animated hydration buddy for Windows, macOS and Linux.

Every N minutes a cute water-drop mascot swims onto your screen holding a glass
of water, while a wave rises along the bottom of the screen. Click
"I drank a glass" to log it, or snooze it.

Usage:
  water_reminder.py              run in the background (normally started at login)
  water_reminder.py --now        show the reminder right now
  water_reminder.py --settings   open the settings window
  water_reminder.py --stats      open history & stats
  water_reminder.py --status     print today's progress
  water_reminder.py --stop       stop the background reminder
  water_reminder.py --install    set up start-at-login for this user
  water_reminder.py --uninstall  remove start-at-login (add --purge to delete history)

Requires Python 3.9+ and PySide6 (pip install PySide6-Essentials).
"""
import argparse
import datetime
import getpass
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave
from functools import lru_cache

APP_NAME = "Water Reminder"
APP_VERSION = "1.3.0"
UPDATE_REPO = "mhd-mansoor/water-reminder"  # GitHub "owner/repo" that publishes releases
IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_ID = "water-reminder"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# On Linux Wayland, use XWayland so the overlay can be positioned and kept on top.
if IS_LINUX and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY") \
        and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "xcb"

try:
    from PySide6.QtCore import QCoreApplication, QElapsedTimer, QPointF, QRectF, Qt, QTime, QTimer, QUrl
    from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QFont, QFontMetricsF,
                               QGuiApplication, QIcon, QLinearGradient, QPainter, QPainterPath,
                               QPen, QPixmap, QPolygonF, QRadialGradient)
    from PySide6.QtNetwork import QLocalServer, QLocalSocket, QNetworkAccessManager, QNetworkReply, QNetworkRequest
    from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDialog, QFormLayout, QFrame, QHBoxLayout,
                                   QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
                                   QSpinBox, QSystemTrayIcon, QTextBrowser,
                                   QTimeEdit, QVBoxLayout, QWidget)
except ImportError:
    print("PySide6 is not installed. Run:  python3 -m pip install PySide6-Essentials", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _home(*p):
    return os.path.join(os.path.expanduser("~"), *p)


if IS_WIN:
    DATA_DIR = os.path.join(os.environ.get("APPDATA") or _home("AppData", "Roaming"), "WaterReminder")
    CONFIG_DIR = DATA_DIR
    APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or _home("AppData", "Local"), "WaterReminder", "app")
elif IS_MAC:
    DATA_DIR = _home("Library", "Application Support", "WaterReminder")
    CONFIG_DIR = DATA_DIR
    APP_DIR = os.path.join(DATA_DIR, "app")
else:
    DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or _home(".local", "share"), APP_ID)
    CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _home(".config"), APP_ID)
    APP_DIR = os.path.join(DATA_DIR, "app")

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "service.log")
SERVER_NAME = "water-reminder-" + re.sub(r"\W", "_", getpass.getuser())

DEFAULTS = {
    "interval_minutes": 30,
    "snooze_minutes": 10,
    "daily_goal_glasses": 8,
    "glass_ml": 250,
    "quiet_hours": {"enabled": True, "start": "22:00", "end": "08:00"},
    "auto_hide_minutes": 3,
    "sound": True,
    "name": "",
    "respect_focus": True,      # don't interrupt full-screen apps, presentations, calls, DND
    "pause_when_away": True,    # freeze the countdown while you're away from the computer
    "away_minutes": 5,
    "check_updates": True,      # look for a new version once a day
}

TIPS = [
    "Your brain is about 75% water. Keep it sharp!",
    "Feeling tired or foggy? It might just be thirst.",
    "A glass now beats a headache later.",
    "Hydrated eyes stare at screens more happily.",
    "Small sips, all day long. You've got this.",
    "Water helps your focus, mood and memory.",
    "Stretch your legs on the way to refill!",
    "Your code compiles better when you're hydrated. Probably.",
]


# --------------------------------------------------------------------------- #
# Config & state
# --------------------------------------------------------------------------- #
def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return _merge(DEFAULTS, json.load(f))
    except FileNotFoundError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
        return dict(DEFAULTS)
    except Exception as e:
        print(f"[{APP_ID}] config error ({e}); using defaults", file=sys.stderr)
        return dict(DEFAULTS)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def load_state():
    today = datetime.date.today().isoformat()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("history", {})
    if st.get("date") != today:
        st["date"] = today
        st["count"] = 0
    return st


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    st["history"][st["date"]] = st["count"]
    for k in sorted(st["history"])[:-400]:
        del st["history"][k]
    times = st.get("times", {})
    for k in sorted(times)[:-60]:
        del times[k]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)


def record_glass():
    """Log one glass now; returns the updated state."""
    st = load_state()
    if "total" not in st:
        st["total"] = sum(v for k, v in st["history"].items() if k != st["date"]) + st["count"]
    st["count"] += 1
    st["total"] += 1
    st.setdefault("times", {}).setdefault(st["date"], []).append(datetime.datetime.now().strftime("%H:%M"))
    save_state(st)
    return st


def undo_glass():
    st = load_state()
    if st["count"] > 0:
        st["count"] -= 1
        if "total" in st:
            st["total"] = max(0, st["total"] - 1)
        times = st.get("times", {}).get(st["date"])
        if times:
            times.pop()
        save_state(st)
    return st


def in_quiet_hours(cfg):
    q = cfg.get("quiet_hours") or {}
    if not q.get("enabled", True):
        return False
    try:
        s = datetime.time.fromisoformat(q.get("start", "22:00"))
        e = datetime.time.fromisoformat(q.get("end", "08:00"))
    except ValueError:
        return False
    now = datetime.datetime.now().time()
    return (s <= now < e) if s <= e else (now >= s or now < e)


def user_first_name(cfg):
    if cfg.get("name"):
        return cfg["name"]
    full = ""
    try:
        if IS_MAC:
            full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=2).stdout.strip()
        elif IS_LINUX:
            import pwd
            full = (pwd.getpwuid(os.getuid()).pw_gecos or "").split(",")[0].strip()
    except Exception:
        pass
    name = full.split()[0] if full else getpass.getuser()
    name = re.split(r"[._@\s]", name)[0] or "friend"
    return name[:1].upper() + name[1:]


# --------------------------------------------------------------------------- #
# Sound: a synthesized "bloop" (no sound files needed)
# --------------------------------------------------------------------------- #
def _make_bloop(path, double):
    rate = 44100
    frames = bytearray()

    def bloop(f0, f1, dur, vol):
        n = int(rate * dur)
        phase = 0.0
        for i in range(n):
            t = i / n
            phase += 2 * math.pi * f0 * (f1 / f0) ** t / rate
            env = math.sin(math.pi * min(1, t * 6)) if t < 1 / 12 else (1 - t) ** 2.2
            frames.extend(struct.pack("<h", int(32767 * vol * env * math.sin(phase))))

    bloop(380, 1100, 0.13, 0.45)
    if double:
        frames.extend(b"\x00\x00" * int(rate * 0.07))
        bloop(520, 1500, 0.12, 0.40)
    frames.extend(b"\x00\x00" * int(rate * 0.05))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def play_sound(kind):
    path = os.path.join(DATA_DIR, f"{kind}.wav")
    try:
        if not os.path.exists(path):
            os.makedirs(DATA_DIR, exist_ok=True)
            _make_bloop(path, double=(kind == "reminder"))
        if IS_WIN:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        players = [["afplay"]] if IS_MAC else [["paplay"], ["pw-play"], ["aplay", "-q"]]
        for player in players:
            if shutil.which(player[0]):
                subprocess.Popen(player + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Platform helpers (all best-effort)
# --------------------------------------------------------------------------- #
class _X11:
    """Tiny ctypes bridge to Xlib for click-through input shapes on Linux/X11."""
    _dpy = None
    _ok = None

    @classmethod
    def _init(cls):
        if cls._ok is not None:
            return cls._ok
        cls._ok = False
        try:
            import ctypes
            import ctypes.util
            cls.ct = ctypes
            cls.xlib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
            cls.xext = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
            cls.xlib.XOpenDisplay.restype = ctypes.c_void_p
            cls.xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            cls.xlib.XFlush.argtypes = [ctypes.c_void_p]
            cls.xlib.XInternAtom.restype = ctypes.c_ulong
            cls.xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            cls.xlib.XGetSelectionOwner.restype = ctypes.c_ulong
            cls.xlib.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            cls.xlib.XDefaultScreen.argtypes = [ctypes.c_void_p]

            class XRectangle(ctypes.Structure):
                _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                            ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]
            cls.XRectangle = XRectangle
            cls.xext.XShapeCombineRectangles.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int]
            cls._dpy = cls.xlib.XOpenDisplay(None)
            cls._ok = bool(cls._dpy)
            # Xlib's default error handler exits the process; ignore errors instead
            # (e.g. a window that closed while we were reading its properties).
            handler_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            cls._err_handler = handler_t(lambda _d, _e: 0)
            cls.xlib.XSetErrorHandler.argtypes = [handler_t]
            cls.xlib.XSetErrorHandler(cls._err_handler)
        except Exception:
            cls._ok = False
        return cls._ok

    @classmethod
    def has_compositor(cls):
        if not cls._init():
            return True  # unknown: assume yes
        scr = cls.xlib.XDefaultScreen(cls._dpy)
        atom = cls.xlib.XInternAtom(cls._dpy, f"_NET_WM_CM_S{scr}".encode(), 0)
        return cls.xlib.XGetSelectionOwner(cls._dpy, atom) != 0

    @classmethod
    def set_input_rects(cls, wid, rects):
        if not cls._init():
            return
        arr = (cls.XRectangle * max(1, len(rects)))()
        for i, (x, y, w, h) in enumerate(rects):
            arr[i] = cls.XRectangle(int(x), int(y), max(0, int(w)), max(0, int(h)))
        # ShapeInput = 2, ShapeSet = 0, Unsorted = 0
        cls.xext.XShapeCombineRectangles(cls._dpy, int(wid), 2, 0, 0, arr, len(rects), 0, 0)
        cls.xlib.XFlush(cls._dpy)

    @classmethod
    def _prep_prop(cls):
        if getattr(cls, "_prop_ready", False):
            return
        ct, x = cls.ct, cls.xlib
        x.XGetWindowProperty.argtypes = [ct.c_void_p, ct.c_ulong, ct.c_ulong, ct.c_long, ct.c_long, ct.c_int,
                                         ct.c_ulong, ct.POINTER(ct.c_ulong), ct.POINTER(ct.c_int),
                                         ct.POINTER(ct.c_ulong), ct.POINTER(ct.c_ulong),
                                         ct.POINTER(ct.POINTER(ct.c_ubyte))]
        x.XFree.argtypes = [ct.c_void_p]
        x.XDefaultRootWindow.restype = ct.c_ulong
        x.XDefaultRootWindow.argtypes = [ct.c_void_p]
        x.XSync.argtypes = [ct.c_void_p, ct.c_int]
        cls._prop_ready = True

    @classmethod
    def _prop(cls, win, name):
        """Return a list of 32-bit items of a window property (or [])."""
        ct = cls.ct
        x = cls.xlib
        cls._prep_prop()
        atom = x.XInternAtom(cls._dpy, name.encode(), 0)
        at, af, n, after = ct.c_ulong(), ct.c_int(), ct.c_ulong(), ct.c_ulong()
        data = ct.POINTER(ct.c_ubyte)()
        if x.XGetWindowProperty(cls._dpy, win, atom, 0, 1024, 0, 0, ct.byref(at), ct.byref(af), ct.byref(n),
                                ct.byref(after), ct.byref(data)) != 0 or not data:
            return []
        try:
            if af.value != 32:
                return []
            arr = ct.cast(data, ct.POINTER(ct.c_ulong))
            return [arr[i] for i in range(n.value)]
        finally:
            x.XFree(data)

    @classmethod
    def active_window_fullscreen(cls):
        if not cls._init():
            return False
        cls._prep_prop()
        root = cls.xlib.XDefaultRootWindow(cls._dpy)
        active = cls._prop(root, "_NET_ACTIVE_WINDOW")
        if not active or not active[0]:
            return False
        full = cls.xlib.XInternAtom(cls._dpy, b"_NET_WM_STATE_FULLSCREEN", 0)
        states = cls._prop(active[0], "_NET_WM_STATE")
        cls.xlib.XSync(cls._dpy, 0)  # flush any (harmless, ignored) BadWindow error
        return full in states

    @classmethod
    def idle_seconds(cls):
        """X screensaver idle time (X11 sessions only)."""
        if not cls._init():
            return None
        ct = cls.ct
        if not hasattr(cls, "xss"):
            import ctypes.util

            class Info(ct.Structure):
                _fields_ = [("window", ct.c_ulong), ("state", ct.c_int), ("kind", ct.c_int),
                            ("til_or_since", ct.c_ulong), ("idle", ct.c_ulong), ("eventMask", ct.c_ulong)]
            try:
                cls.xss = ct.CDLL(ctypes.util.find_library("Xss") or "libXss.so.1")
            except OSError:
                cls.xss = None
                return None
            cls.xss.XScreenSaverAllocInfo.restype = ct.POINTER(Info)
            cls.xss.XScreenSaverQueryInfo.argtypes = [ct.c_void_p, ct.c_ulong, ct.POINTER(Info)]
            cls.xlib.XDefaultRootWindow.restype = ct.c_ulong
            cls.xlib.XDefaultRootWindow.argtypes = [ct.c_void_p]
            cls.xss_info = cls.xss.XScreenSaverAllocInfo()
        if not cls.xss:
            return None
        root = cls.xlib.XDefaultRootWindow(cls._dpy)
        if not cls.xss.XScreenSaverQueryInfo(cls._dpy, root, cls.xss_info):
            return None
        return cls.xss_info.contents.idle / 1000.0


def is_x11():
    return IS_LINUX and QGuiApplication.platformName() == "xcb"


# --------------------------------------------------------------------------- #
# System probes: how long the user has been idle, and whether they are busy
# (full-screen app / presentation / call / Do Not Disturb). All best-effort:
# any failure means "unknown", which never blocks a reminder.
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=2):
    try:
        flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: no console flash
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _win_idle():
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def _win_device_in_use(device):
    """True if any app is using the microphone/webcam right now (Windows 10/11 privacy registry)."""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore" + "\\" + device

    def walk(path, depth=0):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return False
        with key:
            try:
                start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
                stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
                if start and not stop:
                    return True
            except OSError:
                pass
            if depth < 3:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    if walk(path + "\\" + sub, depth + 1):
                        return True
                    i += 1
        return False
    return walk(base)


def _win_busy():
    import ctypes
    state = ctypes.c_int(0)
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
        if state.value in (2, 3):   # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN
            return "a full-screen app"
        if state.value == 4:        # QUNS_PRESENTATION_MODE
            return "presentation mode"
    if _win_device_in_use("microphone") or _win_device_in_use("webcam"):
        return "a call"
    return None


def _mac_idle():
    import ctypes
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    f = cg.CGEventSourceSecondsSinceLastEventType
    f.restype = ctypes.c_double
    f.argtypes = [ctypes.c_int32, ctypes.c_uint32]
    return f(1, 0xFFFFFFFF)  # HID system state, any input event


def _mac_fullscreen():
    """Is the frontmost normal window covering the whole main display?"""
    import ctypes as ct
    cf = ct.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg = ct.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")

    class CGPoint(ct.Structure):
        _fields_ = [("x", ct.c_double), ("y", ct.c_double)]

    class CGSize(ct.Structure):
        _fields_ = [("width", ct.c_double), ("height", ct.c_double)]

    class CGRect(ct.Structure):
        _fields_ = [("origin", CGPoint), ("size", CGSize)]

    cg.CGWindowListCopyWindowInfo.restype = ct.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ct.c_uint32, ct.c_uint32]
    cg.CGMainDisplayID.restype = ct.c_uint32
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayBounds.argtypes = [ct.c_uint32]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ct.c_bool
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [ct.c_void_p, ct.POINTER(CGRect)]
    cf.CFArrayGetCount.restype = ct.c_long
    cf.CFArrayGetCount.argtypes = [ct.c_void_p]
    cf.CFArrayGetValueAtIndex.restype = ct.c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [ct.c_void_p, ct.c_long]
    cf.CFDictionaryGetValue.restype = ct.c_void_p
    cf.CFDictionaryGetValue.argtypes = [ct.c_void_p, ct.c_void_p]
    cf.CFStringCreateWithCString.restype = ct.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ct.c_void_p, ct.c_char_p, ct.c_uint32]
    cf.CFNumberGetValue.restype = ct.c_bool
    cf.CFNumberGetValue.argtypes = [ct.c_void_p, ct.c_int, ct.c_void_p]
    cf.CFRelease.argtypes = [ct.c_void_p]

    keys = [cf.CFStringCreateWithCString(None, k, 0x08000100)
            for k in (b"kCGWindowLayer", b"kCGWindowOwnerPID", b"kCGWindowBounds")]
    arr = cg.CGWindowListCopyWindowInfo(1 | 16, 0)  # on-screen only, exclude desktop
    try:
        if not arr:
            return False
        disp = cg.CGDisplayBounds(cg.CGMainDisplayID())
        for i in range(cf.CFArrayGetCount(arr)):
            d = cf.CFArrayGetValueAtIndex(arr, i)
            layer, pid = ct.c_int(-1), ct.c_int(0)
            v = cf.CFDictionaryGetValue(d, keys[0])
            if not v or not cf.CFNumberGetValue(v, 9, ct.byref(layer)) or layer.value != 0:
                continue
            v = cf.CFDictionaryGetValue(d, keys[1])
            if v and cf.CFNumberGetValue(v, 9, ct.byref(pid)) and pid.value == os.getpid():
                continue
            r = CGRect()
            b = cf.CFDictionaryGetValue(d, keys[2])
            if not b or not cg.CGRectMakeWithDictionaryRepresentation(b, ct.byref(r)):
                continue
            return r.size.width >= disp.size.width and r.size.height >= disp.size.height
        return False
    finally:
        if arr:
            cf.CFRelease(arr)
        for k in keys:
            if k:
                cf.CFRelease(k)


def _mac_busy():
    return "a full-screen app" if _mac_fullscreen() else None


_MUTTER_OK = None


def _linux_idle():
    global _MUTTER_OK
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") + os.environ.get("DESKTOP_SESSION", "")).lower()
    if _MUTTER_OK is not False and shutil.which("gdbus") and "gnome" in desktop + "ubuntu":
        out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                    "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                    "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
        m = re.search(r"(\d+)", out or "")
        _MUTTER_OK = bool(m)
        if m:
            return int(m.group(1)) / 1000.0
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return None  # XWayland can't see native Wayland input: unknown is safer than "away"
    return _X11.idle_seconds()


def _linux_busy():
    out = _run(["gsettings", "get", "org.gnome.desktop.notifications", "show-banners"]) \
        if shutil.which("gsettings") else None
    if out and out.strip() == "false":
        return "Do Not Disturb"
    if shutil.which("pactl"):
        out = _run(["pactl", "list", "source-outputs", "short"])
        if out and out.strip():
            return "a call"
    if os.environ.get("DISPLAY") and _X11.active_window_fullscreen():
        return "a full-screen app"
    return None


class Probes:
    _cache = {}

    @classmethod
    def _cached(cls, key, ttl, fn):
        now = time.monotonic()
        hit = cls._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except Exception:
            val = None
        cls._cache[key] = (now, val)
        return val

    @classmethod
    def idle_seconds(cls):
        """Seconds since the last keyboard/mouse input, or None if unknown."""
        fn = _win_idle if IS_WIN else _mac_idle if IS_MAC else _linux_idle
        return cls._cached("idle", 5, fn)

    @classmethod
    def busy_reason(cls):
        """A short reason the user shouldn't be interrupted right now, or None."""
        fn = _win_busy if IS_WIN else _mac_busy if IS_MAC else _linux_busy
        return cls._cached("busy", 10, fn)


def mac_hide_dock_icon():
    """Run as a menu-bar-only app (no Dock icon) on macOS."""
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ns_app = objc.objc_msgSend(objc.objc_getClass(b"NSApplication"),
                                   objc.sel_registerName(b"sharedApplication"))
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        objc.objc_msgSend(ns_app, objc.sel_registerName(b"setActivationPolicy:"), 1)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #
def clamp(v, a, b):
    return a if v < a else b if v > b else v


def lerp(a, b, t):
    return a + (b - a) * t


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_in_cubic(t):
    return t * t * t


def ease_out_back(t, s=1.9):
    t -= 1
    return t * t * ((s + 1) * t + s) + 1


def C(r, g, b, a=1.0):
    return QColor.fromRgbF(clamp(r, 0, 1), clamp(g, 0, 1), clamp(b, 0, 1), clamp(a, 0, 1))


def _rect(cx, cy, r):
    return QRectF(cx - r, cy - r, 2 * r, 2 * r)


def arc_to(path, cx, cy, r, a1, a2):
    """Cairo-style arc (radians, clockwise on screen), connected to the current point."""
    path.arcTo(_rect(cx, cy, r), -math.degrees(a1), -math.degrees(a2 - a1))


def arc_path(cx, cy, r, a1, a2):
    p = QPainterPath()
    p.arcMoveTo(_rect(cx, cy, r), -math.degrees(a1))
    p.arcTo(_rect(cx, cy, r), -math.degrees(a1), -math.degrees(a2 - a1))
    return p


def drop_path(R, cx=0.0, cy=0.0):
    p = QPainterPath()
    p.moveTo(cx, cy - 1.75 * R)
    p.cubicTo(cx + 0.3 * R, cy - 1.2 * R, cx + R, cy - 0.75 * R, cx + R, cy)
    arc_to(p, cx, cy, R, 0, math.pi)
    p.cubicTo(cx - R, cy - 0.75 * R, cx - 0.3 * R, cy - 1.2 * R, cx, cy - 1.75 * R)
    p.closeSubpath()
    return p


def rounded(x, y, w, h, r):
    p = QPainterPath()
    r = min(r, w / 2, h / 2)
    p.addRoundedRect(QRectF(x, y, w, h), r, r)
    return p


def glass_path(x, y, w, h):
    inset = w * 0.12
    p = QPainterPath()
    p.addPolygon(QPolygonF([QPointF(x, y), QPointF(x + w, y), QPointF(x + w - inset, y + h),
                            QPointF(x + inset, y + h)]))
    p.closeSubpath()
    return p


def fill(p, path, brush):
    p.setPen(Qt.NoPen)
    p.setBrush(brush)
    p.drawPath(path)


def stroke(p, path, color, width):
    p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def ellipse(p, cx, cy, rx, ry, brush):
    p.setPen(Qt.NoPen)
    p.setBrush(brush)
    p.drawEllipse(QPointF(cx, cy), rx, ry)


def lin(x0, y0, x1, y1, stops):
    g = QLinearGradient(x0, y0, x1, y1)
    for pos, col in stops:
        g.setColorAt(pos, col)
    return g


def rad(cx, cy, r, stops):
    g = QRadialGradient(cx, cy, r)
    for pos, col in stops:
        g.setColorAt(pos, col)
    return g


@lru_cache(maxsize=64)
def make_font(px, bold=False):
    f = QFont(QGuiApplication.font())
    f.setPixelSize(max(1, int(round(px))))
    f.setBold(bold)
    return f


@lru_cache(maxsize=256)
def _text_height(font_key, text, width):
    px, bold = font_key
    fm = QFontMetricsF(make_font(px, bold))
    return fm.boundingRect(QRectF(0, 0, width, 100000), int(Qt.TextWordWrap), text).height()


def text_height(font, text, width):
    return _text_height((font.pixelSize(), font.bold()), text, round(width, 1))


def draw_glass(p, x, y, w, h, level, t, tilt=0.0):
    gp = glass_path(x, y, w, h)
    fill(p, gp, C(1, 1, 1, 0.30))
    if level > 0.01:
        p.save()
        p.setClipPath(gp)
        top = y + h * (1 - level * 0.88)
        wp = QPainterPath()
        wp.moveTo(x - 2, y + h + 2)
        steps = 12
        for i in range(steps + 1):
            px = x - 2 + (w + 4) * i / steps
            wp.lineTo(px, top + math.sin(t * 5 + i * 0.9) * h * 0.035 + (i / steps - 0.5) * tilt * h * 0.5)
        wp.lineTo(x + w + 2, y + h + 2)
        wp.closeSubpath()
        fill(p, wp, lin(0, top, 0, y + h, [(0, C(0.55, 0.88, 1.0, 0.95)), (1, C(0.12, 0.50, 0.95, 0.95))]))
        p.restore()
    stroke(p, gp, C(1, 1, 1, 0.95), max(1.5, w * 0.07))
    shine = QPainterPath()
    shine.moveTo(x + w * 0.22, y + h * 0.15)
    shine.lineTo(x + w * 0.28, y + h * 0.75)
    stroke(p, shine, C(1, 1, 1, 0.75), max(1.2, w * 0.06))


# --------------------------------------------------------------------------- #
# Scene: animation state + drawing, in "screen" coordinates
# --------------------------------------------------------------------------- #
class Particle:
    __slots__ = ("kind", "x", "y", "vx", "vy", "r", "life", "max_life", "spin", "seed")

    def __init__(self, kind, x, y, vx, vy, r, life):
        self.kind, self.x, self.y, self.vx, self.vy, self.r = kind, x, y, vx, vy, r
        self.life = self.max_life = life
        self.spin = random.uniform(-6, 6)
        self.seed = random.uniform(0, 100)


class Scene:
    ENTER_DUR = 3.2
    EXIT_DUR = 1.6
    CELEBRATE_DUR = 2.6

    def __init__(self, W, H, cfg, count, name, compact=False, intro=None):
        self.cfg = cfg
        self.intro = intro
        self.compact = compact
        self.count = count
        self.goal = max(1, int(cfg.get("daily_goal_glasses", 8)))
        self.ml = int(cfg.get("glass_ml", 250))
        self.snooze = int(cfg.get("snooze_minutes", 10))
        self.auto_hide = float(cfg.get("auto_hide_minutes", 3)) * 60
        self.name = name
        self.tip = random.choice(TIPS)

        u = self.u = 0.9 if compact else clamp(H / 1080, 0.85, 1.3)
        self.R = 52 * u
        self.bw = 372 * u
        self.pad = 20 * u
        self.gap = 24 * u
        self.bh_max = max(self.bubble_height(False), self.bubble_height(True))
        self.margin = 60 * u
        self.wave_h = 0 if compact else 70 * u
        self.fx_h = int(self.wave_h + 50 * u)

        # Layout of the (moving) mascot window, relative to the drop's centre
        self.win_w = int(self.bw + 2 * self.margin)
        self.anchor_y = self.margin + self.bh_max + self.gap + 1.75 * self.R + 0.95 * self.R
        self.win_h = int(self.anchor_y + self.R + 40 * u + self.margin)
        self.W, self.H = (self.win_w, self.win_h) if compact else (W, H)

        self.t = 0.0
        self.state = "enter"
        self.state_t = 0.0
        self.roam_t = 0.0
        self.speed = 1.0
        self.result = None
        self.done = False

        self._setup_roam()
        sx, sy = self.roam_pos(0)
        self.start = (sx, self.H + self.R * 3) if compact else (-self.R * 4, sy + 40 * u)
        self.pos = self.start
        self.vel = (0.0, 0.0)
        self.exit_from = self.exit_to = self.start

        self.pointer = None
        self.hover = None
        self.particles = []
        self.wave_level = 0.0
        self.bubble_vis = 0.0
        self.blink_next = 1.8
        self.blink_t = -1.0
        self.jump_t = -1.0
        self.glass_level = 0.8
        self.face = "normal"
        self.buttons = {}
        self.bubble_rect = None
        self._wave_spawn = 0.0
        self._trail_spawn = 0.0

    # -- text ---------------------------------------------------------------- #
    def _texts(self, celebrate, count=None):
        count = self.count if count is None else count
        if celebrate:
            if count < self.goal:
                return f"Great job, {self.name}!", f"+{self.ml} ml logged. Your body says thank you."
            return "Daily goal reached!", "You're a hydration hero today. Keep sipping!"
        if self.intro == "welcome":
            return f"Welcome back, {self.name}!", "Good to see you again. Start with a fresh glass of water."
        if self.intro == "updated":
            return (f"I'm all updated, {self.name}!",
                    f"Now on version {APP_VERSION}. Same friendly reminders, new tricks.")
        if self.intro == "hello":
            mins = int(self.cfg.get("interval_minutes", 30))
            return (f"Hi {self.name}, I'm your water buddy!",
                    f"I'll drop by every {mins} minutes. Click the gear to change that.")
        return f"Time to drink water, {self.name}!", self.tip

    def bubble_height(self, celebrate):
        u, pad = self.u, self.pad
        inner = self.bw - 2 * pad
        best = 0
        for c in (self.count + 1, self.goal + 1):
            title, sub = self._texts(celebrate, c)
            th = text_height(make_font(19 * u, True), title, inner - 84 * u)
            sh = text_height(make_font(13.5 * u), sub, inner)
            best = max(best, self._bh(th, sh, celebrate))
        return best

    def _bh(self, th, sh, celebrate):
        u = self.u
        btn = 0 if celebrate else 14 * u + 42 * u
        return self.pad + max(th, 26 * u) + 6 * u + sh + 14 * u + 24 * u + btn + self.pad

    # -- motion -------------------------------------------------------------- #
    def _setup_roam(self):
        u, R = self.u, self.R
        xmin, xmax = self.bw / 2 + 24 * u, self.W - self.bw / 2 - 24 * u
        if xmin > xmax:
            xmin = xmax = self.W / 2
        ymin = 1.75 * R + 0.95 * R + self.gap + self.bh_max + 16 * u
        ymax = self.H - R - self.wave_h - 34 * u
        if ymin > ymax:
            ymin = ymax
        self.cx, self.cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        self.ax, self.ay = (xmax - xmin) / 2, (ymax - ymin) / 2

    def roam_pos(self, rt):
        return (self.cx + self.ax * math.sin(0.16 * rt - 0.9),
                self.cy + self.ay * math.sin(0.27 * rt + 0.7))

    def origin(self):
        """Top-left of the mascot window, in scene coordinates."""
        if self.compact:
            return 0.0, 0.0
        x, y = self.pos
        return x - self.win_w / 2, y - self.anchor_y

    def leave(self, result):
        if self.state in ("exit", "celebrate"):
            return
        self.result = result
        if result == "drank":
            self.count += 1
            self.state, self.state_t = "celebrate", 0.0
            self.face = "happy"
            self._splash(34)
        else:
            self.face = "sleepy" if result == "snooze" else "normal"
            self._begin_exit()

    def _begin_exit(self):
        self.state, self.state_t = "exit", 0.0
        x, y = self.pos
        self.exit_from = (x, y)
        u = self.u
        if self.compact:
            self.exit_to = (x, self.H + self.R * 4)
        elif self.result == "snooze":
            self.exit_to = (-self.win_w, y + 60 * u)
        elif self.result == "drank":
            self.exit_to = (self.W + self.win_w, y - 160 * u)
        else:
            self.exit_to = (self.W + self.win_w, y + 40 * u)

    def poke(self):
        if self.jump_t < 0 and self.state == "idle":
            self.jump_t = 0.0
            self._splash(8, power=0.5)

    def _splash(self, n, power=1.0):
        x, y = self.pos
        u = self.u
        for _ in range(n):
            a = random.uniform(-math.pi * 0.92, -math.pi * 0.08)
            sp = random.uniform(220, 470) * u * power
            self.particles.append(Particle("drop", x, y - self.R * 0.6, math.cos(a) * sp,
                                           math.sin(a) * sp, random.uniform(4, 8) * u,
                                           random.uniform(0.8, 1.2)))
        for _ in range(int(n * 0.4)):
            a = random.uniform(0, 2 * math.pi)
            sp = random.uniform(70, 200) * u * power
            self.particles.append(Particle("spark", x, y - self.R * 0.6, math.cos(a) * sp,
                                           math.sin(a) * sp - 100 * u, random.uniform(5, 10) * u,
                                           random.uniform(0.8, 1.3)))

    def body_offset(self):
        R = self.R
        off = math.sin(self.t * 2.4) * 5 * self.u
        if self.jump_t >= 0:
            off -= math.sin(clamp(self.jump_t / 0.55, 0, 1) * math.pi) * R * 0.9
        if self.state == "celebrate":
            k = self.state_t / self.CELEBRATE_DUR
            off -= abs(math.sin(self.state_t * math.pi * 1.9)) * R * 0.75 * (1 - k * 0.6)
        return off

    def update(self, dt):
        self.t += dt
        self.state_t += dt
        u = self.u

        hovered = self.pointer is not None and self._pointer_near()
        self.speed += ((0.0 if hovered else 1.0) - self.speed) * min(1.0, dt * 4)

        if self.state == "enter":
            k = clamp(self.state_t / self.ENTER_DUR, 0, 1)
            e = ease_out_cubic(k)
            ex, ey = self.roam_pos(0)
            x = lerp(self.start[0], ex, e)
            y = lerp(self.start[1], ey, e) + math.sin(k * math.pi * 3) * 45 * u * (1 - k)
            if k >= 1:
                self.state, self.state_t = "idle", 0.0
        elif self.state == "idle":
            self.roam_t += dt * self.speed
            x, y = self.roam_pos(self.roam_t)
            if self.state_t > self.auto_hide:
                self.leave("timeout")
        elif self.state == "celebrate":
            x, y = self.pos
            self.glass_level = max(0.0, self.glass_level - dt * 1.2)
            if self.state_t > self.CELEBRATE_DUR:
                self._begin_exit()
        else:  # exit
            k = clamp(self.state_t / self.EXIT_DUR, 0, 1)
            e = ease_in_cubic(k)
            x = lerp(self.exit_from[0], self.exit_to[0], e)
            y = lerp(self.exit_from[1], self.exit_to[1], e) + math.sin(k * math.pi * 2) * 20 * u
            if k >= 1:
                x, y = self.exit_to
                if self.wave_level < 0.02 or self.compact:
                    self.done = True

        if dt > 0:
            vx, vy = (x - self.pos[0]) / dt, (y - self.pos[1]) / dt
            self.vel = (lerp(self.vel[0], vx, 0.2), lerp(self.vel[1], vy, 0.2))
        self.pos = (x, y)

        if self.jump_t >= 0:
            self.jump_t += dt
            if self.jump_t > 0.55:
                self.jump_t = -1.0

        self.blink_next -= dt
        if self.blink_next <= 0:
            self.blink_t = 0.0
            self.blink_next = random.uniform(2.2, 4.5)
        if self.blink_t >= 0:
            self.blink_t += dt
            if self.blink_t > 0.16:
                self.blink_t = -1.0

        target = 0.0 if self.state == "exit" else 1.0
        self.wave_level += (target - self.wave_level) * min(1.0, dt * (3.0 if target == 0 else 1.6))
        if self.state in ("idle", "celebrate"):
            self.bubble_vis = min(1.0, self.bubble_vis + dt / 0.5)
        else:
            self.bubble_vis = max(0.0, self.bubble_vis - dt / 0.22)

        self._update_particles(dt)

    def _update_particles(self, dt):
        u = self.u
        if self.wave_h and self.wave_level > 0.3 and self.state != "exit":
            self._wave_spawn += dt * 7 * self.wave_level
            while self._wave_spawn > 1:
                self._wave_spawn -= 1
                self.particles.append(Particle("bubble", random.uniform(0, self.W),
                                               self.H - random.uniform(5, 40) * u, 0,
                                               -random.uniform(15, 35) * u, random.uniform(3, 8) * u,
                                               random.uniform(1.2, 2.4)))
        sp = math.hypot(*self.vel)
        if sp > 30 * u and not self.compact:
            self._trail_spawn += dt * min(18, sp / (14 * u))
            while self._trail_spawn > 1:
                self._trail_spawn -= 1
                x, y = self.pos
                nx, ny = -self.vel[0] / sp, -self.vel[1] / sp
                self.particles.append(Particle(
                    "trail", x + nx * self.R * 1.1 + random.uniform(-8, 8) * u,
                    y + ny * self.R * 0.6 + random.uniform(-10, 10) * u,
                    nx * 20 * u, -random.uniform(20, 50) * u, random.uniform(3, 8) * u,
                    random.uniform(0.6, 1.2)))
        alive = []
        g = 1100 * u
        for p in self.particles:
            p.life -= dt
            if p.life <= 0:
                continue
            if p.kind == "drop":
                p.vy += g * dt
            elif p.kind == "spark":
                p.vy += g * 0.25 * dt
                p.vx *= 1 - dt * 1.5
            else:
                p.x += math.sin(self.t * 3 + p.seed) * 14 * u * dt
            p.x += p.vx * dt
            p.y += p.vy * dt
            alive.append(p)
        self.particles = alive[-400:]

    def _pointer_near(self):
        px, py = self.pointer
        for (x, y, w, h) in self.hit_rects(pad=18 * self.u):
            if x <= px <= x + w and y <= py <= y + h:
                return True
        return False

    # -- hit testing ---------------------------------------------------------- #
    def drop_rect(self, pad=0.0):
        x, y = self.pos
        R = self.R
        oy = self.body_offset()
        return (x - 1.5 * R - pad, y + oy - 2.0 * R - pad, 3.4 * R + 2 * pad, 3.2 * R + 2 * pad)

    def hit_rects(self, pad=0.0):
        if self.state == "exit":
            return []
        rects = [self.drop_rect(pad)]
        if self.bubble_rect and self.bubble_vis > 0.6:
            x, y, w, h = self.bubble_rect
            rects.append((x - pad, y - pad, w + 2 * pad, h + 2 * pad))
        return rects

    def hit(self, px, py):
        if self.state != "idle":
            return None
        if self.bubble_vis >= 0.95:
            for bid, (x, y, w, h) in self.buttons.items():
                if x <= px <= x + w and y <= py <= y + h:
                    return bid
        x, y, w, h = self.drop_rect()
        if x <= px <= x + w and y <= py <= y + h:
            return "drop"
        return None

    # -- drawing -------------------------------------------------------------- #
    def draw_fx(self, p):
        """Wave + rising bubbles (bottom strip of the screen)."""
        self._draw_wave(p)
        fx_rect = (0, self.H - self.fx_h, self.W, self.fx_h)
        self._draw_particles(p, ("bubble",), fx_rect)

    def draw_mascot(self, p, clip_rect=None):
        if self.compact:
            p.fillRect(QRectF(0, 0, self.W, self.H),
                       lin(0, 0, 0, self.H, [(0, C(0.93, 0.97, 1.0)), (1, C(0.78, 0.89, 0.99))]))
        self._draw_particles(p, ("trail",), clip_rect)
        self._draw_drop(p)
        self._draw_bubble(p)
        self._draw_particles(p, ("drop", "spark"), clip_rect)

    def _draw_wave(self, p):
        if not self.wave_h or self.wave_level < 0.01:
            return
        W, H, t, u = self.W, self.H, self.t, self.u
        base = self.wave_h * ease_out_cubic(clamp(self.wave_level, 0, 1))
        for amp, freq, spd, lift, alpha in ((12 * u, 0.0060, 0.9, 18 * u, 0.30),
                                            (9 * u, 0.0085, -1.3, 0.0, 0.42)):
            pts = []
            for x in range(-24, int(W) + 48, 24):
                pts.append(QPointF(x, H - base - lift + amp * math.sin(x * freq + t * spd)
                                   + amp * 0.45 * math.sin(x * freq * 2.3 - t * spd * 1.6)))
            body = QPainterPath()
            body.moveTo(-20, H + 2)
            for pt in pts:
                body.lineTo(pt)
            body.lineTo(W + 40, H + 2)
            body.closeSubpath()
            fill(p, body, lin(0, H - base - lift - amp, 0, H,
                              [(0, C(0.50, 0.85, 1.0, alpha)), (1, C(0.08, 0.40, 0.88, alpha + 0.2))]))
            foam = QPainterPath(pts[0])
            for pt in pts[1:]:
                foam.lineTo(pt)
            stroke(p, foam, C(1, 1, 1, alpha * 1.3), 2 * u)

    def _draw_particles(self, p, kinds, clip_rect=None):
        u = self.u
        edge = 40 * u
        for q in self.particles:
            if q.kind not in kinds:
                continue
            a = clamp(q.life / q.max_life, 0, 1)
            if clip_rect:
                cx, cy, cw, ch = clip_rect
                d = min(q.x - cx, cx + cw - q.x, q.y - cy, cy + ch - q.y)
                if d <= 0:
                    continue
                a *= clamp(d / edge, 0, 1)
            if q.kind in ("bubble", "trail"):
                fade = min(1.0, a * 3) * min(1.0, (q.max_life - q.life) * 4)
                ellipse(p, q.x, q.y, q.r, q.r, C(0.75, 0.92, 1, 0.18 * fade))
                p.setPen(QPen(C(1, 1, 1, 0.75 * fade), 1.4 * u))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(q.x, q.y), q.r, q.r)
                ellipse(p, q.x - q.r * 0.35, q.y - q.r * 0.35, q.r * 0.25, q.r * 0.25, C(1, 1, 1, 0.9 * fade))
            elif q.kind == "drop":
                p.save()
                p.translate(q.x, q.y)
                p.rotate(math.degrees(math.atan2(q.vy, q.vx) + math.pi / 2))
                s = q.r / 10
                p.scale(s, s)
                fill(p, drop_path(6), lin(-6, -10, 6, 6, [(0, C(0.65, 0.92, 1, a)), (1, C(0.12, 0.48, 0.95, a))]))
                p.restore()
            else:  # spark
                p.save()
                p.translate(q.x, q.y)
                p.rotate(math.degrees(self.t * q.spin * 0.3))
                r = q.r * (0.6 + 0.4 * math.sin(self.t * 12 + q.seed))
                pts = []
                for i in range(8):
                    rr = r if i % 2 == 0 else r * 0.32
                    ang = -math.pi / 2 + i * math.pi / 4
                    pts.append(QPointF(math.cos(ang) * rr, math.sin(ang) * rr))
                star = QPainterPath()
                star.addPolygon(QPolygonF(pts))
                star.closeSubpath()
                fill(p, star, C(1.0, 0.86, 0.35, a) if int(q.seed) % 2 else C(1, 1, 1, a))
                p.restore()

    def _draw_drop(self, p):
        R, u, t = self.R, self.u, self.t
        x, y = self.pos
        oy = self.body_offset()
        vx, vy = self.vel
        tilt = clamp(vx * 0.0005 / u, -0.28, 0.28)
        squash = clamp(-vy * 0.0004 / u, -0.12, 0.12) + 0.035 * math.sin(t * 4.8)
        if self.jump_t >= 0:
            squash += 0.12 * math.cos(clamp(self.jump_t / 0.55, 0, 1) * math.pi * 2)

        if not self.compact:  # soft shadow
            k = clamp(1 + oy / (R * 2), 0.3, 1)
            p.save()
            p.translate(x, y + R * 1.35)
            p.scale(1, 0.28)
            ellipse(p, 0, 0, R * 1.1, R * 1.1, rad(0, 0, R * 1.1, [(0, C(0.05, 0.25, 0.55, 0.22 * k)),
                                                                     (1, C(0.05, 0.25, 0.55, 0))]))
            p.restore()

        p.save()
        p.translate(x, y + oy)
        p.rotate(math.degrees(tilt))
        p.translate(0, R)
        p.scale(1 + squash * 0.7, 1 - squash)
        p.translate(0, -R)

        ellipse(p, 0, -0.3 * R, R * 2.2, R * 2.2,
                rad(0, -0.3 * R, R * 2.2, [(0, C(0.45, 0.80, 1.0, 0.30)), (0.27, C(0.45, 0.80, 1.0, 0.30)),
                                           (1, C(0.45, 0.80, 1.0, 0.0))]))
        body = drop_path(R)
        fill(p, body, lin(-R, -1.75 * R, R, R, [(0, C(0.66, 0.92, 1.0)), (0.5, C(0.26, 0.66, 0.98)),
                                                (1, C(0.07, 0.37, 0.84))]))
        fill(p, body, rad(0.35 * R, 0.55 * R, R * 1.1, [(0, C(0.02, 0.18, 0.55, 0.35)),
                                                          (1, C(0.02, 0.18, 0.55, 0.0))]))
        stroke(p, body, C(0.04, 0.28, 0.70, 0.45), 2 * u)

        p.save()
        p.translate(-0.45 * R, -0.35 * R)
        p.rotate(math.degrees(0.35))
        ellipse(p, 0, 0, 0.17 * R, 0.40 * R, C(1, 1, 1, 0.55))
        p.restore()
        ellipse(p, -0.22 * R, -1.0 * R, 0.08 * R, 0.08 * R, C(1, 1, 1, 0.75))

        self._draw_face(p)
        self._draw_arms(p)
        p.restore()

    def _draw_arms(self, p):
        R, t, u = self.R, self.t, self.u
        arm = C(0.16, 0.52, 0.93)
        pen = QPen(arm, 0.17 * R, Qt.SolidLine, Qt.RoundCap)
        # left arm: waving hello / cheering
        if self.state == "celebrate":
            ang = -2.1 + math.sin(t * 14) * 0.4
        elif self.state in ("enter", "idle"):
            ang = -2.5 + math.sin(t * 6) * 0.5
        else:
            ang = 2.6
        sx, sy = -0.8 * R, 0.2 * R
        ex, ey = sx + math.cos(ang) * 0.6 * R, sy + math.sin(ang) * 0.6 * R
        p.setPen(pen)
        p.drawLine(QPointF(sx, sy), QPointF(ex, ey))
        ellipse(p, ex, ey, 0.13 * R, 0.13 * R, arm)
        # right arm holding the glass
        gw, gh = 0.62 * R, 0.82 * R
        gx = 1.05 * R
        lift = -0.45 * R * min(1, self.state_t * 3) if self.state == "celebrate" else 0.0
        gy = -0.3 * R + lift + math.sin(t * 2.4 + 1) * 2 * u
        p.setPen(pen)
        p.drawLine(QPointF(0.88 * R, 0.15 * R), QPointF(gx + 0.05 * R, gy + gh * 0.6))
        draw_glass(p, gx, gy, gw, gh, self.glass_level, t, tilt=math.sin(t * 2.4) * 0.3)
        ellipse(p, gx + 0.05 * R, gy + gh * 0.6, 0.13 * R, 0.13 * R, arm)

    def _draw_face(self, p):
        R = self.R
        dark = C(0.06, 0.12, 0.28)
        ex, ey = 0.36 * R, -0.05 * R
        for sx in (-1, 1):
            ellipse(p, sx * 0.62 * R, 0.24 * R, 0.15 * R, 0.085 * R, C(1.0, 0.45, 0.62, 0.55))

        if self.face == "happy":
            for sx in (-1, 1):
                stroke(p, arc_path(sx * ex, ey + 0.06 * R, 0.14 * R, math.pi * 1.1, math.pi * 1.9), dark, 0.075 * R)
            mouth = QPainterPath()
            mouth.moveTo(-0.24 * R, 0.2 * R)
            mouth.cubicTo(-0.2 * R, 0.55 * R, 0.2 * R, 0.55 * R, 0.24 * R, 0.2 * R)
            mouth.closeSubpath()
            fill(p, mouth, dark)
            p.save()
            p.setClipPath(mouth, Qt.IntersectClip)
            ellipse(p, 0, 0.5 * R, 0.16 * R, 0.16 * R, C(1.0, 0.48, 0.58))
            p.restore()
            return

        if self.face == "sleepy":
            for sx in (-1, 1):
                stroke(p, arc_path(sx * ex, ey - 0.02 * R, 0.13 * R, math.pi * 0.15, math.pi * 0.85), dark, 0.07 * R)
            ellipse(p, 0, 0.3 * R, 0.06 * R, 0.06 * R, dark)
            return

        blink = 1.0
        if self.blink_t >= 0:
            blink = max(0.08, abs(math.cos(self.blink_t / 0.16 * math.pi)))
        lookx = looky = 0.0
        if self.pointer:
            dx = self.pointer[0] - self.pos[0]
            dy = self.pointer[1] - (self.pos[1] + self.body_offset())
            d = math.hypot(dx, dy) or 1
            k = min(1.0, d / (R * 4))
            lookx, looky = dx / d * k, dy / d * k
        else:
            lookx = math.sin(self.t * 0.7) * 0.6
        for sx in (-1, 1):
            p.save()
            p.translate(sx * ex, ey)
            p.scale(1, blink)
            ellipse(p, 0, 0, 0.2 * R, 0.25 * R, C(1, 1, 1))
            px, py = lookx * 0.07 * R, looky * 0.08 * R
            ellipse(p, px, py + 0.02 * R, 0.13 * R, 0.13 * R, dark)
            ellipse(p, px - 0.045 * R, py - 0.03 * R, 0.045 * R, 0.045 * R, C(1, 1, 1))
            p.restore()
        stroke(p, arc_path(0, 0.16 * R, 0.17 * R, math.pi * 0.18, math.pi * 0.82), dark, 0.065 * R)

    def _draw_bubble(self, p):
        if self.bubble_vis <= 0.001:
            return
        u, R, pad = self.u, self.R, self.pad
        W = self.W
        x, y = self.pos
        bw = self.bw
        inner = bw - 2 * pad
        celebrate = self.state == "celebrate" or self.result == "drank"

        title, sub = self._texts(celebrate)
        f_title, f_sub = make_font(19 * u, True), make_font(13.5 * u)
        th = text_height(f_title, title, inner - 84 * u)
        sh = text_height(f_sub, sub, inner)
        btn_h = 0 if celebrate else 42 * u
        bh = self._bh(th, sh, celebrate)

        bx = clamp(x - bw / 2, 14 * u, W - bw - 14 * u)
        drop_top = y + self.body_offset() - 1.75 * R
        by = max(8 * u, drop_top - self.gap - bh)
        self.bubble_rect = (bx, by, bw, bh)

        tail_x = clamp(x, bx + 28 * u, bx + bw - 28 * u)
        tail_y = by + bh
        vis = self.bubble_vis
        sc = max(0.01, ease_out_back(vis) if self.state in ("idle", "celebrate") else vis)

        p.save()
        p.setOpacity(clamp(vis * 1.5, 0, 1))
        p.translate(tail_x, tail_y)
        p.scale(sc, sc)
        p.translate(-tail_x, -tail_y)

        for i in range(5):  # soft shadow
            fill(p, rounded(bx - i * 2, by + 8 * u - i * 2, bw + i * 4, bh + i * 4, 20 * u + i * 2),
                 C(0.04, 0.16, 0.36, 0.045))
        bottom_col = C(0.94, 0.97, 1.0)
        box = rounded(bx, by, bw, bh, 20 * u)
        fill(p, box, lin(0, by, 0, by + bh, [(0, C(1, 1, 1)), (1, bottom_col)]))
        stroke(p, box, C(0.55, 0.78, 1.0, 0.9), 1.5 * u)
        tip = QPointF(tail_x + (x - tail_x) * 0.3, tail_y + 17 * u)
        tail = QPainterPath()
        tail.addPolygon(QPolygonF([QPointF(tail_x - 13 * u, tail_y - 2 * u), QPointF(tail_x + 13 * u, tail_y - 2 * u),
                                   tip]))
        tail.closeSubpath()
        fill(p, tail, bottom_col)
        edges = QPainterPath(QPointF(tail_x - 13 * u, tail_y))
        edges.lineTo(tip)
        edges.lineTo(QPointF(tail_x + 13 * u, tail_y))
        stroke(p, edges, C(0.55, 0.78, 1.0, 0.9), 1.5 * u)

        th_box = max(th, 26 * u)
        cy = by + pad + th_box / 2
        fill(p, drop_path(8 * u, bx + pad + 11 * u, cy + 4 * u),
             lin(bx + pad + 3 * u, cy - 10 * u, bx + pad + 19 * u, cy + 12 * u,
                 [(0, C(0.6, 0.9, 1.0)), (1, C(0.1, 0.45, 0.92))]))
        p.setFont(f_title)
        p.setPen(C(0.05, 0.18, 0.38))
        p.drawText(QRectF(bx + pad + 30 * u, cy - th / 2 - 1, inner - 84 * u, th + 4),
                   int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop), title)

        self.buttons = {}
        if not celebrate:  # close (skip) button
            ccx, ccy, cr = bx + bw - pad - 4 * u, by + pad + 6 * u, 12 * u
            hov = self.hover == "skip"
            if hov:
                ellipse(p, ccx, ccy, cr, cr, C(0.85, 0.91, 0.98))
            k = 4.5 * u
            xp = QPainterPath()
            xp.moveTo(ccx - k, ccy - k)
            xp.lineTo(ccx + k, ccy + k)
            xp.moveTo(ccx + k, ccy - k)
            xp.lineTo(ccx - k, ccy + k)
            stroke(p, xp, C(0.35, 0.45, 0.58, 1 if hov else 0.7), 2 * u)
            self.buttons["skip"] = (ccx - cr - 4 * u, ccy - cr - 4 * u, 2 * cr + 8 * u, 2 * cr + 8 * u)
            # settings (gear) button
            gx0, hov = ccx - 28 * u, self.hover == "settings"
            if hov:
                ellipse(p, gx0, ccy, cr, cr, C(0.85, 0.91, 0.98))
            col = C(0.35, 0.45, 0.58, 1 if hov else 0.7)
            teeth = QPainterPath()
            for i in range(8):
                a = i * math.pi / 4 + (self.t * 0.8 if hov else 0)
                teeth.moveTo(gx0 + math.cos(a) * 4.5 * u, ccy + math.sin(a) * 4.5 * u)
                teeth.lineTo(gx0 + math.cos(a) * 7.5 * u, ccy + math.sin(a) * 7.5 * u)
            stroke(p, teeth, col, 2.4 * u)
            ring = QPainterPath()
            ring.addEllipse(QPointF(gx0, ccy), 5 * u, 5 * u)
            stroke(p, ring, col, 2 * u)
            self.buttons["settings"] = (gx0 - cr - 2 * u, ccy - cr - 4 * u, 2 * cr + 4 * u, 2 * cr + 8 * u)

        yy = by + pad + th_box + 6 * u
        p.setFont(f_sub)
        p.setPen(C(0.33, 0.42, 0.53))
        p.drawText(QRectF(bx + pad, yy, inner, sh + 4), int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop), sub)
        yy += sh + 14 * u

        # progress: little glasses
        gw, gh = 14 * u, 20 * u
        n = min(self.goal, 12)
        for i in range(n):
            gx = bx + pad + i * (gw + 6 * u)
            filled = i < self.count
            gp = glass_path(gx, yy + 2 * u, gw, gh)
            fill(p, gp, C(0.88, 0.94, 1.0))
            if filled:
                pop = 1.0
                if self.state == "celebrate" and i == self.count - 1:
                    pop = clamp(self.state_t / 0.6, 0, 1)
                top = yy + 2 * u + gh * 0.22 + (1 - pop) * gh
                p.save()
                p.setClipPath(gp)
                p.fillRect(QRectF(gx, top, gw, gh), lin(0, top, 0, yy + 2 * u + gh,
                                                          [(0, C(0.45, 0.82, 1.0)), (1, C(0.12, 0.48, 0.95))]))
                p.restore()
            stroke(p, gp, C(0.30, 0.58, 0.92, 1 if filled else 0.55), 1.5 * u)
        vol = self.count * self.ml
        vol_s = f"{vol} ml" if vol < 1000 else f"{vol / 1000:.2f}".rstrip("0").rstrip(".") + " L"
        extra = f"  +{self.count - self.goal}" if self.count > self.goal else ""
        f_prog = make_font(12.5 * u, True)
        p.setFont(f_prog)
        p.setPen(C(0.18, 0.45, 0.82))
        p.drawText(QRectF(bx + pad, yy + 2 * u, inner, gh), int(Qt.AlignRight | Qt.AlignVCenter),
                   f"{self.count}/{self.goal}{extra}  ·  {vol_s}")
        yy += 24 * u + 14 * u

        if btn_h:
            prim_w = inner * 0.58
            self._button(p, "drank", bx + pad, yy, prim_w, btn_h, "I drank a glass", True)
            self._button(p, "snooze", bx + pad + prim_w + 10 * u, yy, inner - prim_w - 10 * u, btn_h,
                         f"Snooze {self.snooze} min", False)
        p.restore()

    def _button(self, p, bid, x, y, w, h, label, primary):
        u = self.u
        hov = self.hover == bid
        lift = -1.5 * u if hov else 0
        self.buttons[bid] = (x, y, w, h)
        y += lift
        if primary:
            fill(p, rounded(x, y + 3 * u, w, h, h / 2), C(0.1, 0.4, 0.85, 0.25))
            stops = ([(0, C(0.36, 0.78, 1.0)), (1, C(0.16, 0.52, 1.0))] if hov else
                     [(0, C(0.28, 0.70, 1.0)), (1, C(0.10, 0.44, 0.94))])
            fill(p, rounded(x, y, w, h, h / 2), lin(x, y, x + w, y + h, stops))
            fg = C(1, 1, 1)
        else:
            box = rounded(x, y, w, h, h / 2)
            fill(p, box, C(0.84, 0.92, 1.0) if hov else C(0.90, 0.95, 1.0))
            stroke(p, box, C(0.55, 0.75, 0.98), 1.2 * u)
            fg = C(0.12, 0.40, 0.80)
        p.setFont(make_font((15 if primary else 13.5) * u, primary))
        p.setPen(fg)
        p.drawText(QRectF(x, y, w, h), int(Qt.AlignCenter), label)


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def _overlay_flags(click_through):
    flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
    if IS_LINUX:
        flags |= Qt.X11BypassWindowManagerHint
    if click_through:
        flags |= Qt.WindowTransparentForInput
    return flags


class MascotWindow(QWidget):
    def __init__(self, overlay):
        super().__init__(None, _overlay_flags(False))
        self.overlay = overlay
        self.scene = overlay.scene
        if not self.scene.compact:
            self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if IS_MAC:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setWindowTitle(APP_NAME)
        self.setMouseTracking(True)
        self.resize(self.scene.win_w, self.scene.win_h)
        self.origin = (0, 0)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        ox, oy = self.origin
        p.translate(-ox, -oy)
        self.scene.draw_mascot(p, (ox, oy, self.width(), self.height()))
        p.end()

    def _scene_pos(self, ev):
        pos = ev.position()
        return pos.x() + self.origin[0], pos.y() + self.origin[1]

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.overlay.click(*self._scene_pos(ev))


class FxWindow(QWidget):
    def __init__(self, overlay):
        super().__init__(None, _overlay_flags(True))
        self.scene = overlay.scene
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        if IS_MAC:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setWindowTitle(APP_NAME + " wave")

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(0, -(self.scene.H - self.scene.fx_h))
        self.scene.draw_fx(p)
        p.end()


class Overlay:
    """One reminder appearance: creates the windows and drives the animation."""

    def __init__(self, cfg, count, on_done, on_settings=None, intro=None):
        self.on_done = on_done
        self.on_settings = on_settings
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.avail = screen.availableGeometry()
        self.x11 = is_x11()
        compact = self.x11 and not _X11.has_compositor()
        self.scene = Scene(self.avail.width(), self.avail.height(), cfg, count, user_first_name(cfg), compact,
                           intro=intro)
        self.fx = None
        if not compact:
            self.fx = FxWindow(self)
            self.fx.setGeometry(self.avail.x(), self.avail.y() + self.avail.height() - self.scene.fx_h,
                                self.avail.width(), self.scene.fx_h)
            self.fx.show()
        self.mascot = MascotWindow(self)
        if compact:
            self.mascot.move(self.avail.right() - self.scene.win_w - 16,
                             self.avail.bottom() - self.scene.win_h - 16)
        else:
            self._place()
        self.mascot.show()
        self.mascot.raise_()
        self._cursor_hand = False
        self._frame = 0
        self._fx_acc = 0.0
        self._finished = False
        self.clock = QElapsedTimer()
        self.clock.start()
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def _place(self):
        ox, oy = self.scene.origin()
        ox, oy = math.floor(ox), math.floor(oy)
        self.mascot.origin = (ox, oy)
        self.mascot.move(self.avail.x() + ox, self.avail.y() + oy)

    def click(self, sx, sy):
        h = self.scene.hit(sx, sy)
        if h == "drop":
            self.scene.poke()
        elif h == "settings" and self.on_settings:
            self.scene.leave("skip")  # step aside so the window isn't covered
            self.on_settings()
        elif h in ("drank", "snooze", "skip"):
            if h == "drank" and self.scene.cfg.get("sound", True):
                play_sound("done")
            self.scene.leave(h)

    def _tick(self):
        dt = min(0.05, self.clock.restart() / 1000.0)
        s = self.scene
        gp = QCursor.pos()
        if self.scene.compact:
            wp = self.mascot.pos()
            px, py = gp.x() - wp.x(), gp.y() - wp.y()
        else:
            px, py = gp.x() - self.avail.x(), gp.y() - self.avail.y()
        s.pointer = (px, py)
        s.hover = s.hit(px, py)
        want_hand = s.hover is not None
        if want_hand != self._cursor_hand:
            self.mascot.setCursor(Qt.PointingHandCursor if want_hand else Qt.ArrowCursor)
            self._cursor_hand = want_hand

        s.update(dt)
        if s.done:
            self._finish()
            return
        if not s.compact:
            self._place()
            self._frame += 1
            if self.x11 and self._frame % 2 == 0:
                self._update_input_shape()
            if self._frame % 120 == 0:
                self.mascot.raise_()
        # Adaptive frame rate: 60 fps for fast motion, 30 fps while gently roaming.
        s_ = self.scene
        fast = (s_.state in ("enter", "exit", "celebrate") or s_.jump_t >= 0
                or any(q.kind in ("drop", "spark") for q in s_.particles))
        want = 16 if fast else 33
        if self.timer.interval() != want:
            self.timer.setInterval(want)
        self.mascot.update()
        # The wave moves slowly, so the wide wave strip only repaints at ~20 fps.
        if self.fx:
            self._fx_acc += dt
            if self._fx_acc >= 0.048:
                self._fx_acc = 0.0
                self.fx.update()

    def _update_input_shape(self):
        ox, oy = self.mascot.origin
        dpr = self.mascot.devicePixelRatioF()
        rects = [((x - ox) * dpr, (y - oy) * dpr, w * dpr, h * dpr)
                 for (x, y, w, h) in self.scene.hit_rects(pad=4)]
        try:
            _X11.set_input_rects(self.mascot.winId(), rects)
        except Exception:
            pass

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        self.timer.stop()
        result = self.scene.result or "timeout"
        self.mascot.close()
        self.mascot.deleteLater()
        if self.fx:
            self.fx.close()
            self.fx.deleteLater()
        self.on_done(result)


# --------------------------------------------------------------------------- #
# Icons
# --------------------------------------------------------------------------- #
def drop_icon(size=64):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    R = size * 0.30
    cx, cy = size / 2, size * 0.62
    fill(p, drop_path(R, cx, cy), lin(cx - R, cy - 1.75 * R, cx + R, cy + R,
                                      [(0, C(0.66, 0.92, 1.0)), (0.5, C(0.26, 0.66, 0.98)), (1, C(0.07, 0.37, 0.84))]))
    ellipse(p, cx - 0.4 * R, cy - 0.35 * R, 0.16 * R, 0.36 * R, C(1, 1, 1, 0.6))
    p.end()
    return QIcon(pm)


def render_app_icon(size=1024):
    """The app icon (used for the .exe/.app): the mascot on a soft rounded tile."""
    from PySide6.QtGui import QImage
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    m = size * 0.06
    fill(p, rounded(m, m, size - 2 * m, size - 2 * m, size * 0.22),
         lin(0, 0, size, size, [(0, C(0.90, 0.97, 1.0)), (1, C(0.70, 0.86, 1.0))]))
    R = size * 0.27
    cx, cy = size / 2, size * 0.60
    body = drop_path(R, cx, cy)
    fill(p, body, lin(cx - R, cy - 1.75 * R, cx + R, cy + R,
                      [(0, C(0.66, 0.92, 1.0)), (0.5, C(0.26, 0.66, 0.98)), (1, C(0.07, 0.37, 0.84))]))
    p.save()
    p.translate(cx - 0.45 * R, cy - 0.35 * R)
    p.rotate(20)
    ellipse(p, 0, 0, 0.17 * R, 0.40 * R, C(1, 1, 1, 0.55))
    p.restore()
    dark = C(0.06, 0.12, 0.28)
    for sx in (-1, 1):
        ellipse(p, cx + sx * 0.62 * R, cy + 0.24 * R, 0.15 * R, 0.085 * R, C(1.0, 0.45, 0.62, 0.55))
        ellipse(p, cx + sx * 0.36 * R, cy - 0.05 * R, 0.2 * R, 0.25 * R, C(1, 1, 1))
        ellipse(p, cx + sx * 0.36 * R, cy - 0.03 * R, 0.13 * R, 0.13 * R, dark)
        ellipse(p, cx + sx * 0.36 * R - 0.045 * R, cy - 0.08 * R, 0.045 * R, 0.045 * R, C(1, 1, 1))
    stroke(p, arc_path(cx, cy + 0.16 * R, 0.17 * R, math.pi * 0.18, math.pi * 0.82), dark, 0.065 * R)
    p.end()
    return img


# --------------------------------------------------------------------------- #
# Start at login + menu shortcuts (per OS)
# --------------------------------------------------------------------------- #
MAC_PLIST = _home("Library", "LaunchAgents", "com.waterreminder.agent.plist")
LINUX_AUTOSTART = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _home(".config"), "autostart",
                               "water-reminder.desktop")
_LINUX_APPS = os.path.join(os.environ.get("XDG_DATA_HOME") or _home(".local", "share"), "applications")
LINUX_LAUNCHER = os.path.join(_LINUX_APPS, "water-reminder.desktop")
LINUX_SETTINGS = os.path.join(_LINUX_APPS, "water-reminder-settings.desktop")
LINUX_STATS = os.path.join(_LINUX_APPS, "water-reminder-stats.desktop")
LINUX_BIN = _home(".local", "bin", "water-reminder")
LINUX_ICON = os.path.join(DATA_DIR, "icon.png")
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_PROGRAMS = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
WIN_SHORTCUTS = [("Water Reminder.lnk", "--now", "Show the water reminder now"),
                 ("Water Reminder Settings.lnk", "--settings", "Water Reminder settings"),
                 ("Water Reminder Stats.lnk", "--stats", "Your hydration history")]


def _gui_python():
    py = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
    return py


def launch_command():
    """The command that starts this app (packaged executable, or python + script)."""
    if IS_FROZEN:
        return [sys.executable]
    installed = os.path.join(APP_DIR, "water_reminder.py")
    script = installed if os.path.exists(installed) else os.path.abspath(__file__)
    return [_gui_python(), script]


def _desktop_exec(cmd):
    return " ".join(f'"{c}"' if " " in c else c for c in cmd)


def autostart_is_enabled():
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as key:
                winreg.QueryValueEx(key, "WaterReminder")
            return True
        except OSError:
            return False
    return os.path.exists(MAC_PLIST if IS_MAC else LINUX_AUTOSTART)


def autostart_enable(cmd=None):
    cmd = cmd or launch_command()
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "WaterReminder", 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
    elif IS_MAC:
        import plistlib
        os.makedirs(os.path.dirname(MAC_PLIST), exist_ok=True)
        with open(MAC_PLIST, "wb") as f:
            plistlib.dump({"Label": "com.waterreminder.agent", "ProgramArguments": list(cmd),
                           "RunAtLoad": True, "LimitLoadToSessionType": "Aqua",
                           "StandardOutPath": LOG_FILE, "StandardErrorPath": LOG_FILE}, f)
    else:
        os.makedirs(os.path.dirname(LINUX_AUTOSTART), exist_ok=True)
        with open(LINUX_AUTOSTART, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Water Reminder\n"
                    f"Comment=Animated reminder to drink water\nExec={_desktop_exec(cmd)}\n"
                    f"Icon={LINUX_ICON}\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=20\n")


def autostart_disable():
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "WaterReminder")
        except OSError:
            pass
    else:
        try:
            os.remove(MAC_PLIST if IS_MAC else LINUX_AUTOSTART)
        except OSError:
            pass


def create_shortcuts(cmd=None):
    """Start-menu (Windows) / app-menu (Linux) entries. macOS uses the menu-bar icon."""
    cmd = cmd or launch_command()
    if IS_WIN:
        def q(v):
            return v.replace("'", "''")
        target, args = cmd[0], subprocess.list2cmdline(cmd[1:])
        ps = "$w=New-Object -ComObject WScript.Shell;"
        for name, flag, desc in WIN_SHORTCUTS:
            lnk = os.path.join(_WIN_PROGRAMS, name)
            ps += (f"$s=$w.CreateShortcut('{q(lnk)}');$s.TargetPath='{q(target)}';"
                   f"$s.Arguments='{q((args + ' ' + flag).strip())}';$s.Description='{q(desc)}';$s.Save();")
        _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
    elif IS_LINUX:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            render_app_icon(256).save(LINUX_ICON)
        except Exception:
            pass
        os.makedirs(_LINUX_APPS, exist_ok=True)
        os.makedirs(os.path.dirname(LINUX_BIN), exist_ok=True)
        with open(LINUX_BIN, "w") as f:
            f.write("#!/bin/sh\nexec " + " ".join(f'"{c}"' for c in cmd) + ' "$@"\n')
        os.chmod(LINUX_BIN, 0o755)
        for path, name, flag, comment in (
                (LINUX_LAUNCHER, "Water Reminder", "--now", "Show the water reminder now"),
                (LINUX_SETTINGS, "Water Reminder Settings", "--settings", "Change how often it reminds you"),
                (LINUX_STATS, "Water Reminder Stats", "--stats", "Your hydration history and streaks")):
            with open(path, "w") as f:
                f.write(f"[Desktop Entry]\nType=Application\nName={name}\nComment={comment}\n"
                        f"Exec={_desktop_exec(cmd)} {flag}\nIcon={LINUX_ICON}\nCategories=Utility;\n"
                        "Keywords=water;drink;hydration;reminder;\n")


def remove_shortcuts():
    paths = ([os.path.join(_WIN_PROGRAMS, n) for n, _, _ in WIN_SHORTCUTS] if IS_WIN else
             [LINUX_LAUNCHER, LINUX_SETTINGS, LINUX_STATS, LINUX_BIN] if IS_LINUX else [])
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def spawn_detached(cmd):
    if IS_WIN:
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, close_fds=True)
    else:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as log:
            subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


# --------------------------------------------------------------------------- #
# Shared window styling
# --------------------------------------------------------------------------- #
APP_QSS = """
QDialog { background: #f2f7ff; }
QLabel { color: #1b2b44; font-size: 13px; }
QLabel#title { font-size: 21px; font-weight: 700; color: #0d2e61; }
QLabel#subtitle { color: #5b6b80; font-size: 12px; }
QLabel#cardTitle { font-size: 11px; font-weight: 700; color: #2a6fd6; letter-spacing: 1px; }
QLabel#hint { color: #6b7a8f; font-size: 11px; }
QLabel#kpi { font-size: 24px; font-weight: 700; color: #0d2e61; }
QLabel#kpiLabel { color: #5b6b80; font-size: 11px; }
QLabel#badgeIcon { font-size: 24px; }
QLabel#badgeTitle { font-size: 12px; font-weight: 700; color: #0d2e61; }
QLabel#badgeDesc { font-size: 11px; color: #6b7a8f; }
QFrame#card { background: #ffffff; border: 1px solid #d6e6fb; border-radius: 12px; }
QFrame#tile { background: #ffffff; border: 1px solid #d6e6fb; border-radius: 12px; }
QFrame#badge { background: #f7fbff; border: 1px solid #e1ecfa; border-radius: 10px; }
QSpinBox, QTimeEdit, QLineEdit {
    background: #f7fbff; border: 1px solid #c9dcf5; border-radius: 8px;
    padding: 4px 8px; min-height: 24px; color: #13243d; font-size: 13px;
    selection-background-color: #3b8cf0;
}
QSpinBox::up-button, QSpinBox::down-button, QTimeEdit::up-button, QTimeEdit::down-button {
    width: 0px; height: 0px; border: none; subcontrol-origin: border;
}
QSpinBox:focus, QTimeEdit:focus, QLineEdit:focus { border: 1px solid #3b8cf0; background: #ffffff; }
QSpinBox:disabled, QTimeEdit:disabled { color: #9aa8ba; background: #f0f3f7; }
QCheckBox { color: #1b2b44; font-size: 13px; spacing: 8px; }
QPushButton {
    background: #e8f2ff; border: 1px solid #c2d9f7; border-radius: 9px;
    padding: 7px 16px; color: #1d5fc2; font-size: 13px;
}
QPushButton:hover { background: #dcebff; }
QPushButton:disabled { color: #9aa8ba; background: #f0f3f7; border-color: #dde5ef; }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #48b0ff, stop:1 #1a70f0);
    color: white; border: none; font-weight: 600; padding: 8px 22px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5cbcff, stop:1 #2a80ff);
}
QPushButton#flat { background: transparent; border: none; color: #5b6b80; padding: 7px 4px; }
QPushButton#flat:hover { color: #1d5fc2; }
QPushButton#step {
    background: #eef5ff; border: 1px solid #c9dcf5; border-radius: 8px; padding: 0;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
    font-size: 17px; font-weight: 600; color: #2a6fd6;
}
QPushButton#step:hover { background: #dcebff; }
QPushButton#step:pressed { background: #c9e0ff; }
QPushButton#seg { padding: 5px 12px; border-radius: 8px; font-size: 12px; }
QPushButton#seg:checked { background: #1a70f0; color: white; border-color: #1a70f0; }
QToolTip { background: #0d2e61; color: white; border: none; padding: 6px 8px; border-radius: 6px; }
"""


def _header(title, subtitle):
    head = QHBoxLayout()
    icon = QLabel()
    icon.setPixmap(drop_icon(96).pixmap(48, 48))
    head.addWidget(icon)
    titles = QVBoxLayout()
    titles.setSpacing(0)
    titles.addWidget(QLabel(title, objectName="title"))
    titles.addWidget(QLabel(subtitle, objectName="subtitle"))
    head.addLayout(titles, 1)
    return head


def _card(parent_layout, title):
    frame = QFrame(objectName="card")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(16, 12, 16, 14)
    outer.setSpacing(8)
    outer.addWidget(QLabel(title.upper(), objectName="cardTitle"))
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.setHorizontalSpacing(18)
    form.setVerticalSpacing(8)
    outer.addLayout(form)
    parent_layout.addWidget(frame)
    return form, outer


def _bring_to_front(w):
    w.show()
    w.raise_()
    w.activateWindow()


# --------------------------------------------------------------------------- #
# Settings window
# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    """A friendly settings window; writes config.json and tells the service."""

    def __init__(self, on_saved=None, on_preview=None, on_stats=None, on_quit=None, on_check_updates=None):
        super().__init__(None, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.on_saved = on_saved
        self.on_quit = on_quit
        self.setWindowTitle(f"{APP_NAME} – Settings")
        self.setWindowIcon(drop_icon())
        self.setStyleSheet(APP_QSS)
        self.setAttribute(Qt.WA_DeleteOnClose)
        cfg = load_config()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        root.addLayout(_header("Settings", "Make your hydration buddy fit your day."))
        cols = QHBoxLayout()
        cols.setSpacing(12)
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(12)
        right.setSpacing(12)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        root.addLayout(cols)

        def spin(lo, hi, step, suffix, value):
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setSingleStep(step)
            sb.setSuffix(suffix)
            sb.setValue(int(clamp(int(value), lo, hi)))
            sb.setButtonSymbols(QAbstractSpinBox.NoButtons)
            sb.setAlignment(Qt.AlignCenter)
            sb.setFixedWidth(110)
            return sb

        def stepper(sb):
            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            minus, plus = QPushButton("−", objectName="step"), QPushButton("+", objectName="step")
            for b, fn in ((minus, sb.stepDown), (plus, sb.stepUp)):
                b.setAutoRepeat(True)
                b.clicked.connect(fn)
            lay.addWidget(minus)
            lay.addWidget(sb)
            lay.addWidget(plus)
            lay.addStretch(1)
            return box

        def label(text):
            lb = QLabel(text)
            lb.setMinimumWidth(140)
            lb.setMinimumHeight(32)
            lb.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            return lb

        # --- left column
        f, _ = _card(left, "Reminders")
        self.interval = spin(5, 240, 5, " min", cfg["interval_minutes"])
        self.snooze = spin(1, 120, 1, " min", cfg["snooze_minutes"])
        self.autohide = spin(1, 30, 1, " min", cfg["auto_hide_minutes"])
        f.addRow(label("Remind me every"), stepper(self.interval))
        f.addRow(label("Snooze for"), stepper(self.snooze))
        f.addRow(label("Hide if ignored after"), stepper(self.autohide))

        f, outer = _card(left, "Daily goal")
        self.goal = spin(1, 24, 1, " glasses", cfg["daily_goal_glasses"])
        self.glass = spin(50, 1000, 50, " ml", cfg["glass_ml"])
        f.addRow(label("Glasses per day"), stepper(self.goal))
        f.addRow(label("Glass size"), stepper(self.glass))
        self.total = QLabel(objectName="hint")
        outer.addWidget(self.total)
        self.goal.valueChanged.connect(self._update_total)
        self.glass.valueChanged.connect(self._update_total)
        self._update_total()
        left.addStretch(1)

        # --- right column
        f, _ = _card(right, "Focus & quiet time")
        q = cfg.get("quiet_hours") or {}
        self.quiet = QCheckBox("Quiet hours at night")
        self.quiet.setChecked(bool(q.get("enabled", True)))
        self.q_start = QTimeEdit(QTime.fromString(q.get("start", "22:00"), "HH:mm"))
        self.q_end = QTimeEdit(QTime.fromString(q.get("end", "08:00"), "HH:mm"))
        for te in (self.q_start, self.q_end):
            te.setDisplayFormat("HH:mm")
            te.setButtonSymbols(QAbstractSpinBox.NoButtons)
            te.setAlignment(Qt.AlignCenter)
            te.setFixedWidth(76)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.q_start)
        row.addWidget(QLabel("to"))
        row.addWidget(self.q_end)
        row.addStretch(1)
        f.addRow(self.quiet)
        f.addRow(label("No reminders from"), row)
        self.quiet.toggled.connect(self._update_enabled)
        self.focus = QCheckBox("Don't interrupt full-screen apps,\npresentations, calls or Do Not Disturb")
        self.focus.setChecked(bool(cfg.get("respect_focus", True)))
        f.addRow(self.focus)
        self.away = QCheckBox("Pause while I'm away from the computer")
        self.away.setChecked(bool(cfg.get("pause_when_away", True)))
        self.away_min = spin(2, 60, 1, " min", cfg.get("away_minutes", 5))
        f.addRow(self.away)
        f.addRow(label("Away after no input for"), stepper(self.away_min))
        self.away.toggled.connect(self._update_enabled)
        self._update_enabled()

        f, _ = _card(right, "Personal")
        self.name = QLineEdit(cfg.get("name", ""))
        self.name.setPlaceholderText(user_first_name({}))
        self.sound = QCheckBox("Play a sound")
        self.sound.setChecked(bool(cfg.get("sound", True)))
        test = QPushButton("▶ Test")
        test.clicked.connect(lambda: play_sound("reminder"))
        srow = QHBoxLayout()
        srow.addWidget(self.sound)
        srow.addStretch(1)
        srow.addWidget(test)
        self.login = QCheckBox("Start automatically when I log in")
        self._login_was = autostart_is_enabled()
        self.login.setChecked(self._login_was)
        self.updates = QCheckBox("Check for updates automatically")
        self.updates.setChecked(bool(cfg.get("check_updates", True)))
        f.addRow(label("Your name"), self.name)
        f.addRow(srow)
        f.addRow(self.login)
        f.addRow(self.updates)
        right.addStretch(1)

        # --- footer
        btns = QHBoxLayout()
        reset = QPushButton("Reset to defaults", objectName="flat")
        reset.clicked.connect(self._reset)
        btns.addWidget(reset)
        if on_quit:
            unin = QPushButton("Uninstall…", objectName="flat")
            unin.clicked.connect(self._uninstall)
            btns.addWidget(unin)
        ver = QLabel(f"v{APP_VERSION}", objectName="hint")
        btns.addWidget(ver)
        if on_check_updates:
            upd = QPushButton("Check for updates", objectName="flat")
            upd.clicked.connect(on_check_updates)
            btns.addWidget(upd)
        btns.addStretch(1)
        if on_stats:
            st = QPushButton("History && stats")
            st.clicked.connect(on_stats)
            btns.addWidget(st)
        if on_preview:
            prev = QPushButton("Preview")
            prev.clicked.connect(on_preview)
            btns.addWidget(prev)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save", objectName="primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        btns.addWidget(cancel)
        btns.addWidget(save)
        root.addSpacing(2)
        root.addLayout(btns)

    def _update_total(self):
        litres = self.goal.value() * self.glass.value() / 1000
        tip = "  ·  most adults need about 2–3 L a day" if litres < 1.5 else ""
        self.total.setText(f"= {litres:g} L per day{tip}")

    def _update_enabled(self):
        for w in (self.q_start, self.q_end):
            w.setEnabled(self.quiet.isChecked())
        self.away_min.setEnabled(self.away.isChecked())

    def _reset(self):
        d = DEFAULTS
        self.interval.setValue(d["interval_minutes"])
        self.snooze.setValue(d["snooze_minutes"])
        self.autohide.setValue(d["auto_hide_minutes"])
        self.goal.setValue(d["daily_goal_glasses"])
        self.glass.setValue(d["glass_ml"])
        self.quiet.setChecked(d["quiet_hours"]["enabled"])
        self.q_start.setTime(QTime.fromString(d["quiet_hours"]["start"], "HH:mm"))
        self.q_end.setTime(QTime.fromString(d["quiet_hours"]["end"], "HH:mm"))
        self.focus.setChecked(d["respect_focus"])
        self.away.setChecked(d["pause_when_away"])
        self.away_min.setValue(d["away_minutes"])
        self.name.setText("")
        self.sound.setChecked(d["sound"])
        self.login.setChecked(True)
        self.updates.setChecked(True)

    def values(self):
        cfg = load_config()  # keep any keys we don't show here
        cfg.update({
            "interval_minutes": self.interval.value(),
            "snooze_minutes": self.snooze.value(),
            "auto_hide_minutes": self.autohide.value(),
            "daily_goal_glasses": self.goal.value(),
            "glass_ml": self.glass.value(),
            "quiet_hours": {"enabled": self.quiet.isChecked(),
                            "start": self.q_start.time().toString("HH:mm"),
                            "end": self.q_end.time().toString("HH:mm")},
            "respect_focus": self.focus.isChecked(),
            "pause_when_away": self.away.isChecked(),
            "away_minutes": self.away_min.value(),
            "sound": self.sound.isChecked(),
            "name": self.name.text().strip(),
            "check_updates": self.updates.isChecked(),
        })
        return cfg

    def _save(self):
        cfg = self.values()
        cfg["autostart_configured"] = True
        save_config(cfg)
        if self.login.isChecked() != self._login_was:
            try:
                autostart_enable() if self.login.isChecked() else autostart_disable()
            except Exception as e:
                print(f"[{APP_ID}] could not change start-at-login: {e}", file=sys.stderr)
        if self.on_saved:
            self.on_saved(cfg)
        self.accept()

    def _uninstall(self):
        from PySide6.QtWidgets import QMessageBox
        msg = ("Remove Water Reminder from this computer?\n\n"
               "It will stop, and won't start at login any more. Your history is kept.")
        if IS_MAC and IS_FROZEN:
            msg += "\n\nAfterwards, drag Water Reminder from Applications to the Trash."
        if QMessageBox.question(self, "Uninstall Water Reminder", msg) != QMessageBox.Yes:
            return
        do_uninstall(purge=False, quiet=True)
        self.reject()
        if self.on_quit:
            self.on_quit()


# --------------------------------------------------------------------------- #
# History & stats window
# --------------------------------------------------------------------------- #
ACHIEVEMENTS = [
    ("💧", "First sip", "Log your first glass", lambda s: s["total"] >= 1),
    ("🎯", "Goal getter", "Reach your daily goal", lambda s: s["days_met"] >= 1),
    ("🌅", "Early bird", "A glass before 9:00", lambda s: s["early"]),
    ("⭐", "Overachiever", "Beat your goal by 2+", lambda s: s["overachiever"]),
    ("🔥", "On a roll", "3-day streak", lambda s: s["best"] >= 3),
    ("🚀", "Unstoppable", "7-day streak", lambda s: s["best"] >= 7),
    ("💯", "Century", "100 glasses in total", lambda s: s["total"] >= 100),
    ("🏆", "Hydration hero", "30-day streak", lambda s: s["best"] >= 30),
]


def compute_stats(st=None, goal=None):
    st = st or load_state()
    goal = goal or max(1, int(load_config()["daily_goal_glasses"]))
    hist = dict(st.get("history", {}))
    hist[st["date"]] = st["count"]
    today = datetime.date.today()
    one = datetime.timedelta(days=1)

    def count(d):
        return hist.get(d.isoformat(), 0)

    streak, d = 0, (today if count(today) >= goal else today - one)
    while count(d) >= goal:
        streak += 1
        d -= one
    best, run, prev = 0, 0, None
    for k in sorted(hist):
        day = datetime.date.fromisoformat(k)
        if hist[k] >= goal:
            run = run + 1 if prev is not None and day - prev == one else 1
            prev = day
            best = max(best, run)
        else:
            run, prev = 0, None
    best = max(best, streak)
    week = [count(today - i * one) for i in range(7)]
    times = st.get("times", {})
    total = st.get("total", sum(hist.values()))
    return {
        "today": st["count"], "goal": goal, "streak": streak, "best": best,
        "avg7": sum(week) / 7.0, "total": max(total, sum(hist.values())),
        "days_met": sum(1 for v in hist.values() if v >= goal),
        "early": any(t < "09:00" for ts in times.values() for t in ts),
        "overachiever": any(v >= goal + 2 for v in hist.values()),
        "hist": hist,
    }


class BarChart(QWidget):
    """Glasses per day vs. the goal line. One series: no legend; a small key explains the two shades."""
    MET = QColor("#1a70f0")
    BELOW = QColor("#9cc6f7")
    INK = QColor("#1b2b44")
    MUTED = QColor("#6b7a8f")
    GRID = QColor("#e3ecf7")

    def __init__(self):
        super().__init__()
        self.days = 7
        self.data = []
        self.goal = 8
        self.ml = 250
        self.hover = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(200)

    def set_data(self, hist, goal, ml, days):
        self.goal, self.ml, self.days = goal, ml, days
        today = datetime.date.today()
        self.data = [(today - datetime.timedelta(days=i), hist.get((today - datetime.timedelta(days=i)).isoformat(), 0))
                     for i in range(days - 1, -1, -1)]
        self.update()

    def _geom(self):
        left, right, top, bottom = 30, 56, 14, 26
        w, h = self.width() - left - right, self.height() - top - bottom
        ymax = max([self.goal + 1] + [c + 1 for _, c in self.data])
        ymax = int(math.ceil(ymax / 2.0) * 2)
        slot = w / max(1, len(self.data))
        return left, top, w, h, ymax, slot

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        left, top, w, h, ymax, slot = self._geom()
        base = top + h
        small = make_font(11)
        p.setFont(small)
        # recessive grid + y labels
        step = 2 if ymax <= 12 else 4
        for v in range(0, ymax + 1, step):
            y = base - h * v / ymax
            p.setPen(QPen(self.GRID, 1))
            p.drawLine(QPointF(left, y), QPointF(left + w, y))
            p.setPen(self.MUTED)
            p.drawText(QRectF(0, y - 8, left - 6, 16), int(Qt.AlignRight | Qt.AlignVCenter), str(v))
        # bars: thin, rounded data-end (top), square at the baseline
        bw = max(4.0, min(28.0, slot * 0.58))
        for i, (day, c) in enumerate(self.data):
            x = left + slot * i + (slot - bw) / 2
            col = QColor(self.MET if c >= self.goal else self.BELOW)
            if i == self.hover:
                col = col.darker(115)
            if c > 0:
                bh = h * c / ymax
                path = QPainterPath()
                r = min(4.0, bw / 2, bh)
                path.moveTo(x, base)
                path.lineTo(x, base - bh + r)
                path.quadTo(x, base - bh, x + r, base - bh)
                path.lineTo(x + bw - r, base - bh)
                path.quadTo(x + bw, base - bh, x + bw, base - bh + r)
                path.lineTo(x + bw, base)
                path.closeSubpath()
                fill(p, path, col)
            # x labels (thinned for 30 days)
            is_today = i == len(self.data) - 1
            if self.days <= 14 or day.day % 5 == 0 or is_today:
                lbl = "Today" if is_today else (day.strftime("%a")[:2] if self.days <= 7 else str(day.day))
                p.setPen(self.INK if is_today else self.MUTED)
                p.setFont(make_font(11, is_today))
                p.drawText(QRectF(left + slot * i - 10, base + 6, slot + 20, 16), int(Qt.AlignHCenter), lbl)
        # selective direct label: today's value
        if self.data:
            c = self.data[-1][1]
            x = left + slot * (len(self.data) - 1)
            y = base - h * c / ymax
            p.setFont(make_font(11, True))
            p.setPen(self.INK)
            p.drawText(QRectF(x - 10, y - 18, slot + 20, 16), int(Qt.AlignHCenter), str(c))
        # goal reference line (neutral ink, dashed) + label
        gy = base - h * self.goal / ymax
        pen = QPen(self.INK, 1.5, Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.drawLine(QPointF(left, gy), QPointF(left + w, gy))
        p.setFont(make_font(11, True))
        p.setPen(self.INK)
        p.drawText(QRectF(left + w + 6, gy - 8, 50, 16), int(Qt.AlignLeft | Qt.AlignVCenter), f"Goal {self.goal}")
        p.end()

    def _index_at(self, x):
        left, _, w, _, _, slot = self._geom()
        if x < left or x > left + w:
            return -1
        return min(len(self.data) - 1, int((x - left) // slot))

    def mouseMoveEvent(self, ev):
        from PySide6.QtWidgets import QToolTip
        i = self._index_at(ev.position().x())
        if i != self.hover:
            self.hover = i
            self.update()
        if i >= 0:
            day, c = self.data[i]
            status = "goal met ✓" if c >= self.goal else f"{self.goal - c} short of goal"
            QToolTip.showText(ev.globalPosition().toPoint(),
                              f"{day.strftime('%a %d %b')}\n{c} glasses · {c * self.ml / 1000:g} L\n{status}", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, _ev):
        self.hover = -1
        self.update()


class StatsDialog(QDialog):
    def __init__(self, on_log=None, on_undo=None):
        super().__init__(None, Qt.Dialog | Qt.WindowCloseButtonHint)
        self.on_log, self.on_undo = on_log, on_undo
        self.setWindowTitle(f"{APP_NAME} – History & stats")
        self.setWindowIcon(drop_icon())
        self.setStyleSheet(APP_QSS)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumWidth(780)
        self.range_days = 7

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        root.addLayout(_header("Your hydration", datetime.date.today().strftime("Today is %A, %d %B")))

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.kpis = {}
        for key, label in (("today", "today"), ("streak", "day streak 🔥"), ("best", "best streak"),
                           ("avg", "glasses/day, last 7 days")):
            frame = QFrame(objectName="tile")
            lay = QVBoxLayout(frame)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(0)
            val = QLabel("–", objectName="kpi")
            lay.addWidget(val)
            lay.addWidget(QLabel(label, objectName="kpiLabel"))
            self.kpis[key] = val
            tiles.addWidget(frame, 1)
        root.addLayout(tiles)

        chart_card = QFrame(objectName="card")
        cl = QVBoxLayout(chart_card)
        cl.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        top.addWidget(QLabel("GLASSES PER DAY", objectName="cardTitle"))
        top.addStretch(1)
        self.seg = []
        for d in (7, 14, 30):
            b = QPushButton(f"{d} days", objectName="seg")
            b.setCheckable(True)
            b.setChecked(d == 7)
            b.clicked.connect(lambda _c=False, d=d: self._set_range(d))
            top.addWidget(b)
            self.seg.append((d, b))
        cl.addLayout(top)
        self.chart = BarChart()
        cl.addWidget(self.chart)
        key = QLabel(objectName="hint")
        key.setTextFormat(Qt.RichText)
        key.setText('<span style="color:#1a70f0">■</span> goal met &nbsp;&nbsp; '
                    '<span style="color:#9cc6f7">■</span> below goal &nbsp;&nbsp; '
                    '<span style="color:#1b2b44">┄</span> daily goal &nbsp;&nbsp; · hover a bar for details')
        cl.addWidget(key)
        root.addWidget(chart_card)

        ach_card = QFrame(objectName="card")
        al = QVBoxLayout(ach_card)
        al.setContentsMargins(16, 12, 16, 14)
        self.ach_title = QLabel("ACHIEVEMENTS", objectName="cardTitle")
        al.addWidget(self.ach_title)
        from PySide6.QtWidgets import QGraphicsOpacityEffect, QGridLayout
        grid = QGridLayout()
        grid.setSpacing(8)
        self.badges = []
        for i, (emoji, title, desc, _test) in enumerate(ACHIEVEMENTS):
            b = QFrame(objectName="badge")
            bl = QHBoxLayout(b)
            bl.setContentsMargins(10, 8, 10, 8)
            ic = QLabel(emoji, objectName="badgeIcon")
            bl.addWidget(ic)
            tl = QVBoxLayout()
            tl.setSpacing(0)
            tl.addWidget(QLabel(title, objectName="badgeTitle"))
            dl = QLabel(desc, objectName="badgeDesc")
            dl.setWordWrap(True)
            tl.addWidget(dl)
            bl.addLayout(tl, 1)
            eff = QGraphicsOpacityEffect(b)
            b.setGraphicsEffect(eff)
            grid.addWidget(b, i // 4, i % 4)
            self.badges.append((eff, _test))
        al.addLayout(grid)
        root.addWidget(ach_card)

        btns = QHBoxLayout()
        self.undo_btn = QPushButton("Undo last glass")
        self.undo_btn.clicked.connect(self._undo)
        log = QPushButton("+ Log a glass")
        log.clicked.connect(self._log)
        btns.addWidget(self.undo_btn)
        btns.addWidget(log)
        btns.addStretch(1)
        close = QPushButton("Close", objectName="primary")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        root.addLayout(btns)
        self.refresh()

    def _set_range(self, d):
        self.range_days = d
        for dd, b in self.seg:
            b.setChecked(dd == d)
        self.refresh()

    def refresh(self):
        cfg = load_config()
        s = compute_stats(goal=max(1, int(cfg["daily_goal_glasses"])))
        self.kpis["today"].setText(f"{s['today']}/{s['goal']}")
        self.kpis["streak"].setText(str(s["streak"]))
        self.kpis["best"].setText(str(s["best"]))
        self.kpis["avg"].setText(f"{s['avg7']:.1f}")
        self.chart.set_data(s["hist"], s["goal"], int(cfg["glass_ml"]), self.range_days)
        unlocked = 0
        for eff, test in self.badges:
            ok = bool(test(s))
            unlocked += ok
            eff.setOpacity(1.0 if ok else 0.35)
        self.ach_title.setText(f"ACHIEVEMENTS · {unlocked}/{len(self.badges)}")
        self.undo_btn.setEnabled(s["today"] > 0)

    def _log(self):
        (self.on_log or record_glass)()
        self.refresh()

    def _undo(self):
        (self.on_undo or undo_glass)()
        self.refresh()


# --------------------------------------------------------------------------- #
# Updates: check GitHub Releases, download, verify (SHA-256), replace, restart
# --------------------------------------------------------------------------- #
UPDATE_FILE = os.path.join(DATA_DIR, "update.json")
UPDATE_DIR = os.path.join(DATA_DIR, "update")


def _version_tuple(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def _update_state(**changes):
    try:
        with open(UPDATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    if changes:
        st.update(changes)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UPDATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    return st


def update_asset_name():
    """Which release file updates *this* installation."""
    if not IS_FROZEN:
        return "water_reminder.py"
    if IS_WIN:
        return "WaterReminder-Windows.exe"
    if IS_MAC:
        return "WaterReminder-macOS.dmg"
    return f"WaterReminder-Linux-{platform.machine()}"


def mac_app_bundle():
    exe = os.path.abspath(sys.executable)
    i = exe.find(".app/")
    return exe[:i + 4] if i > 0 else None


class Updater:
    """Small async client for GitHub Releases using Qt networking (native TLS on every OS)."""

    API = os.environ.get("WATER_REMINDER_UPDATE_URL") or f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"

    def __init__(self):
        self.net = QNetworkAccessManager()
        self.latest = None   # dict: version, notes, url, assets{name: url}
        self._busy = False

    def _request(self, url):
        req = QNetworkRequest(url if isinstance(url, QUrl) else QUrl(url))
        req.setRawHeader(b"User-Agent", f"WaterReminder/{APP_VERSION}".encode())
        req.setRawHeader(b"Accept", b"application/vnd.github+json, */*")
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        req.setTransferTimeout(60000)
        return req

    def check(self, callback):
        """callback(info_or_None, error_or_None); info is set only when a newer version exists."""
        reply = self.net.get(self._request(self.API))

        def done():
            reply.deleteLater()
            if reply.error() != QNetworkReply.NoError:
                callback(None, reply.errorString())
                return
            try:
                data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                info = {
                    "version": str(data.get("tag_name", "")).lstrip("vV"),
                    "notes": data.get("body") or "",
                    "url": data.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/latest",
                    "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets", [])},
                }
            except Exception as e:
                callback(None, f"unexpected reply ({e})")
                return
            _update_state(last_check=time.time())
            newer = _version_tuple(info["version"]) > _version_tuple(APP_VERSION)
            self.latest = info if newer else None
            callback(self.latest, None)
        reply.finished.connect(done)

    def download(self, url, path, progress, finished):
        reply = self.net.get(self._request(url))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path + ".part", "wb")
        reply.readyRead.connect(lambda: fh.write(bytes(reply.readAll())))
        reply.downloadProgress.connect(lambda got, total: progress(got, total))

        def done():
            fh.write(bytes(reply.readAll()))
            fh.close()
            reply.deleteLater()
            if reply.error() != QNetworkReply.NoError:
                finished(reply.errorString())
                return
            os.replace(path + ".part", path)
            finished(None)
        reply.finished.connect(done)
        return reply


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_update(path, app):
    """Install the downloaded file and restart. Returns an error string, or None (app is quitting)."""
    if not IS_FROZEN:
        installed = os.path.join(APP_DIR, "water_reminder.py")
        if os.path.abspath(__file__) != os.path.abspath(installed):
            return ("You're running Water Reminder from its source folder. "
                    "Update it with: git pull, then run the installer again.")
        import py_compile
        try:
            py_compile.compile(path, doraise=True)
        except Exception as e:
            return f"The downloaded file looks broken ({e})."
        shutil.copy2(installed, installed + ".bak")
        shutil.copy2(path, installed)
        spawn_detached([_gui_python(), installed, "--after-update"])
    elif IS_MAC:
        bundle = mac_app_bundle()
        if not bundle:
            return "Couldn't find the app bundle to update."
        script = os.path.join(UPDATE_DIR, "install.sh")
        mnt = os.path.join(UPDATE_DIR, "mnt")
        with open(script, "w") as f:
            f.write(f"""#!/bin/bash
# Water Reminder self-update: swap in the new .app once the old one has quit.
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done
mkdir -p "{mnt}"
hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "{mnt}" "{path}" || {{ open "{path}"; exit 1; }}
NEW=$(ls -d "{mnt}"/*.app | head -1)
rm -rf "{bundle}.old"
if mv "{bundle}" "{bundle}.old" && ditto "$NEW" "{bundle}"; then
  rm -rf "{bundle}.old"
else
  [ -d "{bundle}.old" ] && [ ! -d "{bundle}" ] && mv "{bundle}.old" "{bundle}"
  hdiutil detach "{mnt}" -quiet; open "{path}"; exit 1
fi
hdiutil detach "{mnt}" -quiet
open "{bundle}" --args --after-update
""")
        os.chmod(script, 0o755)
        spawn_detached(["/bin/bash", script])
    else:
        # Windows .exe / Linux binary: the new file installs itself (see frozen_first_run)
        if not IS_WIN:
            os.chmod(path, 0o755)
        spawn_detached([path, "--after-update"])
    QTimer.singleShot(300, app.quit)
    return None


class UpdateDialog(QDialog):
    def __init__(self, updater, info, app):
        super().__init__(None, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.updater, self.info, self.app = updater, info, app
        self.setWindowTitle(f"{APP_NAME} – Update")
        self.setWindowIcon(drop_icon())
        self.setStyleSheet(APP_QSS + "QTextBrowser { background: white; border: 1px solid #d6e6fb;"
                                     " border-radius: 10px; padding: 8px; color: #1b2b44; }"
                                     "QProgressBar { border: 1px solid #c9dcf5; border-radius: 6px; height: 10px;"
                                     " background: #eef5ff; text-align: center; }"
                                     "QProgressBar::chunk { background: #1a70f0; border-radius: 5px; }")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumWidth(480)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        root.addLayout(_header(f"Version {info['version']} is here!", f"You have version {APP_VERSION}."))
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info["notes"].strip() or "Improvements and fixes.")
        notes.setMaximumHeight(220)
        root.addWidget(notes)
        self.status = QLabel("Updating takes a few seconds. Your history and settings are kept.", objectName="hint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.hide()
        root.addWidget(self.bar)
        btns = QHBoxLayout()
        self.skip = QPushButton("Skip this version", objectName="flat")
        self.skip.clicked.connect(self._skip)
        btns.addWidget(self.skip)
        btns.addStretch(1)
        self.later = QPushButton("Later")
        self.later.clicked.connect(self.reject)
        self.go = QPushButton("Update now", objectName="primary")
        self.go.setDefault(True)
        self.go.clicked.connect(self._update)
        btns.addWidget(self.later)
        btns.addWidget(self.go)
        root.addLayout(btns)

    def _skip(self):
        _update_state(skipped=self.info["version"])
        self.reject()

    def _fail(self, msg):
        self.status.setText(msg)
        self.bar.hide()
        self.go.setEnabled(True)
        self.go.setText("Try again")
        self.later.setEnabled(True)

    def _update(self):
        name = update_asset_name()
        assets = self.info["assets"]
        if name not in assets:
            self._fail(f"This release has no file for your system ({name}). "
                       f"You can download it from the releases page instead.")
            QDesktopServices.openUrl(QUrl(self.info["url"]))
            return
        self.go.setEnabled(False)
        self.later.setEnabled(False)
        self.skip.hide()
        self.bar.show()
        self.bar.setRange(0, 0)
        self.status.setText("Downloading…")
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        os.makedirs(UPDATE_DIR, exist_ok=True)
        sums = os.path.join(UPDATE_DIR, "SHA256SUMS.txt")
        target = os.path.join(UPDATE_DIR, name)

        def progress(got, total):
            if total > 0:
                self.bar.setRange(0, 100)
                self.bar.setValue(int(got * 100 / total))
                self.status.setText(f"Downloading… {got / 1e6:.1f} of {total / 1e6:.1f} MB")

        def got_file(err):
            if err:
                self._fail(f"Download failed: {err}")
                return
            try:
                expected = {}
                with open(sums, encoding="utf-8") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) == 2:
                            expected[parts[1].lstrip("*")] = parts[0].lower()
                if expected.get(name) != _sha256(target):
                    self._fail("The download didn't match its checksum, so it wasn't installed. Please try again.")
                    return
            except OSError as e:
                self._fail(f"Couldn't verify the download ({e}).")
                return
            self.status.setText("Installing… Water Reminder will restart in a moment.")
            self.bar.setRange(0, 0)
            err = apply_update(target, self.app)
            if err:
                self._fail(err)

        def got_sums(err):
            if err or "SHA256SUMS.txt" not in assets:
                self._fail("Couldn't download the checksum file, so the update wasn't installed.")
                return
            self.updater.download(assets[name], target, progress, got_file)

        if "SHA256SUMS.txt" not in assets:
            self._fail("This release has no checksum file, so it can't be installed automatically.")
            return
        self.updater.download(assets["SHA256SUMS.txt"], sums, lambda *_: None, got_sums)


# --------------------------------------------------------------------------- #
# Background service: smart scheduling + tray icon
# --------------------------------------------------------------------------- #
class Service:
    HEARTBEAT_MS = 15000
    LONG_BREAK = 15 * 60  # away at least this long -> "welcome back" reminder

    def __init__(self, app, once=False):
        self.app = app
        self.once = once
        self.overlay = None
        self.due_at = None
        self.paused_until = None
        self.away = False
        self.away_since = None
        self.frozen_remaining = None
        self.waiting_reason = None
        self.intro = None
        self.settings_dlg = None
        self.stats_dlg = None
        self.tray = None
        self._last_beat = time.monotonic()
        self._last_wall = time.time()
        self.precise = QTimer()
        self.precise.setSingleShot(True)
        self.precise.timeout.connect(self._heartbeat)
        self.updater = Updater()
        self.update_dlg = None
        if not once:
            QTimer.singleShot(90 * 1000, self._auto_check_updates)
            self.update_timer = QTimer()
            self.update_timer.timeout.connect(self._auto_check_updates)
            self.update_timer.start(6 * 3600 * 1000)
            self.beat = QTimer()
            self.beat.timeout.connect(self._heartbeat)
            self.beat.start(self.HEARTBEAT_MS)
            if QSystemTrayIcon.isSystemTrayAvailable():
                self._make_tray()

    # -- scheduling -------------------------------------------------------------
    def schedule(self, minutes):
        minutes = max(0.02, float(minutes))
        self.due_at = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        self.precise.start(int(minutes * 60000) + 200)
        print(f"[{APP_ID}] next reminder at {self.due_at:%H:%M:%S}", flush=True)
        self._refresh_tray()

    def _heartbeat(self):
        if self.once:
            return
        now = datetime.datetime.now()
        cfg = load_config()
        # A big gap between heartbeats means the computer was asleep.
        gap = time.time() - self._last_wall
        self._last_wall = time.time()
        if gap > 180 and self.overlay is None and not self.away:
            print(f"[{APP_ID}] woke up after {gap / 60:.0f} min", flush=True)
            self.away, self.away_since = True, now - datetime.timedelta(seconds=gap)
            self.frozen_remaining = None
        if cfg.get("pause_when_away", True):
            idle = Probes.idle_seconds()
            limit = max(60, float(cfg.get("away_minutes", 5)) * 60)
            if idle is not None and not self.away and idle >= limit and self.overlay is None:
                self.away = True
                self.away_since = now - datetime.timedelta(seconds=idle)
                self.frozen_remaining = ((self.due_at - now).total_seconds() if self.due_at else None)
                self.precise.stop()
                print(f"[{APP_ID}] you seem to be away – pausing", flush=True)
                self._refresh_tray()
            elif self.away and (idle is None or idle < 30):
                self._came_back((now - self.away_since).total_seconds())
        elif self.away:
            self._came_back((now - self.away_since).total_seconds())
        if self.away:
            return
        if self.paused_until and now >= self.paused_until:
            self.paused_until = None
        if self.overlay is None and self.due_at and now >= self.due_at:
            self._try_show(cfg)

    def _came_back(self, away_seconds):
        cfg = load_config()
        self.away, self.away_since = False, None
        if away_seconds >= self.LONG_BREAK:
            print(f"[{APP_ID}] welcome back (away {away_seconds / 60:.0f} min)", flush=True)
            self.intro = "welcome"
            self.schedule(1)
        else:
            rem = self.frozen_remaining
            self.schedule(rem / 60 if rem and rem > 0 else 0.5 if rem is not None else cfg["interval_minutes"])
        self.frozen_remaining = None

    def _try_show(self, cfg=None, force=False):
        cfg = cfg or load_config()
        if not force:
            if in_quiet_hours(cfg):
                self.intro = None
                self.schedule(cfg["interval_minutes"])
                return
            if cfg.get("respect_focus", True):
                reason = Probes.busy_reason()
                if reason:
                    if reason != self.waiting_reason:
                        print(f"[{APP_ID}] waiting – {reason}", flush=True)
                        self.waiting_reason = reason
                        self._refresh_tray()
                    return  # try again on the next heartbeat
        self.waiting_reason = None
        self.paused_until = None
        self.show(cfg)

    def show(self, cfg=None):
        if self.overlay is not None:
            return
        cfg = cfg or load_config()
        self.precise.stop()
        self.due_at = None
        if cfg.get("sound", True):
            play_sound("reminder")
        intro, self.intro = self.intro, None
        self.overlay = Overlay(cfg, load_state()["count"], self._done, self.open_settings, intro=intro)
        self._refresh_tray()

    def _done(self, result):
        self.overlay = None
        cfg = load_config()
        if result == "drank":
            self.log_glass()
        if self.once:
            QTimer.singleShot(100, self.app.quit)
            return
        self.schedule(cfg["snooze_minutes"] if result == "snooze" else cfg["interval_minutes"])

    def log_glass(self):
        record_glass()
        self._refresh_tray()
        if self.stats_dlg is not None and self.stats_dlg.isVisible():
            self.stats_dlg.refresh()

    def undo_glass(self):
        undo_glass()
        self._refresh_tray()

    def command(self, cmd):
        cmd = cmd.strip()
        if cmd == "show":
            self._try_show(force=True)
        elif cmd == "welcome":
            self.intro = "hello"
            self._try_show(force=True)
        elif cmd == "settings":
            self.open_settings()
        elif cmd == "stats":
            self.open_stats()
        elif cmd == "update":
            self.check_updates_now()
        elif cmd == "quit":
            self.app.quit()

    # -- updates -----------------------------------------------------------------
    def _auto_check_updates(self, force=False):
        if not load_config().get("check_updates", True):
            return
        last = _update_state().get("last_check", 0)
        if not force and time.time() - last < 20 * 3600 and not self.updater.latest:
            return
        self.updater.check(self._on_auto_checked)

    def _on_auto_checked(self, info, err):
        self._refresh_tray()
        if not info or info["version"] == _update_state().get("skipped"):
            return
        st = _update_state()
        if st.get("prompted") == info["version"] and time.time() - st.get("prompted_at", 0) < 20 * 3600:
            return  # asked recently: don't nag
        cfg = load_config()
        if self.overlay is not None or (cfg.get("respect_focus", True) and Probes.busy_reason()):
            QTimer.singleShot(30 * 60 * 1000, lambda: self._on_auto_checked(info, None))
            return
        _update_state(prompted=info["version"], prompted_at=time.time())
        if self.tray:
            self.tray.showMessage(APP_NAME, f"Version {info['version']} is available.", drop_icon(), 6000)
        self.show_update(info)

    def check_updates_now(self):
        def done(info, err):
            self._refresh_tray()
            if info:
                self.show_update(info)
            elif err:
                QMessageBox.warning(None, APP_NAME, f"Couldn't check for updates:\n{err}")
            else:
                QMessageBox.information(None, APP_NAME, f"You're up to date ✓\n\nVersion {APP_VERSION}")
        self.updater.check(done)

    def show_update(self, info):
        if self.update_dlg is not None and self.update_dlg.isVisible():
            _bring_to_front(self.update_dlg)
            return
        self.update_dlg = UpdateDialog(self.updater, info, self.app)
        _bring_to_front(self.update_dlg)

    # -- windows ---------------------------------------------------------------
    def open_settings(self):
        if self.settings_dlg is not None and self.settings_dlg.isVisible():
            _bring_to_front(self.settings_dlg)
            return
        self.settings_dlg = SettingsDialog(on_saved=self._settings_saved, on_preview=lambda: self._try_show(force=True),
                                           on_stats=self.open_stats, on_quit=self.app.quit,
                                           on_check_updates=self.check_updates_now)
        _bring_to_front(self.settings_dlg)

    def open_stats(self):
        if self.stats_dlg is not None and self.stats_dlg.isVisible():
            self.stats_dlg.refresh()
            _bring_to_front(self.stats_dlg)
            return
        self.stats_dlg = StatsDialog(on_log=self.log_glass, on_undo=self.undo_glass)
        _bring_to_front(self.stats_dlg)

    def _settings_saved(self, cfg):
        if self.overlay is None and not self.paused_until and not self.once:
            self.schedule(cfg["interval_minutes"])
        self._refresh_tray()

    # -- tray --------------------------------------------------------------------
    def _make_tray(self):
        self.tray = QSystemTrayIcon(drop_icon(), self.app)
        menu = QMenu()
        self.a_now = QAction("Remind me now", menu, triggered=lambda: self._try_show(force=True))
        self.a_log = QAction("Log a glass", menu, triggered=self.log_glass)
        self.a_undo = QAction("Undo last glass", menu, triggered=self.undo_glass)
        self.a_today = QAction("", menu, enabled=False)
        self.a_next = QAction("", menu, enabled=False)
        self.a_stats = QAction("History & stats…", menu, triggered=self.open_stats)
        self.a_pause = QAction("Pause for 1 hour", menu, triggered=self._toggle_pause)
        self.a_settings = QAction("Settings…", menu, triggered=self.open_settings)
        self.a_update = QAction("Check for updates…", menu, triggered=self._tray_update)
        self.a_quit = QAction("Quit Water Reminder", menu, triggered=self.app.quit)
        for a in (self.a_now, self.a_log, self.a_undo, None, self.a_today, self.a_next, None, self.a_stats,
                  self.a_pause, self.a_settings, self.a_update, None, self.a_quit):
            menu.addSeparator() if a is None else menu.addAction(a)
        menu.aboutToShow.connect(self._refresh_tray)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.open_stats() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()
        self._refresh_tray()

    def status_text(self):
        if self.overlay is not None:
            return "Reminder on screen"
        if self.away:
            return "Paused – you're away"
        if self.waiting_reason:
            return f"Waiting – {self.waiting_reason}"
        if self.paused_until:
            return f"Paused until {self.paused_until:%H:%M}"
        return f"Next reminder: {self.due_at:%H:%M}" if self.due_at else "Next reminder: –"

    def _refresh_tray(self):
        if not self.tray:
            return
        cfg, st = load_config(), load_state()
        today = f"Today: {st['count']}/{cfg['daily_goal_glasses']} glasses ({st['count'] * cfg['glass_ml']} ml)"
        nxt = self.status_text()
        self.a_today.setText(today)
        self.a_next.setText(nxt)
        self.a_undo.setEnabled(st["count"] > 0)
        self.a_pause.setText("Resume reminders" if self.paused_until else "Pause for 1 hour")
        latest = self.updater.latest
        self.a_update.setText(f"Update to version {latest['version']}…" if latest else "Check for updates…")
        self.tray.setToolTip(f"{APP_NAME}\n{today}\n{nxt}")

    def _tray_update(self):
        if self.updater.latest:
            self.show_update(self.updater.latest)
        else:
            self.check_updates_now()

    def _toggle_pause(self):
        if self.paused_until:
            self.paused_until = None
            self.schedule(load_config()["interval_minutes"])
        else:
            self.paused_until = datetime.datetime.now() + datetime.timedelta(hours=1)
            self.schedule(60)
        self._refresh_tray()


# --------------------------------------------------------------------------- #
# Single instance + commands (--now, --settings, ...) over a local socket
# --------------------------------------------------------------------------- #
def send_command(cmd, timeout=800):
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(timeout):
        return False
    sock.write((cmd + "\n").encode())
    sock.flush()
    sock.waitForBytesWritten(timeout)
    sock.disconnectFromServer()
    return True


def start_server(service):
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(SERVER_NAME):
        print(f"[{APP_ID}] could not open control socket: {server.errorString()}", file=sys.stderr)

    def on_conn():
        conn = server.nextPendingConnection()

        def on_read():
            for line in bytes(conn.readAll()).decode(errors="ignore").split():
                service.command(line)
        conn.readyRead.connect(on_read)
        conn.disconnected.connect(conn.deleteLater)
    server.newConnection.connect(on_conn)
    return server


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #
def _stop_old_linux_daemon():
    """Stop the earlier GTK-based version if it is running."""
    pid_file = os.path.join(DATA_DIR, "daemon.pid")
    try:
        with open(pid_file) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
    except Exception:
        pass
    for fn in ("daemon.pid", "daemon.lock"):
        try:
            os.remove(os.path.join(DATA_DIR, fn))
        except OSError:
            pass


def _wait_and_welcome():
    for _ in range(60):
        time.sleep(0.25)
        if send_command("welcome"):
            return True
    return False


def do_install():
    """Script install (used by install.sh / install.bat): copy, start at login, launch."""
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    src = os.path.abspath(__file__)
    dst = os.path.join(APP_DIR, "water_reminder.py")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    cfg = load_config()
    cmd = [_gui_python(), dst]
    if send_command("quit"):
        time.sleep(1.0)
    if IS_LINUX:
        _stop_old_linux_daemon()
    autostart_enable(cmd)
    create_shortcuts(cmd)
    cfg["autostart_configured"] = True
    save_config(cfg)
    if IS_MAC:
        _run(["launchctl", "unload", MAC_PLIST])
        _run(["launchctl", "load", "-w", MAC_PLIST])
    else:
        spawn_detached(cmd)
    _wait_and_welcome()
    print(f"\nInstalled! A water drop will visit every {cfg['interval_minutes']} minutes "
          f"and it starts automatically when you log in.")
    print("  Use the water-drop icon in your " + ("menu bar" if IS_MAC else "system tray") +
          " for Settings and History & stats.")
    if IS_LINUX:
        print("  Or run: water-reminder --settings   /   water-reminder --stats")
    return 0


def frozen_first_run(after_update=False):
    """Packaged app (.exe / Linux binary / .app): install itself for this user.

    Returns True if this process should exit (because an installed copy was started).
    """
    cfg = load_config()
    if IS_MAC:
        exe = sys.executable
        if exe.startswith("/Volumes/") or "/AppTranslocation/" in exe:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(None, APP_NAME, "Please drag Water Reminder into your Applications "
                                                    "folder first, then open it from there.")
            return True
        moved = False
        if autostart_is_enabled():
            with open(MAC_PLIST, "rb") as f:
                moved = exe.encode() not in f.read()
        if not cfg.get("autostart_configured") or moved:
            autostart_enable([exe])
            cfg["autostart_configured"] = True
            save_config(cfg)
            return "welcome"
        return False

    exe = os.path.abspath(sys.executable)
    target = os.path.join(APP_DIR, "WaterReminder.exe" if IS_WIN else "WaterReminder")
    if os.path.normcase(exe) == os.path.normcase(os.path.abspath(target)):
        return False  # already running the installed copy
    # A fresh download (or an update): replace the installed copy and start it.
    if send_command("quit"):
        time.sleep(1.5)
    os.makedirs(APP_DIR, exist_ok=True)
    for _ in range(20):
        try:
            shutil.copy2(exe, target)
            break
        except OSError:
            time.sleep(0.5)
    else:
        return False  # couldn't copy: just run from here
    if not IS_WIN:
        os.chmod(target, 0o755)
    if IS_LINUX:
        _stop_old_linux_daemon()
    if not cfg.get("autostart_configured") or autostart_is_enabled():
        autostart_enable([target])
    create_shortcuts([target])
    cfg["autostart_configured"] = True
    save_config(cfg)
    spawn_detached([target, "--after-update" if after_update else "--welcome"])
    return True


def do_uninstall(purge=False, quiet=False):
    send_command("quit")
    autostart_disable()
    remove_shortcuts()
    if IS_MAC:
        _run(["launchctl", "unload", MAC_PLIST])
    if IS_LINUX:
        _stop_old_linux_daemon()
    if IS_FROZEN and IS_WIN:
        # can't delete a running .exe: remove the folder shortly after we exit
        subprocess.Popen(f'cmd /c for /l %i in (1,1,15) do (ping -n 2 127.0.0.1 >nul '
                         f'& if exist "{APP_DIR}" rmdir /s /q "{APP_DIR}" 2>nul)',
                         creationflags=0x08000000, close_fds=True, cwd=os.path.dirname(APP_DIR))
    else:
        shutil.rmtree(APP_DIR, ignore_errors=True)
    if purge:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    if not quiet:
        print("Water Reminder removed." + ("" if purge else " (Your history and settings were kept.)"))
    return 0


def print_status():
    cfg, st = load_config(), load_state()
    if QCoreApplication.instance() is None:
        QCoreApplication(sys.argv)
    running = send_command("ping")
    s = compute_stats(st, max(1, int(cfg["daily_goal_glasses"])))
    print(f"Today: {st['count']}/{cfg['daily_goal_glasses']} glasses ({st['count'] * cfg['glass_ml']} ml)")
    print(f"Streak: {s['streak']} days (best {s['best']})   7-day average: {s['avg7']:.1f} glasses/day")
    print(f"Reminder service: {'running' if running else 'not running'}   Start at login: "
          f"{'on' if autostart_is_enabled() else 'off'}")
    print(f"Interval: every {cfg['interval_minutes']} min   Config: {CONFIG_FILE}")
    return 0


# --------------------------------------------------------------------------- #
def main():
    if IS_FROZEN and sys.stdout is None:  # windowed build: keep a log instead of a console
        os.makedirs(DATA_DIR, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1, encoding="utf-8")

    ap = argparse.ArgumentParser(description="Animated water reminder")
    ap.add_argument("--now", action="store_true", help="show the reminder right now")
    ap.add_argument("--settings", action="store_true", help="open the settings window")
    ap.add_argument("--stats", action="store_true", help="open history & stats")
    ap.add_argument("--status", action="store_true", help="print today's progress")
    ap.add_argument("--stop", action="store_true", help="stop the background reminder")
    ap.add_argument("--install", action="store_true", help="start automatically at login")
    ap.add_argument("--uninstall", action="store_true", help="remove start-at-login")
    ap.add_argument("--purge", action="store_true", help="with --uninstall: also delete history/settings")
    ap.add_argument("--welcome", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--check-updates", action="store_true", help="check for a new version")
    args, _unknown = ap.parse_known_args()  # tolerate extra args (e.g. macOS -psn_...)

    if args.status:
        return print_status()

    if args.stop or args.install or args.uninstall:
        QCoreApplication(sys.argv)
        if args.install:
            return do_install()
        if args.uninstall:
            return do_uninstall(args.purge)
        print("Stopped." if send_command("quit") else "Not running.")
        return 0

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(drop_icon(128))
    app.setQuitOnLastWindowClosed(False)
    if IS_MAC:
        mac_hide_dock_icon()

    if args.check_updates:
        if send_command("update"):
            return 0
        upd = Updater()
        keep = []

        def checked(info, err):
            if info:
                dlg = UpdateDialog(upd, info, app)
                dlg.finished.connect(lambda _r: QTimer.singleShot(0, app.quit))
                keep.append(dlg)
                _bring_to_front(dlg)
                return
            if err:
                QMessageBox.warning(None, APP_NAME, f"Couldn't check for updates:\n{err}")
            else:
                QMessageBox.information(None, APP_NAME, f"You're up to date ✓\n\nVersion {APP_VERSION}")
            app.quit()
        upd.check(checked)
        return app.exec()

    for flag, cmd, dialog in ((args.settings, "settings", "settings"), (args.stats, "stats", "stats")):
        if not flag:
            continue
        if send_command(cmd):
            return 0
        if dialog == "settings":
            keep = []

            def preview():
                if not send_command("show") and not keep:
                    keep.append(Overlay(load_config(), load_state()["count"], lambda _r: keep.clear()))
            dlg = SettingsDialog(on_preview=preview)
        else:
            dlg = StatsDialog()
        dlg.setAttribute(Qt.WA_DeleteOnClose, False)
        _bring_to_front(dlg)
        dlg.finished.connect(lambda _r: QTimer.singleShot(0, app.quit))
        return app.exec()

    if args.now:
        if send_command("show"):
            return 0
        svc = Service(app, once=True)
        svc.show()
        return app.exec()

    welcome = args.welcome
    if args.after_update:
        for _ in range(60):  # let the old version finish quitting
            if not send_command("ping", timeout=300):
                break
            send_command("quit")
            time.sleep(0.5)
    if IS_FROZEN:
        r = frozen_first_run(after_update=args.after_update)
        if r is True:
            return 0
        welcome = welcome or r == "welcome"

    if send_command("ping"):
        # Opened again while already running (e.g. double-clicked the app): show Settings.
        send_command("welcome" if welcome else "settings")
        print(f"[{APP_ID}] already running.")
        return 0
    svc = Service(app)
    svc.server = start_server(svc)  # keep a reference
    cfg = load_config()
    print(f"[{APP_ID}] v{APP_VERSION} running - every {cfg['interval_minutes']} min "
          f"(quiet {cfg['quiet_hours']['start']}-{cfg['quiet_hours']['end']})", flush=True)
    svc.schedule(cfg["interval_minutes"])
    if args.after_update:
        print(f"[{APP_ID}] updated to v{APP_VERSION}", flush=True)
        _update_state(skipped=None, prompted=None)

        def greet():
            svc.intro = "updated"
            svc._try_show(force=True)
        QTimer.singleShot(1500, greet)
    elif welcome:
        QTimer.singleShot(1500, lambda: svc.command("welcome"))

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
