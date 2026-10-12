#!/usr/bin/env python3
"""
Water Reminder - an animated hydration buddy for Linux desktops.

A cute water-drop mascot swims onto your screen every N minutes, holding a
glass of water, with a rising wave along the bottom of the screen. Click
"I drank a glass" to log it, or snooze.

Usage:
  water_reminder.py            run the background reminder (normally started at login)
  water_reminder.py --now      show the reminder right now
  water_reminder.py --status   print today's progress
  water_reminder.py --stop     stop the background reminder

Config: ~/.config/water-reminder/config.json  (re-read on every reminder)
"""
import argparse
import datetime
import fcntl
import json
import math
import os
import pwd
import random
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave

# Positioning a full-screen transparent overlay needs X11 (or XWayland on Wayland).
os.environ.setdefault("GDK_BACKEND", "x11,wayland")

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

try:
    gi.require_foreign("cairo")
except Exception:
    pass

APP = "water-reminder"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP)
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOCK_FILE = os.path.join(DATA_DIR, "daemon.lock")
PID_FILE = os.path.join(DATA_DIR, "daemon.pid")

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

FONT = "Inter, Ubuntu, Cantarell, Noto Sans, DejaVu Sans, Sans"


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
        with open(CONFIG_FILE) as f:
            return _merge(DEFAULTS, json.load(f))
    except FileNotFoundError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULTS, f, indent=2)
        return dict(DEFAULTS)
    except Exception as e:  # broken JSON -> fall back to defaults
        print(f"[{APP}] config error ({e}); using defaults", file=sys.stderr)
        return dict(DEFAULTS)


def load_state():
    today = datetime.date.today().isoformat()
    try:
        with open(STATE_FILE) as f:
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
    # keep the last 60 days
    for k in sorted(st["history"])[:-60]:
        del st["history"][k]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
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
    if s <= e:
        return s <= now < e
    return now >= s or now < e


def user_first_name(cfg):
    if cfg.get("name"):
        return cfg["name"]
    try:
        pw = pwd.getpwuid(os.getuid())
        full = (pw.pw_gecos or "").split(",")[0].strip()
        name = full.split()[0] if full else pw.pw_name
        return name[:1].upper() + name[1:]
    except Exception:
        return "friend"


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
            f = f0 * (f1 / f0) ** t
            phase += 2 * math.pi * f / rate
            env = math.sin(math.pi * min(1, t * 6)) if t < 1 / 12 else (1 - t) ** 2.2
            frames.extend(struct.pack("<h", int(32767 * vol * env * math.sin(phase))))

    def silence(d):
        frames.extend(b"\x00\x00" * int(rate * d))

    bloop(380, 1100, 0.13, 0.45)
    if double:
        silence(0.07)
        bloop(520, 1500, 0.12, 0.40)
    silence(0.05)
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
        for player in (["paplay"], ["pw-play"], ["aplay", "-q"]):
            if shutil.which(player[0]):
                subprocess.Popen(player + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
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


def rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def drop_path(cr, R):
    cr.move_to(0, -1.75 * R)
    cr.curve_to(0.3 * R, -1.2 * R, R, -0.75 * R, R, 0)
    cr.arc(0, 0, R, 0, math.pi)
    cr.curve_to(-R, -0.75 * R, -0.3 * R, -1.2 * R, 0, -1.75 * R)
    cr.close_path()


def make_layout(cr, text, px, bold=False, width=None):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(FONT)
    fd.set_absolute_size(px * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.BOLD)
    lay.set_font_description(fd)
    if width:
        lay.set_width(int(width * Pango.SCALE))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_text(text, -1)
    return lay


def show_layout(cr, lay, x, y, rgba):
    cr.move_to(x, y)
    cr.set_source_rgba(*rgba)
    PangoCairo.show_layout(cr, lay)


def glass_path(cr, x, y, w, h):
    """Tapered glass, (x, y) = top-left of its bounding box."""
    inset = w * 0.12
    cr.move_to(x, y)
    cr.line_to(x + w, y)
    cr.line_to(x + w - inset, y + h)
    cr.line_to(x + inset, y + h)
    cr.close_path()


def draw_glass(cr, x, y, w, h, level, t, tilt=0.0):
    """Glass with sloshing water. level in 0..1."""
    cr.save()
    glass_path(cr, x, y, w, h)
    cr.set_source_rgba(1, 1, 1, 0.30)
    cr.fill_preserve()
    cr.clip()
    if level > 0.01:
        top = y + h * (1 - level * 0.88)
        cr.move_to(x - 2, y + h + 2)
        steps = 12
        for i in range(steps + 1):
            px = x - 2 + (w + 4) * i / steps
            wy = top + math.sin(t * 5 + i * 0.9) * h * 0.035 + (i / steps - 0.5) * tilt * h * 0.5
            cr.line_to(px, wy)
        cr.line_to(x + w + 2, y + h + 2)
        cr.close_path()
        g = cairo.LinearGradient(0, top, 0, y + h)
        g.add_color_stop_rgba(0, 0.55, 0.88, 1.0, 0.95)
        g.add_color_stop_rgba(1, 0.12, 0.50, 0.95, 0.95)
        cr.set_source(g)
        cr.fill()
    cr.restore()
    glass_path(cr, x, y, w, h)
    cr.set_line_width(max(1.5, w * 0.07))
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.stroke()
    # shine
    cr.move_to(x + w * 0.22, y + h * 0.15)
    cr.line_to(x + w * 0.28, y + h * 0.75)
    cr.set_line_width(max(1.2, w * 0.06))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgba(1, 1, 1, 0.75)
    cr.stroke()


# --------------------------------------------------------------------------- #
# Scene: all animation logic, independent of the window
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
        self.W, self.H = W, H
        self.cfg = cfg
        self.compact = compact
        self.count = count
        self.goal = max(1, int(cfg.get("daily_goal_glasses", 8)))
        self.ml = int(cfg.get("glass_ml", 250))
        self.snooze = int(cfg.get("snooze_minutes", 10))
        self.auto_hide = float(cfg.get("auto_hide_minutes", 3)) * 60
        self.name = name
        self.tip = random.choice(TIPS)

        self.u = clamp(H / 1080, 0.85, 1.3) if not compact else 0.9
        self.R = 52 * self.u
        self.bw = 372 * self.u
        self.bh_est = 236 * self.u
        self.wave_h = 0 if compact else 70 * self.u

        self.t = 0.0
        self.state = "enter"
        self.state_t = 0.0
        self.roam_t = 0.0
        self.speed = 1.0
        self.result = None
        self.done = False

        self._setup_roam()
        sx, sy = self.roam_pos(0)
        self.start = (-self.R * 4, sy + 40 * self.u) if not compact else (sx, H + self.R * 3)
        self.pos = self.start
        self.prev = self.start
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
        self.bubble_bob = 0.0
        self._wave_spawn = 0.0
        self._trail_spawn = 0.0

    # -- motion ------------------------------------------------------------ #
    def _setup_roam(self):
        u, R = self.u, self.R
        xmin, xmax = self.bw / 2 + 24 * u, self.W - self.bw / 2 - 24 * u
        if xmin > xmax:
            xmin = xmax = self.W / 2
        ymin = 1.75 * R + 26 * u + self.bh_est + 16 * u
        ymax = self.H - R - self.wave_h - 34 * u
        if ymin > ymax:
            ymin = ymax
        self.cx, self.cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        self.ax, self.ay = (xmax - xmin) / 2, (ymax - ymin) / 2

    def roam_pos(self, rt):
        return (self.cx + self.ax * math.sin(0.16 * rt - 0.9),
                self.cy + self.ay * math.sin(0.27 * rt + 0.7))

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
        if self.compact:
            self.exit_to = (x, self.H + self.R * 4)
        elif self.result == "snooze":
            self.exit_to = (-self.R * 5, y + 60 * self.u)
        elif self.result == "drank":
            self.exit_to = (self.W + self.R * 5, y - 160 * self.u)
        else:
            self.exit_to = (self.W + self.R * 5, y + 40 * self.u)

    def poke(self):
        if self.jump_t < 0 and self.state == "idle":
            self.jump_t = 0.0
            self._splash(8, power=0.5)

    def _splash(self, n, power=1.0):
        x, y = self.pos
        for _ in range(n):
            a = random.uniform(-math.pi * 0.95, -math.pi * 0.05)
            sp = random.uniform(260, 620) * self.u * power
            self.particles.append(Particle("drop", x, y - self.R * 0.6, math.cos(a) * sp,
                                           math.sin(a) * sp, random.uniform(4, 8) * self.u,
                                           random.uniform(0.9, 1.5)))
        for _ in range(int(n * 0.4)):
            a = random.uniform(0, 2 * math.pi)
            sp = random.uniform(80, 260) * self.u * power
            self.particles.append(Particle("spark", x, y - self.R * 0.6, math.cos(a) * sp,
                                           math.sin(a) * sp - 120 * self.u, random.uniform(5, 10) * self.u,
                                           random.uniform(0.8, 1.4)))

    def body_offset(self):
        """Vertical offset from bobbing / jumping."""
        u, R = self.u, self.R
        off = math.sin(self.t * 2.4) * 5 * u
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
            if k >= 1 and not self.particles_alive():
                self.done = True
            if k >= 1:
                x, y = self.exit_to

        if dt > 0:
            vx, vy = (x - self.pos[0]) / dt, (y - self.pos[1]) / dt
            self.vel = (lerp(self.vel[0], vx, 0.2), lerp(self.vel[1], vy, 0.2))
        self.pos = (x, y)

        if self.jump_t >= 0:
            self.jump_t += dt
            if self.jump_t > 0.55:
                self.jump_t = -1.0

        # blinking
        self.blink_next -= dt
        if self.blink_next <= 0:
            self.blink_t = 0.0
            self.blink_next = random.uniform(2.2, 4.5)
        if self.blink_t >= 0:
            self.blink_t += dt
            if self.blink_t > 0.16:
                self.blink_t = -1.0

        # wave + bubble visibility
        wave_target = 0.0 if self.state == "exit" else 1.0
        self.wave_level += (wave_target - self.wave_level) * min(1.0, dt * 1.6)
        show_bubble = self.state in ("idle", "celebrate")
        if show_bubble:
            self.bubble_vis = min(1.0, self.bubble_vis + dt / 0.5)
        else:
            self.bubble_vis = max(0.0, self.bubble_vis - dt / 0.22)

        self._update_particles(dt)

    def particles_alive(self):
        return any(p.kind in ("drop", "spark") for p in self.particles)

    def _update_particles(self, dt):
        u = self.u
        # bubbles rising out of the wave
        if self.wave_h and self.wave_level > 0.3:
            self._wave_spawn += dt * 7 * self.wave_level
            while self._wave_spawn > 1:
                self._wave_spawn -= 1
                x = random.uniform(0, self.W)
                self.particles.append(Particle("bubble", x, self.H - random.uniform(5, 40) * u,
                                               0, -random.uniform(25, 60) * u,
                                               random.uniform(3, 9) * u, random.uniform(1.2, 2.6)))
        # trail bubbles behind the moving drop
        sp = math.hypot(*self.vel)
        if sp > 30 * u:
            self._trail_spawn += dt * min(18, sp / (14 * u))
            while self._trail_spawn > 1:
                self._trail_spawn -= 1
                x, y = self.pos
                nx, ny = -self.vel[0] / sp, -self.vel[1] / sp
                self.particles.append(Particle(
                    "bubble", x + nx * self.R * 1.1 + random.uniform(-8, 8) * u,
                    y + ny * self.R * 0.6 + random.uniform(-10, 10) * u,
                    nx * 20 * u, -random.uniform(20, 50) * u, random.uniform(3, 8) * u,
                    random.uniform(0.8, 1.6)))

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

    # -- hit testing ------------------------------------------------------- #
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

    # -- drawing ----------------------------------------------------------- #
    def draw(self, cr):
        if self.compact:
            g = cairo.LinearGradient(0, 0, 0, self.H)
            g.add_color_stop_rgb(0, 0.93, 0.97, 1.0)
            g.add_color_stop_rgb(1, 0.78, 0.89, 0.99)
            cr.set_source(g)
            cr.paint()
        self._draw_wave(cr)
        self._draw_particles(cr, ("bubble",))
        self._draw_drop(cr)
        self._draw_bubble(cr)
        self._draw_particles(cr, ("drop", "spark"))

    def _draw_wave(self, cr):
        if not self.wave_h or self.wave_level < 0.01:
            return
        W, H, t, u = self.W, self.H, self.t, self.u
        base = self.wave_h * ease_out_cubic(clamp(self.wave_level, 0, 1))
        layers = [
            (12 * u, 0.0060, 0.9, 18 * u, 0.30),
            (9 * u, 0.0085, -1.3, 0.0, 0.42),
        ]
        for amp, freq, spd, lift, alpha in layers:
            pts = []
            for x in range(-20, int(W) + 40, 16):
                y = (H - base - lift + amp * math.sin(x * freq + t * spd)
                     + amp * 0.45 * math.sin(x * freq * 2.3 - t * spd * 1.6))
                pts.append((x, y))
            cr.move_to(-20, H + 2)
            for p in pts:
                cr.line_to(*p)
            cr.line_to(W + 40, H + 2)
            cr.close_path()
            g = cairo.LinearGradient(0, H - base - lift - amp, 0, H)
            g.add_color_stop_rgba(0, 0.50, 0.85, 1.0, alpha)
            g.add_color_stop_rgba(1, 0.08, 0.40, 0.88, alpha + 0.2)
            cr.set_source(g)
            cr.fill()
            # foam line
            cr.move_to(*pts[0])
            for p in pts[1:]:
                cr.line_to(*p)
            cr.set_line_width(2 * u)
            cr.set_source_rgba(1, 1, 1, alpha * 1.3)
            cr.stroke()

    def _draw_particles(self, cr, kinds):
        for p in self.particles:
            if p.kind not in kinds:
                continue
            a = clamp(p.life / p.max_life, 0, 1)
            if p.kind == "bubble":
                fade = min(1.0, a * 3) * min(1.0, (p.max_life - p.life) * 4)
                cr.arc(p.x, p.y, p.r, 0, 2 * math.pi)
                cr.set_source_rgba(0.75, 0.92, 1, 0.18 * fade)
                cr.fill_preserve()
                cr.set_line_width(1.4 * self.u)
                cr.set_source_rgba(1, 1, 1, 0.75 * fade)
                cr.stroke()
                cr.arc(p.x - p.r * 0.35, p.y - p.r * 0.35, p.r * 0.25, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.9 * fade)
                cr.fill()
            elif p.kind == "drop":
                cr.save()
                cr.translate(p.x, p.y)
                cr.rotate(math.atan2(p.vy, p.vx) + math.pi / 2)
                s = p.r / 10
                cr.scale(s, s)
                drop_path(cr, 6)
                g = cairo.LinearGradient(-6, -10, 6, 6)
                g.add_color_stop_rgba(0, 0.65, 0.92, 1, a)
                g.add_color_stop_rgba(1, 0.12, 0.48, 0.95, a)
                cr.set_source(g)
                cr.fill()
                cr.restore()
            else:  # spark (4-point star)
                cr.save()
                cr.translate(p.x, p.y)
                cr.rotate(self.t * p.spin * 0.3)
                r = p.r * (0.6 + 0.4 * math.sin(self.t * 12 + p.seed))
                cr.move_to(0, -r)
                for i in range(1, 8):
                    rr = r if i % 2 == 0 else r * 0.32
                    ang = -math.pi / 2 + i * math.pi / 4
                    cr.line_to(math.cos(ang) * rr, math.sin(ang) * rr)
                cr.close_path()
                cr.set_source_rgba(1.0, 0.86, 0.35, a) if int(p.seed) % 2 else cr.set_source_rgba(1, 1, 1, a)
                cr.fill()
                cr.restore()

    def _draw_drop(self, cr):
        R, u, t = self.R, self.u, self.t
        x, y = self.pos
        oy = self.body_offset()
        vx, vy = self.vel
        tilt = clamp(vx * 0.0005 / u, -0.28, 0.28)
        squash = clamp(-vy * 0.0004 / u, -0.12, 0.12) + 0.035 * math.sin(t * 4.8)
        if self.jump_t >= 0:
            squash += 0.12 * math.cos(clamp(self.jump_t / 0.55, 0, 1) * math.pi * 2)

        # soft shadow / glow under the drop
        if not self.compact:
            cr.save()
            cr.translate(x, y + R * 1.35)
            cr.scale(1, 0.28)
            sh = cairo.RadialGradient(0, 0, 0, 0, 0, R * 1.1)
            k = clamp(1 + oy / (R * 2), 0.3, 1)
            sh.add_color_stop_rgba(0, 0.05, 0.25, 0.55, 0.22 * k)
            sh.add_color_stop_rgba(1, 0.05, 0.25, 0.55, 0)
            cr.set_source(sh)
            cr.arc(0, 0, R * 1.1, 0, 2 * math.pi)
            cr.fill()
            cr.restore()

        cr.save()
        cr.translate(x, y + oy)
        cr.rotate(tilt)
        cr.translate(0, R)  # squash from the bottom
        cr.scale(1 + squash * 0.7, 1 - squash)
        cr.translate(0, -R)

        # glow
        gl = cairo.RadialGradient(0, -0.3 * R, R * 0.6, 0, -0.3 * R, R * 2.2)
        gl.add_color_stop_rgba(0, 0.45, 0.80, 1.0, 0.30)
        gl.add_color_stop_rgba(1, 0.45, 0.80, 1.0, 0.0)
        cr.set_source(gl)
        cr.arc(0, -0.3 * R, R * 2.2, 0, 2 * math.pi)
        cr.fill()

        # body
        drop_path(cr, R)
        g = cairo.LinearGradient(-R, -1.75 * R, R, R)
        g.add_color_stop_rgb(0.0, 0.66, 0.92, 1.0)
        g.add_color_stop_rgb(0.5, 0.26, 0.66, 0.98)
        g.add_color_stop_rgb(1.0, 0.07, 0.37, 0.84)
        cr.set_source(g)
        cr.fill_preserve()
        shade = cairo.RadialGradient(0.35 * R, 0.55 * R, 0, 0.35 * R, 0.55 * R, R * 1.1)
        shade.add_color_stop_rgba(0, 0.02, 0.18, 0.55, 0.35)
        shade.add_color_stop_rgba(1, 0.02, 0.18, 0.55, 0.0)
        cr.set_source(shade)
        cr.fill_preserve()
        cr.set_line_width(2 * u)
        cr.set_source_rgba(0.04, 0.28, 0.70, 0.45)
        cr.stroke()

        # highlights
        cr.save()
        cr.translate(-0.45 * R, -0.35 * R)
        cr.rotate(0.35)
        cr.scale(0.17 * R, 0.40 * R)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.fill()
        cr.arc(-0.22 * R, -1.0 * R, 0.08 * R, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.fill()

        self._draw_face(cr)
        self._draw_arms(cr, back=True)
        self._draw_arms(cr, back=False)
        cr.restore()

    def _draw_arms(self, cr, back):
        R, t, u = self.R, self.t, self.u
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(0.17 * R)
        cr.set_source_rgb(0.16, 0.52, 0.93)
        if back:
            # left arm waving hello (or cheering)
            if self.state == "celebrate":
                ang = -2.1 + math.sin(t * 14) * 0.4
            elif self.state in ("enter", "idle"):
                ang = -2.5 + math.sin(t * 6) * 0.5
            else:
                ang = 2.6
            sx, sy = -0.8 * R, 0.2 * R
            ex, ey = sx + math.cos(ang) * 0.6 * R, sy + math.sin(ang) * 0.6 * R
            cr.move_to(sx, sy)
            cr.line_to(ex, ey)
            cr.stroke()
            cr.arc(ex, ey, 0.13 * R, 0, 2 * math.pi)
            cr.fill()
            return
        # right arm holding the glass
        gw, gh = 0.62 * R, 0.82 * R
        gx = 1.05 * R
        lift = 0.0
        if self.state == "celebrate":
            lift = -0.45 * R * min(1, self.state_t * 3)  # raise glass to "drink"
        gy = -0.3 * R + lift + math.sin(t * 2.4 + 1) * 2 * u
        cr.move_to(0.88 * R, 0.15 * R)
        cr.line_to(gx + 0.05 * R, gy + gh * 0.6)
        cr.stroke()
        draw_glass(cr, gx, gy, gw, gh, self.glass_level, t, tilt=math.sin(t * 2.4) * 0.3)
        cr.set_source_rgb(0.16, 0.52, 0.93)
        cr.arc(gx + 0.05 * R, gy + gh * 0.6, 0.13 * R, 0, 2 * math.pi)
        cr.fill()

    def _draw_face(self, cr):
        R = self.R
        dark = (0.06, 0.12, 0.28)
        ex, ey = 0.36 * R, -0.05 * R
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # cheeks
        for sx in (-1, 1):
            cr.save()
            cr.translate(sx * 0.62 * R, 0.24 * R)
            cr.scale(0.15 * R, 0.085 * R)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(1.0, 0.45, 0.62, 0.55)
            cr.fill()

        if self.face == "happy":
            cr.set_line_width(0.075 * R)
            cr.set_source_rgb(*dark)
            for sx in (-1, 1):
                cr.arc(sx * ex, ey + 0.06 * R, 0.14 * R, math.pi * 1.1, math.pi * 1.9)
                cr.stroke()
            # open smile
            cr.move_to(-0.24 * R, 0.2 * R)
            cr.curve_to(-0.2 * R, 0.55 * R, 0.2 * R, 0.55 * R, 0.24 * R, 0.2 * R)
            cr.close_path()
            cr.set_source_rgb(*dark)
            cr.fill_preserve()
            cr.save()
            cr.clip()
            cr.arc(0, 0.5 * R, 0.16 * R, 0, 2 * math.pi)
            cr.set_source_rgb(1.0, 0.48, 0.58)
            cr.fill()
            cr.restore()
            return

        if self.face == "sleepy":
            cr.set_line_width(0.07 * R)
            cr.set_source_rgb(*dark)
            for sx in (-1, 1):
                cr.arc(sx * ex, ey - 0.02 * R, 0.13 * R, math.pi * 0.15, math.pi * 0.85)
                cr.stroke()
            cr.arc(0, 0.3 * R, 0.06 * R, 0, 2 * math.pi)
            cr.fill()
            return

        # normal eyes that follow the mouse pointer
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
            cr.save()
            cr.translate(sx * ex, ey)
            cr.scale(1, blink)
            cr.save()
            cr.scale(0.2 * R, 0.25 * R)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgb(1, 1, 1)
            cr.fill()
            px, py = lookx * 0.07 * R, looky * 0.08 * R
            cr.arc(px, py + 0.02 * R, 0.13 * R, 0, 2 * math.pi)
            cr.set_source_rgb(*dark)
            cr.fill()
            cr.arc(px - 0.045 * R, py - 0.03 * R, 0.045 * R, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill()
            cr.restore()
        # smile
        cr.set_line_width(0.065 * R)
        cr.set_source_rgb(*dark)
        cr.arc(0, 0.16 * R, 0.17 * R, math.pi * 0.18, math.pi * 0.82)
        cr.stroke()

    def _draw_bubble(self, cr):
        if self.bubble_vis <= 0.001:
            if self.state != "idle":
                self.buttons = {}
            return
        u, R = self.u, self.R
        W, H = self.W, self.H
        x, y = self.pos
        oy = self.body_offset() * 0.35  # bubble follows bob gently
        pad = 20 * u
        bw = self.bw
        inner = bw - 2 * pad
        celebrate = self.state == "celebrate" or self.result == "drank"

        if celebrate:
            title = f"Great job, {self.name}!" if self.count < self.goal else "Daily goal reached!"
            sub = (f"+{self.ml} ml logged. Your body says thank you."
                   if self.count < self.goal else "You're a hydration hero today. Keep sipping!")
        else:
            title = f"Time to drink water, {self.name}!"
            sub = self.tip
        tl = make_layout(cr, title, 19 * u, bold=True, width=inner - 58 * u)
        sl = make_layout(cr, sub, 13.5 * u, width=inner)
        tw, th = tl.get_pixel_size()
        sw, sh = sl.get_pixel_size()
        prog_h = 24 * u
        btn_h = 0 if celebrate else 42 * u
        bh = pad + max(th, 26 * u) + 6 * u + sh + 14 * u + prog_h + (14 * u + btn_h if btn_h else 0) + pad

        bx = clamp(x - bw / 2, 14 * u, W - bw - 14 * u)
        drop_top = y + self.body_offset() - 1.75 * R
        by = drop_top - 24 * u - bh + oy
        below = by < 10 * u
        if below:
            by = y + R + 26 * u + oy
        by = clamp(by, 8 * u, H - bh - 8 * u)
        self.bubble_rect = (bx, by, bw, bh)

        tail_x = clamp(x, bx + 28 * u, bx + bw - 28 * u)
        tail_y = by if below else by + bh
        vis = self.bubble_vis
        sc = ease_out_back(vis) if self.state in ("idle", "celebrate") else vis
        alpha = clamp(vis * 1.5, 0, 1)

        cr.save()
        cr.translate(tail_x, tail_y)
        cr.scale(max(0.01, sc), max(0.01, sc))
        cr.translate(-tail_x, -tail_y)
        cr.push_group()

        # shadow
        for i in range(5):
            rounded_rect(cr, bx - i * 2, by + 8 * u - i * 2, bw + i * 4, bh + i * 4, 20 * u + i * 2)
            cr.set_source_rgba(0.04, 0.16, 0.36, 0.045)
            cr.fill()
        # body + tail
        rounded_rect(cr, bx, by, bw, bh, 20 * u)
        g = cairo.LinearGradient(0, by, 0, by + bh)
        g.add_color_stop_rgb(0, 1, 1, 1)
        g.add_color_stop_rgb(1, 0.94, 0.97, 1.0)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_line_width(1.5 * u)
        cr.set_source_rgba(0.55, 0.78, 1.0, 0.9)
        cr.stroke()
        tdir = -1 if below else 1
        cr.move_to(tail_x - 13 * u, tail_y - tdir * 2 * u)
        cr.line_to(tail_x + 13 * u, tail_y - tdir * 2 * u)
        cr.line_to(tail_x + (x - tail_x) * 0.3, tail_y + tdir * 17 * u)
        cr.close_path()
        cr.set_source_rgb(0.94, 0.97, 1.0) if not below else cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        cr.set_source_rgba(0.55, 0.78, 1.0, 0.9)
        cr.set_line_width(1.5 * u)
        cr.stroke()
        cr.move_to(tail_x - 12 * u, tail_y)
        cr.line_to(tail_x + 12 * u, tail_y)
        cr.set_line_width(3 * u)
        cr.set_source_rgb(0.94, 0.97, 1.0) if not below else cr.set_source_rgb(1, 1, 1)
        cr.stroke()

        # header icon (mini drop)
        cy = by + pad + max(th, 26 * u) / 2
        cr.save()
        cr.translate(bx + pad + 11 * u, cy + 4 * u)
        drop_path(cr, 8 * u)
        ig = cairo.LinearGradient(-8 * u, -14 * u, 8 * u, 8 * u)
        ig.add_color_stop_rgb(0, 0.6, 0.9, 1.0)
        ig.add_color_stop_rgb(1, 0.1, 0.45, 0.92)
        cr.set_source(ig)
        cr.fill()
        cr.restore()
        show_layout(cr, tl, bx + pad + 30 * u, cy - th / 2, (0.05, 0.18, 0.38, 1))

        # close (skip) button
        if not celebrate:
            ccx, ccy, cr_r = bx + bw - pad - 4 * u, by + pad + 6 * u, 12 * u
            hov = self.hover == "skip"
            cr.arc(ccx, ccy, cr_r, 0, 2 * math.pi)
            cr.set_source_rgba(0.85, 0.91, 0.98, 1 if hov else 0.0)
            cr.fill()
            cr.set_line_width(2 * u)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgba(0.35, 0.45, 0.58, 1 if hov else 0.7)
            k = 4.5 * u
            cr.move_to(ccx - k, ccy - k)
            cr.line_to(ccx + k, ccy + k)
            cr.move_to(ccx + k, ccy - k)
            cr.line_to(ccx - k, ccy + k)
            cr.stroke()
            self.buttons["skip"] = (ccx - cr_r - 4 * u, ccy - cr_r - 4 * u, 2 * cr_r + 8 * u, 2 * cr_r + 8 * u)

        yy = by + pad + max(th, 26 * u) + 6 * u
        show_layout(cr, sl, bx + pad, yy, (0.33, 0.42, 0.53, 1))
        yy += sh + 14 * u

        # progress: little glasses
        gw, gh_ = 14 * u, 20 * u
        gap = 6 * u
        n = self.goal if self.goal <= 12 else 12
        for i in range(n):
            gx = bx + pad + i * (gw + gap)
            filled = i < self.count
            cr.save()
            glass_path(cr, gx, yy + 2 * u, gw, gh_)
            cr.set_source_rgba(0.88, 0.94, 1.0, 1)
            cr.fill_preserve()
            if filled:
                cr.clip()
                fill_top = yy + 2 * u + gh_ * 0.22
                pop = 1.0
                if celebrate and i == self.count - 1:
                    pop = clamp(self.state_t / 0.6, 0, 1) if self.state == "celebrate" else 1
                cr.rectangle(gx, fill_top + (1 - pop) * gh_, gw, gh_)
                fg = cairo.LinearGradient(0, fill_top, 0, yy + 2 * u + gh_)
                fg.add_color_stop_rgb(0, 0.45, 0.82, 1.0)
                fg.add_color_stop_rgb(1, 0.12, 0.48, 0.95)
                cr.set_source(fg)
                cr.fill()
            cr.restore()
            glass_path(cr, gx, yy + 2 * u, gw, gh_)
            cr.set_line_width(1.5 * u)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.set_source_rgba(0.30, 0.58, 0.92, 1 if filled else 0.55)
            cr.stroke()
        extra = f"  +{self.count - self.goal}" if self.count > self.goal else ""
        vol = self.count * self.ml
        vol_s = f"{vol} ml" if vol < 1000 else f"{vol / 1000:.2f}".rstrip("0").rstrip(".") + " L"
        pl = make_layout(cr, f"{self.count}/{self.goal}{extra}  ·  {vol_s}",
                         12.5 * u, bold=True)
        pw_, ph_ = pl.get_pixel_size()
        show_layout(cr, pl, bx + bw - pad - pw_, yy + 2 * u + (gh_ - ph_) / 2, (0.18, 0.45, 0.82, 1))
        yy += prog_h + 14 * u

        # buttons
        if btn_h:
            prim_w = inner * 0.58
            sec_w = inner - prim_w - 10 * u
            self._button(cr, "drank", bx + pad, yy, prim_w, btn_h, "I drank a glass", True)
            self._button(cr, "snooze", bx + pad + prim_w + 10 * u, yy, sec_w, btn_h,
                         f"Snooze {self.snooze} min", False)

        cr.pop_group_to_source()
        cr.paint_with_alpha(alpha)
        cr.restore()

    def _button(self, cr, bid, x, y, w, h, label, primary):
        u = self.u
        hov = self.hover == bid
        lift = -1.5 * u if hov else 0
        y += lift
        if primary:
            rounded_rect(cr, x, y + 3 * u, w, h, h / 2)
            cr.set_source_rgba(0.1, 0.4, 0.85, 0.25)
            cr.fill()
            rounded_rect(cr, x, y, w, h, h / 2)
            g = cairo.LinearGradient(x, y, x + w, y + h)
            if hov:
                g.add_color_stop_rgb(0, 0.36, 0.78, 1.0)
                g.add_color_stop_rgb(1, 0.16, 0.52, 1.0)
            else:
                g.add_color_stop_rgb(0, 0.28, 0.70, 1.0)
                g.add_color_stop_rgb(1, 0.10, 0.44, 0.94)
            cr.set_source(g)
            cr.fill()
            fg = (1, 1, 1, 1)
        else:
            rounded_rect(cr, x, y, w, h, h / 2)
            cr.set_source_rgb(*((0.84, 0.92, 1.0) if hov else (0.90, 0.95, 1.0)))
            cr.fill_preserve()
            cr.set_line_width(1.2 * u)
            cr.set_source_rgba(0.55, 0.75, 0.98, 1)
            cr.stroke()
            fg = (0.12, 0.40, 0.80, 1)
        lay = make_layout(cr, label, (15 if primary else 13.5) * u, bold=primary)
        lw, lh = lay.get_pixel_size()
        show_layout(cr, lay, x + (w - lw) / 2, y + (h - lh) / 2, fg)
        self.buttons[bid] = (x, y - lift, w, h)


# --------------------------------------------------------------------------- #
# GTK overlay window
# --------------------------------------------------------------------------- #
class Overlay(Gtk.Window):
    def __init__(self, cfg, count, on_done):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.on_done = on_done
        display = Gdk.Display.get_default()
        self.pointer_dev = display.get_default_seat().get_pointer()
        _, px, py = self.pointer_dev.get_position()
        monitor = display.get_monitor_at_point(px, py) or display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        composited = visual is not None and screen.is_composited()
        if composited:
            self.set_visual(visual)
            W, H, ox, oy = geo.width, geo.height, geo.x, geo.y
        else:  # no compositor: small opaque card in the bottom-right corner
            W, H = 420, 400
            ox, oy = geo.x + geo.width - W - 24, geo.y + geo.height - H - 64
        self.origin = (ox, oy)
        self.compact = not composited

        self.set_app_paintable(True)
        self.set_title("Water Reminder")
        self.set_default_size(W, H)
        self.move(ox, oy)
        self.resize(W, H)
        self.scene = Scene(W, H, cfg, count, user_first_name(cfg), compact=self.compact)

        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_click)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("realize", lambda *_: self._update_input_shape())
        self._cursor_hand = Gdk.Cursor.new_from_name(display, "pointer")
        self._cursor_cur = None
        self.show_all()
        self._last = time.monotonic()
        self._finished = False
        GLib.timeout_add(16, self._tick)

    def _on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        self.scene.draw(cr)
        return True

    def _on_motion(self, _w, ev):
        self._set_hover(ev.x, ev.y)

    def _set_hover(self, x, y):
        h = self.scene.hit(x, y)
        self.scene.hover = h
        want = self._cursor_hand if h else None
        if want is not self._cursor_cur and self.get_window():
            self.get_window().set_cursor(want)
            self._cursor_cur = want

    def _on_click(self, _w, ev):
        if ev.button != 1:
            return False
        h = self.scene.hit(ev.x, ev.y)
        if h == "drop":
            self.scene.poke()
        elif h in ("drank", "snooze", "skip"):
            if h == "drank":
                play_sound("done") if self.scene.cfg.get("sound", True) else None
            self.scene.leave(h)
        return True

    def _tick(self):
        now = time.monotonic()
        dt = min(0.05, now - self._last)
        self._last = now
        try:
            _, px, py = self.pointer_dev.get_position()
            p = (px - self.origin[0], py - self.origin[1])
            if 0 <= p[0] <= self.scene.W and 0 <= p[1] <= self.scene.H:
                self.scene.pointer = p
                self._set_hover(*p)
        except Exception:
            pass
        self.scene.update(dt)
        if self.scene.done:
            self._finish()
            return False
        self._update_input_shape()
        self.queue_draw()
        return True

    def _update_input_shape(self):
        if self.compact:
            return
        rects = [cairo.RectangleInt(int(x), int(y), int(w) + 1, int(h) + 1)
                 for (x, y, w, h) in self.scene.hit_rects(pad=4)]
        try:
            self.input_shape_combine_region(cairo.Region(rects))
        except Exception:
            pass

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        result = self.scene.result or "timeout"
        self.destroy()
        self.on_done(result)


# --------------------------------------------------------------------------- #
# Background service
# --------------------------------------------------------------------------- #
class ReminderService:
    def __init__(self, once=False):
        self.once = once
        self.timer = None
        self.overlay = None

    def schedule(self, minutes):
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add_seconds(max(1, int(minutes * 60)), self._fire)
        when = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        print(f"[{APP}] next reminder at {when:%H:%M}", flush=True)

    def _fire(self, force=False):
        self.timer = None
        cfg = load_config()
        if not force and in_quiet_hours(cfg):
            self.schedule(cfg["interval_minutes"])
            return False
        self.show(cfg)
        return False

    def show(self, cfg=None):
        if self.overlay is not None:
            return
        cfg = cfg or load_config()
        st = load_state()
        if cfg.get("sound", True):
            play_sound("reminder")
        self.overlay = Overlay(cfg, st["count"], self._done)

    def show_now(self, *_):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        self._fire(force=True)
        return True  # keep signal handler installed

    def _done(self, result):
        self.overlay = None
        cfg = load_config()
        if result == "drank":
            st = load_state()
            st["count"] += 1
            save_state(st)
        if self.once:
            Gtk.main_quit()
            return
        self.schedule(cfg["snooze_minutes"] if result == "snooze" else cfg["interval_minutes"])


def read_pid():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def run_daemon():
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[{APP}] already running.")
        return 0
    cfg = load_config()
    svc = ReminderService()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, svc.show_now)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (Gtk.main_quit(), False)[1])
    # publish the pid only once signal handlers exist (so --now can't kill us)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    print(f"[{APP}] running - every {cfg['interval_minutes']} min "
          f"(quiet {cfg['quiet_hours']['start']}-{cfg['quiet_hours']['end']})", flush=True)
    svc.schedule(cfg["interval_minutes"])
    Gtk.main()
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser(description="Animated water reminder")
    ap.add_argument("--now", action="store_true", help="show the reminder right now")
    ap.add_argument("--status", action="store_true", help="print today's progress")
    ap.add_argument("--stop", action="store_true", help="stop the background reminder")
    args = ap.parse_args()

    if args.status:
        cfg, st = load_config(), load_state()
        pid = read_pid()
        print(f"Today: {st['count']}/{cfg['daily_goal_glasses']} glasses "
              f"({st['count'] * cfg['glass_ml']} ml)")
        print(f"Reminder service: {'running (pid %d)' % pid if pid else 'not running'}")
        print(f"Interval: every {cfg['interval_minutes']} min   Config: {CONFIG_FILE}")
        last = sorted(st["history"].items())[-7:]
        if last:
            print("Last days: " + "  ".join(f"{d[5:]}:{c}" for d, c in last))
        return 0
    if args.stop:
        pid = read_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            print("Stopped.")
        else:
            print("Not running.")
        return 0
    if args.now:
        pid = read_pid()
        if pid:
            os.kill(pid, signal.SIGUSR1)
            return 0
        svc = ReminderService(once=True)
        svc.show()
        Gtk.main()
        return 0
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
