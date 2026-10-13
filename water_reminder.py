#!/usr/bin/env python3
"""
Water Reminder - an animated hydration buddy for Windows, macOS and Linux.

Every N minutes a cute water-drop mascot swims onto your screen holding a glass
of water, while a wave rises along the bottom of the screen. Click
"I drank a glass" to log it, or snooze it.

Usage:
  water_reminder.py              run in the background (normally started at login)
  water_reminder.py --now        show the reminder right now
  water_reminder.py --status     print today's progress
  water_reminder.py --stop       stop the background reminder
  water_reminder.py --install    set up start-at-login for this user
  water_reminder.py --uninstall  remove start-at-login (add --purge to delete history)

Requires Python 3.9+ and PySide6 (pip install PySide6-Essentials).
"""
import argparse
import datetime
import getpass
import json
import math
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave

APP_NAME = "Water Reminder"
APP_ID = "water-reminder"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# On Linux Wayland, use XWayland so the overlay can be positioned and kept on top.
if IS_LINUX and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY") \
        and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "xcb"

try:
    from PySide6.QtCore import QCoreApplication, QElapsedTimer, QPointF, QRectF, Qt, QTimer, QUrl
    from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QFont, QFontMetricsF,
                               QGuiApplication, QIcon, QLinearGradient, QPainter, QPainterPath,
                               QPen, QPixmap, QPolygonF, QRadialGradient)
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget
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
    for k in sorted(st["history"])[:-60]:
        del st["history"][k]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)


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


def is_x11():
    return IS_LINUX and QGuiApplication.platformName() == "xcb"


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


def make_font(px, bold=False):
    f = QFont(QGuiApplication.font())
    f.setPixelSize(max(1, int(round(px))))
    f.setBold(bold)
    return f


def text_height(font, text, width):
    fm = QFontMetricsF(font)
    return fm.boundingRect(QRectF(0, 0, width, 100000), int(Qt.TextWordWrap), text).height()


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

    def __init__(self, W, H, cfg, count, name, compact=False):
        self.cfg = cfg
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
        self.fx_h = int(self.wave_h + 80 * u)

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
        return f"Time to drink water, {self.name}!", self.tip

    def bubble_height(self, celebrate):
        u, pad = self.u, self.pad
        inner = self.bw - 2 * pad
        best = 0
        for c in (self.count + 1, self.goal + 1):
            title, sub = self._texts(celebrate, c)
            th = text_height(make_font(19 * u, True), title, inner - 58 * u)
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
                                               -random.uniform(25, 55) * u, random.uniform(3, 9) * u,
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
            for x in range(-20, int(W) + 40, 16):
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
        W, H = self.W, self.H
        x, y = self.pos
        bw = self.bw
        inner = bw - 2 * pad
        celebrate = self.state == "celebrate" or self.result == "drank"

        title, sub = self._texts(celebrate)
        f_title, f_sub = make_font(19 * u, True), make_font(13.5 * u)
        th = text_height(f_title, title, inner - 58 * u)
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
        p.drawText(QRectF(bx + pad + 30 * u, cy - th / 2 - 1, inner - 58 * u, th + 4),
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

    def __init__(self, cfg, count, on_done):
        self.on_done = on_done
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.avail = screen.availableGeometry()
        self.x11 = is_x11()
        compact = self.x11 and not _X11.has_compositor()
        self.scene = Scene(self.avail.width(), self.avail.height(), cfg, count, user_first_name(cfg), compact)
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
        self.mascot.update()
        if self.fx:
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
# Background service + tray icon
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


class Service:
    def __init__(self, app, once=False):
        self.app = app
        self.once = once
        self.overlay = None
        self.next_at = None
        self.paused_until = None
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self._fire(False))
        self.tray = None
        if not once and QSystemTrayIcon.isSystemTrayAvailable():
            self._make_tray()

    # scheduling
    def schedule(self, minutes):
        minutes = max(0.05, float(minutes))
        self.timer.start(int(minutes * 60000))
        self.next_at = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        print(f"[{APP_ID}] next reminder at {self.next_at:%H:%M}", flush=True)
        self._refresh_tray()

    def _fire(self, force):
        cfg = load_config()
        now = datetime.datetime.now()
        if not force:
            if self.paused_until and now < self.paused_until:
                self.schedule((self.paused_until - now).total_seconds() / 60 + 0.1)
                return
            if in_quiet_hours(cfg):
                self.schedule(cfg["interval_minutes"])
                return
        self.paused_until = None
        self.show(cfg)

    def show(self, cfg=None):
        if self.overlay is not None:
            return
        cfg = cfg or load_config()
        self.timer.stop()
        if cfg.get("sound", True):
            play_sound("reminder")
        self.overlay = Overlay(cfg, load_state()["count"], self._done)

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
        st = load_state()
        st["count"] += 1
        save_state(st)
        self._refresh_tray()

    def command(self, cmd):
        cmd = cmd.strip()
        if cmd == "show":
            self._fire(True)
        elif cmd == "quit":
            self.app.quit()

    # tray
    def _make_tray(self):
        self.tray = QSystemTrayIcon(drop_icon(), self.app)
        menu = QMenu()
        self.a_now = QAction("Remind me now", menu, triggered=lambda: self._fire(True))
        self.a_log = QAction("Log a glass", menu, triggered=self.log_glass)
        self.a_today = QAction("", menu, enabled=False)
        self.a_next = QAction("", menu, enabled=False)
        self.a_pause = QAction("Pause for 1 hour", menu, triggered=self._toggle_pause)
        self.a_settings = QAction("Settings…", menu, triggered=self._open_settings)
        self.a_quit = QAction("Quit Water Reminder", menu, triggered=self.app.quit)
        for a in (self.a_now, self.a_log, None, self.a_today, self.a_next, None, self.a_pause,
                  self.a_settings, None, self.a_quit):
            menu.addSeparator() if a is None else menu.addAction(a)
        menu.aboutToShow.connect(self._refresh_tray)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()
        self._refresh_tray()

    def _refresh_tray(self):
        if not self.tray:
            return
        cfg, st = load_config(), load_state()
        today = f"Today: {st['count']}/{cfg['daily_goal_glasses']} glasses ({st['count'] * cfg['glass_ml']} ml)"
        if self.paused_until:
            nxt = f"Paused until {self.paused_until:%H:%M}"
        else:
            nxt = f"Next reminder: {self.next_at:%H:%M}" if self.next_at else "Next reminder: –"
        self.a_today.setText(today)
        self.a_next.setText(nxt)
        self.a_pause.setText("Resume reminders" if self.paused_until else "Pause for 1 hour")
        self.tray.setToolTip(f"{APP_NAME}\n{today}\n{nxt}")

    def _toggle_pause(self):
        if self.paused_until:
            self.paused_until = None
            self.schedule(load_config()["interval_minutes"])
        else:
            self.paused_until = datetime.datetime.now() + datetime.timedelta(hours=1)
            self.schedule(60.1)
        self._refresh_tray()

    def _open_settings(self):
        load_config()
        QDesktopServices.openUrl(QUrl.fromLocalFile(CONFIG_FILE))


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
# Install / uninstall (start at login)
# --------------------------------------------------------------------------- #
MAC_PLIST = _home("Library", "LaunchAgents", "com.waterreminder.agent.plist")
LINUX_AUTOSTART = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _home(".config"), "autostart",
                               "water-reminder.desktop")
LINUX_LAUNCHER = os.path.join(os.environ.get("XDG_DATA_HOME") or _home(".local", "share"), "applications",
                              "water-reminder.desktop")
LINUX_BIN = _home(".local", "bin", "water-reminder")
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_START_MENU = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                              "Water Reminder.lnk")


def _gui_python():
    py = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
    return py


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


def do_install():
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    src = os.path.abspath(__file__)
    dst = os.path.join(APP_DIR, "water_reminder.py")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    load_config()
    py = _gui_python()

    if send_command("quit"):
        time.sleep(1.0)

    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "WaterReminder", 0, winreg.REG_SZ, f'"{py}" "{dst}"')
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{WIN_START_MENU}');"
              f"$s.TargetPath='{py}';$s.Arguments='\"{dst}\" --now';"
              f"$s.Description='Show the water reminder now';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        subprocess.Popen([py, dst], creationflags=0x00000008 | 0x00000200, close_fds=True)  # detached
    elif IS_MAC:
        os.makedirs(os.path.dirname(MAC_PLIST), exist_ok=True)
        with open(MAC_PLIST, "w") as f:
            f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.waterreminder.agent</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>{dst}</string></array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>{LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict>
</plist>
""")
        subprocess.run(["launchctl", "unload", MAC_PLIST], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", MAC_PLIST], capture_output=True)
    else:
        _stop_old_linux_daemon()
        for path in (LINUX_AUTOSTART, LINUX_LAUNCHER, LINUX_BIN):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(LINUX_BIN, "w") as f:
            f.write(f'#!/bin/sh\nexec "{py}" "{dst}" "$@"\n')
        os.chmod(LINUX_BIN, 0o755)
        entry = ("[Desktop Entry]\nType=Application\nName=Water Reminder\nIcon=weather-showers\n"
                 "Categories=Utility;\nKeywords=water;drink;hydration;reminder;\n")
        with open(LINUX_AUTOSTART, "w") as f:
            f.write(entry + f"Comment=Animated reminder to drink water\nExec={LINUX_BIN}\n"
                            "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=20\n")
        with open(LINUX_LAUNCHER, "w") as f:
            f.write(entry + f"Comment=Show the water reminder now\nExec={LINUX_BIN} --now\n")
        with open(LOG_FILE, "a") as log:
            subprocess.Popen([py, dst], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)

    # wait for the service, then show a first reminder as a demo
    for _ in range(40):
        time.sleep(0.25)
        if send_command("show"):
            break
    cfg = load_config()
    print(f"\nInstalled! A water drop will visit every {cfg['interval_minutes']} minutes "
          f"and it starts automatically when you log in.")
    print(f"  Settings: {CONFIG_FILE}")
    if not IS_WIN:
        print(f"  Show now: {'water-reminder' if IS_LINUX else 'python3 ' + repr(dst)} --now")
    print("  Tip: the water-drop icon in your " + ("menu bar" if IS_MAC else "system tray") +
          " has 'Remind me now', 'Pause' and 'Settings'.")
    return 0


def do_uninstall(purge):
    send_command("quit")
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "WaterReminder")
        except OSError:
            pass
        for path in (WIN_START_MENU,):
            try:
                os.remove(path)
            except OSError:
                pass
    elif IS_MAC:
        subprocess.run(["launchctl", "unload", MAC_PLIST], capture_output=True)
        try:
            os.remove(MAC_PLIST)
        except OSError:
            pass
    else:
        _stop_old_linux_daemon()
        for path in (LINUX_AUTOSTART, LINUX_LAUNCHER, LINUX_BIN):
            try:
                os.remove(path)
            except OSError:
                pass
    try:
        os.remove(os.path.join(APP_DIR, "water_reminder.py"))
    except OSError:
        pass
    if purge:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    print("Water Reminder removed." + ("" if purge else " (Your history and settings were kept.)"))
    return 0


def print_status():
    cfg, st = load_config(), load_state()
    if QCoreApplication.instance() is None:
        QCoreApplication(sys.argv)
    running = send_command("ping")
    print(f"Today: {st['count']}/{cfg['daily_goal_glasses']} glasses ({st['count'] * cfg['glass_ml']} ml)")
    print(f"Reminder service: {'running' if running else 'not running'}")
    print(f"Interval: every {cfg['interval_minutes']} min   Config: {CONFIG_FILE}")
    last = sorted(st["history"].items())[-7:]
    if last:
        print("Last days: " + "  ".join(f"{d[5:]}:{c}" for d, c in last))
    return 0


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Animated water reminder")
    ap.add_argument("--now", action="store_true", help="show the reminder right now")
    ap.add_argument("--status", action="store_true", help="print today's progress")
    ap.add_argument("--stop", action="store_true", help="stop the background reminder")
    ap.add_argument("--install", action="store_true", help="start automatically at login")
    ap.add_argument("--uninstall", action="store_true", help="remove start-at-login")
    ap.add_argument("--purge", action="store_true", help="with --uninstall: also delete history/settings")
    args = ap.parse_args()

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
    app.setQuitOnLastWindowClosed(False)
    if IS_MAC:
        mac_hide_dock_icon()

    if args.now:
        if send_command("show"):
            return 0
        svc = Service(app, once=True)
        svc.show()
        return app.exec()

    if send_command("ping"):
        print(f"[{APP_ID}] already running.")
        return 0
    svc = Service(app)
    server = start_server(svc)  # noqa: F841 (keep a reference)
    cfg = load_config()
    print(f"[{APP_ID}] running - every {cfg['interval_minutes']} min "
          f"(quiet {cfg['quiet_hours']['start']}-{cfg['quiet_hours']['end']})", flush=True)
    svc.schedule(cfg["interval_minutes"])

    # let Ctrl+C / SIGTERM stop the Qt event loop
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
