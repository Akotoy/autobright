#!/usr/bin/env python3
"""Webcam-based automatic screen brightness for Linux.

The webcam is used as an ambient light sensor: every so often it grabs a few
tiny frames with *manual* exposure, auto-ranges the exposure until the wall
behind you (top band of the picture) is mid-grey, and derives scene brightness
as signal / exposure time. That number (log scale) is mapped through a curve to
a screen brightness.

On GNOME 49+ the level goes to org.gnome.Shell.Brightness.SetAutoBrightnessTarget
— the same hook GNOME uses for hardware light sensors. Auto brightness is in
charge: moving the GNOME slider by hand lasts only until the next reading (and
repeated corrections turn into a "remember this?" suggestion).
Elsewhere it falls back to systemd-logind's Session.SetBrightness (no root needed).

Commands:
  run      the daemon
  measure  take one reading and print it (use it to tune the curve)
  probe    show camera formats and controls
  off      hand brightness back to manual control
"""

import argparse
import errno
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import Counter

import v4l2cam as v

CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                           "autobright", "config.toml")
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "autobright")
STATUS_PATH = os.path.join(RUNTIME_DIR, "status.json")
# The settings window writes a deadline here while you preview a brightness with its slider.
PREVIEW_PATH = os.path.join(RUNTIME_DIR, "preview_until")
# ...and a one-word request here (then SIGUSR1), e.g. "calibrate-glow".
COMMAND_PATH = os.path.join(RUNTIME_DIR, "command")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "autobright")
CORRECTIONS_PATH = os.path.join(STATE_DIR, "corrections.json")
GAIN_CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                          "autobright", "gain.json")
LIVE_SECONDS = 20
METRIC = "roi-iqm"   # what the curve's x axis is measured with (see roi_stats)

DEFAULTS = {
    "device": "",               # empty = first capture device with YUYV
    "interval": 30.0,           # seconds between readings while the light changes
    "max_interval": 180.0,      # ...stretching up to this while it stays the same
    "fast_interval": 8.0,       # after a big change, re-check sooner
    "width": 160, "height": 120,
    "roi_top": 0.35,            # measure the top part of the picture: wall/ceiling, not you
    "black_level": 12.0,        # sensor luma at zero light (see `probe`)
    "target_luma": 55.0,        # auto-ranging aims for this luma (keep it in
                                # the camera's linear range, roughly 35..90)
    "max_exposure_ms": 150.0,   # longer = slower startup; darker rooms all read as "dark"
    "use_gain": True,           # max sensor gain + short exposure = camera (and LED) on for less time
    # Curve: [log10(ambient), brightness 0..1], piecewise linear, sorted by x.
    # `measure` prints log10(ambient) so you can place your own points.
    "curve": [[1.0, 0.05], [2.5, 0.50], [4.0, 0.85], [5.0, 1.00]],
    "curve_metric": "mean",     # metric the curve was made with; migrated to METRIC automatically
    "screen_glow": 0.0,         # how much the screen itself lights what the camera sees (calibrated)
    "smoothing": 0.5,           # EMA weight of the newest reading (log domain)
    "hysteresis": 0.03,         # ignore target changes smaller than this
    "transition": 2.5,          # seconds to ease between levels
    "pause_when_locked": True,
    "pause_when_away": True,    # no readings while nobody touches mouse/keyboard (video excepted)
    "away_after": 120.0,
    "suggest": True,            # offer to remember repeated manual corrections
    "kbd_auto": False,          # keyboard backlight on in the dark
    "kbd_level": 33,            # percent, as GNOME's keyboard brightness steps (33/66/100)
    "kbd_dark_below": 1.8,      # log10(ambient) under which the room counts as dark
}

log_verbose = False


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def debug(*a):
    if log_verbose:
        log(*a)


def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, "rb") as f:
            user = tomllib.load(f)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            log("config: ignoring unknown keys:", ", ".join(sorted(unknown)))
        cfg.update({k: user[k] for k in user if k in DEFAULTS})
    cfg["curve"] = sorted((float(x), float(y)) for x, y in cfg["curve"])
    return cfg


def save_config(cfg, path):
    """Write the settings that differ from the defaults (plus the curve) as TOML."""
    def fmt(val):
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, str):
            return json.dumps(val)
        if isinstance(val, (list, tuple)):
            return "[" + ", ".join(fmt(x) for x in val) + "]"
        return repr(round(val, 4) if isinstance(val, float) else val)

    lines = ["# autobright settings — written by the settings window, safe to edit by hand.", ""]
    for key, default in DEFAULTS.items():
        val = cfg.get(key, default)
        if key == "curve":
            val = [[round(x, 3), round(y, 3)] for x, y in val]
        elif val == default:
            continue
        lines.append(f"{key} = {fmt(val)}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


# --- pure helpers (unit-tested) ---------------------------------------------------

def curve_target(curve, x):
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def roi_stats(frames, width, height, roi_top):
    """(robust luma of the top band, its saturated share, full-frame mean luma).

    The top band is usually wall or ceiling behind you; the rest is mostly your
    face and clothes, which would make a white T-shirt read as a brighter room.
    "Robust" = interquartile mean: like a median (ignores a lamp or a dark
    doorway in view) but without the median's 1-level steps.
    """
    rows = max(1, min(height, int(round(height * roi_top))))
    hist = Counter()
    full_total = full_n = 0
    for y in frames:
        hist.update(y[:width * rows])
        full_total += sum(y)
        full_n += len(y)
    n = sum(hist.values())
    lo_cut, hi_cut = n * 0.25, n * 0.75
    acc = total = count = 0.0
    for level in range(256):
        c = hist.get(level, 0)
        if not c:
            continue
        take = max(0.0, min(acc + c, hi_cut) - max(acc, lo_cut))
        total += take * level
        count += take
        acc += c
    iqm = total / count if count else 0.0
    sat = (hist.get(255, 0) + hist.get(254, 0)) / n if n else 0.0
    return iqm, sat, full_total / full_n if full_n else 0.0


def remove_glow(ambient, level, k):
    """Subtract the screen's own light (k per unit of brightness) from a reading.

    Never removes more than 95%: a reading is never pure screen glow, and log10
    needs something positive.
    """
    return max(ambient - k * level, ambient * 0.05)


def migrate_curve(curve, shift):
    return [(round(x + shift, 3), y) for x, y in curve]


def next_interval(prev, change, cfg):
    """Seconds until the next reading.

    Light steady (change < 0.05 in log10) -> stretch 1.5x per reading up to
    max_interval: fewer LED blinks. Big change -> look again soon. Else the base.
    """
    base = cfg["interval"]
    if change is None:
        return base
    if abs(change) > 0.15:
        return min(cfg["fast_interval"], base)
    if abs(change) < 0.05:
        # stretch from at least the base (not from a short test-mode interval)
        return min(max(prev or base, base) * 1.5, max(cfg["max_interval"], base))
    return base


def suggest(corrections, curve, now, min_count=3, window=0.3, min_diff=0.08, max_age=14 * 86400):
    """Find a repeated manual correction worth remembering.

    corrections: [(time, light_log, chosen_brightness)]. Returns
    (light_log, brightness, direction, matched_events) or None, where direction is
    +1 if you keep making it brighter than the curve, -1 if darker.
    """
    recent = [c for c in corrections if now - c[0] <= max_age]
    for _, x, _ in recent:
        group = [c for c in recent if abs(c[1] - x) <= window]
        if len(group) < min_count:
            continue
        diffs = [b - curve_target(curve, lx) for _, lx, b in group]
        if all(d > min_diff for d in diffs) or all(d < -min_diff for d in diffs):
            xs = sorted(c[1] for c in group)
            bs = sorted(c[2] for c in group)
            return xs[len(xs) // 2], bs[len(bs) // 2], 1 if diffs[0] > 0 else -1, group
    return None


def add_curve_point(curve, x, y, near=0.25):
    pts = [p for p in curve if abs(p[0] - x) > near]
    pts.append((round(x, 2), round(min(max(y, 0.0), 1.0), 3)))
    return sorted(pts)


# --- light meter -------------------------------------------------------------

class NoCamera(Exception):
    """No webcam right now (privacy key, unplugged, driver reload)."""


class LightMeter:
    """Webcam as a light meter.

    The camera LED is on for as long as the camera streams, and a webcam frame
    lasts as long as its exposure. So readings run at maximum sensor gain with a
    short exposure. Gain multiplies the signal by a camera-specific factor; it is
    measured once (gain off vs. gain max on the same scene), cached, and divided
    out, so readings stay on the same scale as without gain.
    """
    SETTLE_FRAMES = 3   # UVC cams apply a new exposure/gain a few frames late
    AVG_FRAMES = 2
    MAX_STEPS = 6

    def __init__(self, cfg):
        self.cfg = cfg
        self.device = None    # resolved on every reading: cameras come and go (privacy key, USB)
        self.reset()

    def reset(self):
        """Forget learned exposures (after a config change)."""
        self.exposure = {}    # per gain, remembered so we usually converge in one step

    def _resolve(self):
        want = self.cfg["device"]
        if want and os.path.exists(want):
            return want
        if self.device and not want and os.path.exists(self.device):
            return self.device
        found = v.find_camera()   # configured one is gone (renumbered?) or auto mode
        if not found:
            raise NoCamera()
        return found

    # -- gain calibration cache
    def _load_factor(self, card):
        try:
            with open(GAIN_CACHE) as f:
                return json.load(f).get(card)
        except (OSError, ValueError):
            return None

    def _save_factor(self, card, factor):
        try:
            with open(GAIN_CACHE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data[card] = factor
        os.makedirs(os.path.dirname(GAIN_CACHE), exist_ok=True)
        with open(GAIN_CACHE, "w") as f:
            json.dump(data, f)

    # -- frames
    def _frames(self, cam, settle, avg):
        for _ in range(settle):
            cam.grab()
        frames = [cam.grab() for _ in range(avg)]
        return roi_stats(frames, cam.width, cam.height, self.cfg["roi_top"])

    def read(self):
        """Return (ambient, details). ambient = luma-above-black per second of exposure at gain 1."""
        cfg = self.cfg
        self.device = self._resolve()
        try:
            return self._read(cfg)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.ENOENT, errno.ENXIO):
                raise NoCamera() from e   # unplugged / switched off mid-reading
            raise

    def _read(self, cfg):
        with v.Camera(self.device) as cam:
            try:
                cam.configure(cfg["width"], cfg["height"])
            except OSError as e:
                if e.errno == errno.EBUSY:
                    raise v.CameraBusy(self.device) from e
                raise
            q = cam.query(v.CID_EXPOSURE_ABSOLUTE)
            if q is None or not cam.set(v.CID_EXPOSURE_AUTO, v.EXPOSURE_MANUAL):
                raise RuntimeError("camera has no manual exposure control")
            emin = max(q.minimum, 1)
            emax = max(emin, min(q.maximum, int(cfg["max_exposure_ms"] * 10)))
            cam.set(v.CID_EXPOSURE_AUTO_PRIORITY, 0)
            cam.set(v.CID_BACKLIGHT_COMPENSATION, 0)

            card = cam.capability().card.decode(errors="replace")
            g = cam.query(v.CID_GAIN)
            can_gain = bool(cfg["use_gain"] and g and g.maximum > g.minimum)
            factor = self._load_factor(card) if can_gain else None
            gain = g.maximum if factor else (g.minimum if g else None)
            if gain is not None:
                cam.set(v.CID_GAIN, gain)

            exp = min(max(self.exposure.get(gain) or q.default_value, emin), emax)
            cam.set(v.CID_EXPOSURE_ABSOLUTE, exp)
            cam.start()

            # Webcams have a toe and a knee; only the middle of the range is close
            # to linear, so insist on landing near the target before trusting it.
            lo, hi = cfg["target_luma"] * 0.75, min(cfg["target_luma"] * 1.5, 200)
            black = cfg["black_level"]
            for step in range(self.MAX_STEPS):
                # A new exposure lands a few frames late — even one set before
                # STREAMON sometimes shows up only after the first frames.
                luma, sat_frac, full = self._frames(cam, self.SETTLE_FRAMES, self.AVG_FRAMES)
                debug(f"  step {step}: gain {gain} exposure {exp} -> luma {luma:.1f} "
                      f"(full frame {full:.1f}), saturated {sat_frac:.1%}")

                too_bright = luma > hi or sat_frac > 0.05
                if (lo <= luma and not too_bright) or (luma < lo and exp >= emax) \
                        or (too_bright and exp <= emin):
                    break
                ratio = (cfg["target_luma"] - black) / max(luma - black, 0.5)
                if too_bright:
                    ratio = min(ratio, 0.5)
                new = int(min(max(exp * min(max(ratio, 1 / 16), 16), emin), emax))
                if new == exp:
                    break
                exp = new
                cam.set(v.CID_EXPOSURE_ABSOLUTE, exp)
            self.exposure[gain] = exp
            in_band = lo <= luma <= hi and sat_frac <= 0.05

            # One-time gain calibration: same scene at max gain and a shorter exposure.
            if can_gain and not factor and in_band:
                e2 = max(emin, exp // 4)
                cam.set(v.CID_GAIN, g.maximum)
                cam.set(v.CID_EXPOSURE_ABSOLUTE, e2)
                m2, s2, _ = self._frames(cam, self.SETTLE_FRAMES + 1, 3)
                if lo * 0.5 <= m2 <= hi * 1.3 and s2 <= 0.02:
                    f = ((m2 - black) / e2) / ((luma - black) / exp)
                    if 1.0 <= f <= 64:
                        self._save_factor(card, round(f, 3))
                        self.exposure[g.maximum] = e2
                        log(f"gain calibrated: max gain = x{f:.2f} (camera on for less time from now on)")

        scale = exp * 1e-4 * (factor or 1.0)   # exposure unit is 100 µs
        ambient = max(luma - black, 0.5) / scale
        ambient_full = max(full - black, 0.5) / scale
        return ambient, {"exposure": exp, "luma": luma, "saturated": sat_frac, "gain": gain,
                         "ambient_full": ambient_full}


# --- brightness backends -----------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


class GnomeBackend:
    """GNOME 49+: drive the shell's auto-brightness target.

    GNOME applies its own brightness slider as an offset on top of that target:
    screen = target + (slider - 50%). We never touch the slider (GNOME applies
    slider and target asynchronously, so moving both at once flickers). Instead
    the offset is tracked and compensated: set(level) sends target = level - offset.
    Moving the slider by hand therefore only changes the offset, which the
    daemon notices and compensates at the next reading — auto brightness stays
    in charge, without any jumps.

    Uses one persistent D-Bus connection (python3-gi, part of every GNOME
    install) — fast enough to animate brightness at 60 fps.
    """
    name = "gnome"
    MUTTER = ("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig")
    GSD_POWER = ("org.gnome.SettingsDaemon.Power", "/org/gnome/SettingsDaemon/Power")
    KBD = "org.gnome.SettingsDaemon.Power.Keyboard"

    def __init__(self):
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.offset = 0.0      # GNOME slider offset, inferred
        self.sent = None       # last target sent (None = auto brightness off)

    @classmethod
    def available(cls):
        try:
            return bool(cls()._call("org.gnome.Shell", "/org/gnome/Shell/Brightness",
                                    "org.freedesktop.DBus.Properties", "Get",
                                    ("(ss)", ("org.gnome.Shell.Brightness", "HasBrightnessControl")))[0])
        except Exception:  # noqa: BLE001 — no gi, no GNOME, old GNOME: all mean "not available"
            return False

    def _call(self, dest, path, iface, method, args=None):
        params = self.GLib.Variant(*args) if args else None
        try:
            res = self.bus.call_sync(dest, path, iface, method, params, None,
                                     self.Gio.DBusCallFlags.NONE, 3000, None)
        except self.GLib.Error as e:
            raise RuntimeError(e.message) from e
        return res.unpack() if res else ()

    def _send(self, target):
        self._call("org.gnome.Shell", "/org/gnome/Shell/Brightness", "org.gnome.Shell.Brightness",
                   "SetAutoBrightnessTarget", ("(d)", (float(target),)))
        self.sent = None if target < 0 else target

    def _backlight(self):
        """(serial, connector, min, max) of the built-in panel."""
        (serial, monitors), = self._call(*self.MUTTER, "org.freedesktop.DBus.Properties", "Get",
                                         ("(ss)", ("org.gnome.Mutter.DisplayConfig", "Backlight")))
        for mon in monitors:
            if mon.get("active"):
                return serial, mon["connector"], mon["min"], mon["max"]
        raise RuntimeError("no active backlight in Mutter")

    def actual(self):
        """Brightness on screen, 0..1 on GNOME's scale.

        Read from sysfs: Mutter's Backlight property only follows manual changes,
        not ones made through the auto-brightness target.
        """
        _, _, lo, hi = self._backlight()
        dev = sorted(os.listdir("/sys/class/backlight"))[0]
        raw = int(open(f"/sys/class/backlight/{dev}/brightness").read())
        return min(max((raw - lo) / (hi - lo), 0.0), 1.0)

    def set(self, level):
        target = level - self.offset
        if not 0.0 <= target <= 1.0:
            # The slider is so far off that compensation can't reach this level.
            # Put the slider back to the middle once (a single small jump).
            serial, connector, lo, hi = self._backlight()
            self._call(*self.MUTTER, "org.gnome.Mutter.DisplayConfig", "SetBacklight",
                       ("(usi)", (serial, connector, round(lo + (hi - lo) / 2))))
            self.offset, target = 0.0, level
        self._send(min(max(target, 0.0), 1.0))

    def take_over(self, shown):
        """Start driving the screen without changing what's on it."""
        if self.sent is None:            # manual mode: screen == slider level
            self.offset = shown - 0.5
            self._send(0.5)               # = shown - offset: no visible change
        else:
            self.notice_manual(shown)

    def notice_manual(self, shown):
        """The screen shows `shown` although we set something else: the slider moved.

        Re-send the current target and measure the offset from the result: a slider
        or brightness-key change already shows target + offset, while a direct
        backlight write by another program only takes the offset form after GNOME
        re-applies the target. Returns what is on screen now.
        """
        if self.sent is None:
            self.take_over(shown)
            return shown
        self._send(self.sent)
        time.sleep(0.06)
        now = self.actual()
        self.offset = now - self.sent   # if clamped at 0/1, the next reading refines it
        return now

    def locked(self):
        try:
            return bool(self._call("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                   "org.gnome.ScreenSaver", "GetActive")[0])
        except RuntimeError:
            return False

    def idle(self):
        """Seconds since the last keyboard/mouse input."""
        try:
            return self._call("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                              "org.gnome.Mutter.IdleMonitor", "GetIdletime")[0] / 1000
        except RuntimeError:
            return 0.0

    def idle_inhibited(self):
        """True while something (a video player, a call) keeps the session from going idle."""
        try:
            return bool(self._call("org.gnome.SessionManager", "/org/gnome/SessionManager",
                                   "org.gnome.SessionManager", "IsInhibited", ("(u)", (8,)))[0])
        except RuntimeError:
            return False

    # keyboard backlight, through GNOME's own power daemon (percent, snapped to its steps)
    def kbd_get(self):
        return int(self._call(*self.GSD_POWER, "org.freedesktop.DBus.Properties", "Get",
                              ("(ss)", (self.KBD, "Brightness")))[0])

    def kbd_set(self, percent):
        self._call(*self.GSD_POWER, "org.freedesktop.DBus.Properties", "Set",
                   ("(ssv)", (self.KBD, "Brightness", self.GLib.Variant("i", int(percent)))))

    def manual_level(self):
        """Where the screen will be once auto brightness lets go."""
        return min(max(0.5 + self.offset, 0.0), 1.0)

    def hand_back(self):
        self._send(-1.0)


class LogindBackend:
    """Any desktop: write the backlight through systemd-logind (no root)."""
    name = "logind"
    offset = 0.0

    def __init__(self):
        devs = sorted(os.listdir("/sys/class/backlight")) if os.path.isdir("/sys/class/backlight") else []
        if not devs:
            raise SystemExit("no backlight device in /sys/class/backlight")
        self.dev = devs[0]
        self.base = f"/sys/class/backlight/{self.dev}"
        self.max = int(open(f"{self.base}/max_brightness").read())
        self.last = None

    def _write(self, value):
        r = _run(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                  "org.freedesktop.login1.Session", "SetBrightness", "ssu", "backlight", self.dev, str(value)])
        if r.returncode:
            raise RuntimeError(r.stderr.strip())

    def set(self, target):
        self.last = target
        self._write(max(1, round(target * self.max)))

    def actual(self):
        return int(open(f"{self.base}/brightness").read()) / self.max

    def take_over(self, shown):
        pass  # writes are absolute: just continue from what's on screen

    def notice_manual(self, shown):
        return shown

    def locked(self):
        return screen_locked()

    def idle(self):
        return idle_seconds()

    def idle_inhibited(self):
        return False

    def manual_level(self):
        return self.actual()

    def hand_back(self):
        pass  # the screen keeps whatever auto brightness set last


def on_gnome():
    if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return True
    return _run(["pgrep", "-x", "gnome-shell"]).returncode == 0


def pick_backend(wait=0.0):
    """GNOME's own brightness control if present, else logind.

    At login this service can start before GNOME Shell exports its brightness
    interface; on GNOME wait for it instead of silently falling back to logind,
    which would fight the shell over the backlight.
    """
    deadline = time.monotonic() + wait
    gnome = on_gnome()
    while True:
        if GnomeBackend.available():
            return GnomeBackend()
        if not gnome or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if gnome:
        log("GNOME brightness control not available, falling back to logind")
    return LogindBackend()


def idle_seconds():
    r = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
              "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
              "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
    m = re.search(r"(\d+)", r.stdout)
    return int(m.group(1)) / 1000 if r.returncode == 0 and m else 0.0


def screen_locked():
    r = _run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
              "--object-path", "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive"])
    return r.returncode == 0 and "true" in r.stdout


def preview_active():
    try:
        return time.time() < float(open(PREVIEW_PATH).read())
    except (OSError, ValueError):
        return False


def take_command():
    """A one-shot request from the settings window, consumed on read."""
    try:
        with open(COMMAND_PATH) as f:
            cmd = f.read().strip()
        os.remove(COMMAND_PATH)
        return cmd
    except OSError:
        return None


# --- smooth brightness ---------------------------------------------------------

class Fader(threading.Thread):
    """Moves the brightness toward a goal at 60 fps.

    Motion is a critically damped spring in perceptual space (lightness ~
    brightness^(1/2.2)): it starts gently, glides, and settles without
    overshoot, and dark-end changes don't look abrupt. A new goal mid-way keeps
    the current speed, so successive readings never produce a staircase or a kink.
    """
    FPS = 60
    GAMMA = 2.2

    def __init__(self, backend):
        super().__init__(daemon=True, name="fader")
        self.backend = backend
        self.value = None      # last level actually sent
        self.speed = 0.0       # perceptual units per second
        self.goal = None
        self.duration = 2.0
        self._cv = threading.Condition()
        self._stopping = False

    def go(self, goal, duration):
        with self._cv:
            self.goal, self.duration = float(goal), max(float(duration), 0.0)
            self._cv.notify()

    def adopt(self, value):
        """The screen is already at `value` (set elsewhere): continue from there."""
        with self._cv:
            self.value = self.goal = float(value)
            self.speed = 0.0

    def idle(self):
        return self.value is not None and self.goal is not None and abs(self.value - self.goal) < 1e-3

    def wait_idle(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.idle() and time.monotonic() < deadline:
            time.sleep(0.02)

    def stop(self):
        with self._cv:
            self._stopping = True
            self._cv.notify()
        self.join(timeout=2)

    def run(self):
        dt = 1 / self.FPS
        while True:
            with self._cv:
                while not self._stopping and (self.goal is None or
                                              (self.value is not None and self.value == self.goal)):
                    self._cv.wait()
                if self._stopping:
                    return
                goal, duration = self.goal, self.duration
                if self.value is None or duration <= 0:
                    new, self.speed = goal, 0.0
                else:
                    w = 5.8 / duration        # critically damped: ~98% settled after `duration`
                    p, pg = self.value ** (1 / self.GAMMA), goal ** (1 / self.GAMMA)
                    self.speed += (w * w * (pg - p) - 2 * w * self.speed) * dt
                    p = min(max(p + self.speed * dt, 0.0), 1.0)
                    if abs(pg - p) < 0.0015 and abs(self.speed) < 0.02:
                        new, self.speed = goal, 0.0
                    else:
                        new = p ** self.GAMMA
                try:
                    self.backend.set(new)
                    self.value = new
                except Exception as e:  # noqa: BLE001 — keep the animation thread alive
                    log("could not set brightness:", e)
                    self.value = goal  # don't spin on a broken backend
            time.sleep(dt)


# --- keyboard backlight ----------------------------------------------------------

class KeyboardLight:
    """Keyboard backlight on in the dark, off in the light.

    Acts only when the room crosses between dark and light; if you change the
    keyboard light yourself in between, it is left alone until the next crossing.
    """
    HYSTERESIS = 0.2

    def __init__(self, get, put):
        self.get, self.put = get, put
        self.dark = None        # last known room state

    def update(self, light_log, cfg):
        if not cfg["kbd_auto"]:
            self.dark = None
            return
        edge = cfg["kbd_dark_below"] + (self.HYSTERESIS if self.dark else 0.0)
        dark = light_log < edge
        if dark == self.dark:
            return               # no crossing: respect whatever is set now
        first = self.dark is None
        self.dark = dark
        if first and not dark:
            return               # starting in daylight: don't switch off a light you turned on
        want = int(cfg["kbd_level"]) if dark else 0
        if self.get() != want:
            self.put(want)
        log(f"keyboard backlight {'on' if dark else 'off'}")


# --- learning from manual corrections ---------------------------------------------

class Corrections:
    """Remembers manual brightness corrections and offers to make them permanent."""
    KEEP = 50

    def __init__(self, path=CORRECTIONS_PATH):
        self.path = path
        self.asking = False
        try:
            with open(path) as f:
                self.events = [tuple(e) for e in json.load(f)]
        except (OSError, ValueError):
            self.events = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.events[-self.KEEP:], f)
        except OSError as e:
            debug("could not save corrections:", e)

    def add(self, light_log, brightness):
        self.events.append((time.time(), round(light_log, 3), round(brightness, 3)))
        self.events = self.events[-self.KEEP:]
        self._save()

    def forget(self, group):
        drop = set(group)
        self.events = [e for e in self.events if e not in drop]
        self._save()

    def maybe_ask(self, curve, on_remember):
        """If a correction keeps repeating, ask (desktop notification) in the background."""
        if self.asking:
            return
        found = suggest(self.events, curve, time.time())
        if not found:
            return
        x, y, direction, group = found
        self.asking = True
        word = "ярче" if direction > 0 else "темнее"

        def ask():
            try:
                r = subprocess.run(
                    ["notify-send", "--app-name=Автояркость", "--icon=display-brightness-symbolic",
                     "--action=remember=Запомнить", "--action=no=Не надо",
                     "Автояркость",
                     f"При таком свете вы уже {len(group)} раза делали экран {word} — "
                     f"примерно {round(y * 100)}%. Запомнить и ставить так сами?"],
                    capture_output=True, text=True)
                answer = r.stdout.strip()
            except OSError as e:
                log("could not show a notification:", e)
                answer = ""
            self.forget(group)          # asked once; don't nag about the same thing
            self.asking = False
            if answer == "remember":
                on_remember(x, y)

        threading.Thread(target=ask, daemon=True, name="ask").start()


# --- commands ----------------------------------------------------------------

def cmd_measure(cfg, args):
    meter = LightMeter(cfg)
    for i in range(args.count):
        ambient, d = meter.read()
        lg = math.log10(ambient)
        print(f"ambient {ambient:10.0f}  log10 {lg:5.2f}  -> target {curve_target(cfg['curve'], lg):.2f}"
              f"   (exposure {d['exposure'] / 10:.1f} ms, wall luma {d['luma']:.0f}, "
              f"full-frame log10 {math.log10(d['ambient_full']):.2f})")
        if i + 1 < args.count:
            time.sleep(args.every)


def cmd_probe(cfg, args):
    path = cfg["device"] or v.find_camera()
    print("device:", path)
    if not path:
        return
    with v.Camera(path) as cam:
        cap = cam.capability()
        print("card:  ", cap.card.decode(), f"({cap.driver.decode()})")
        print("formats:", ", ".join(f for f, _ in cam.formats()))
        print("YUYV sizes:", ", ".join(f"{w}x{h}" for w, h in cam.frame_sizes()))
        print("controls:")
        for cid, q in cam.controls():
            if q.type == 6:  # control class header
                continue
            try:
                cur = cam.get(cid)
            except OSError:
                cur = "?"
            print(f"  {q.name.decode():34} {q.minimum:>6}..{q.maximum:<6} default {q.default_value:<6} now {cur}")
        cam.configure(cfg["width"], cfg["height"])
        cam.set(v.CID_EXPOSURE_AUTO, v.EXPOSURE_MANUAL)
        cam.set(v.CID_EXPOSURE_ABSOLUTE, 1)
        cam.start()
        for _ in range(5):
            y = cam.grab()
        print(f"black level (shortest exposure): {sum(y) / len(y):.1f}"
              "  — only meaningful if the lens is covered or the room is dark")


def cmd_off(cfg, args):
    pick_backend().hand_back()
    print("brightness returned to manual control")


def write_status(**fields):
    """Publish the daemon's state for the settings window (atomic replace)."""
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"time": time.time(), "pid": os.getpid(), **fields}, f)
    os.replace(tmp, STATUS_PATH)


def cmd_run(cfg, args):
    meter = LightMeter(cfg)
    backend = pick_backend(wait=90)
    log(f"camera {cfg['device'] or 'auto'}, backend {backend.name}, interval {cfg['interval']}s")

    stop = wake = reload = False
    live_until = 0.0

    def on_stop(*_):
        nonlocal stop
        stop = True

    def on_wake(*_):     # SIGUSR1: measure now (or run a command from the window)
        nonlocal wake
        wake = True

    def on_reload(*_):   # SIGHUP: re-read the config file
        nonlocal reload, wake
        reload = wake = True

    def on_live(*_):     # SIGUSR2: follow the light continuously for a short while (test mode)
        nonlocal live_until, wake
        live_until = time.monotonic() + LIVE_SECONDS
        wake = True
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGUSR1, on_wake)
    signal.signal(signal.SIGUSR2, on_live)
    signal.signal(signal.SIGHUP, on_reload)

    # Sleep without polling: signals (and helper threads) write a byte to this
    # pipe, which wakes select() at once. No wakeups in between.
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)

    def poke():
        nonlocal wake
        wake = True
        try:
            os.write(wake_w, b"!")
        except BlockingIOError:
            pass

    def sleep(seconds):
        nonlocal wake
        end = time.monotonic() + seconds
        while not stop and not wake:
            left = end - time.monotonic()
            if left <= 0:
                break
            select.select([wake_r], [], [], left)
            try:
                os.read(wake_r, 512)
            except BlockingIOError:
                pass
        wake = False

    smoothed = None      # EMA of log10(ambient), screen glow removed
    interval = None      # current (adaptive) interval
    problem = None       # last reading problem, so it's logged once
    pause = None         # "locked" / "away" while paused
    last = {}            # last good reading, kept in the status file
    glow_result = None   # last screen-glow calibration, shown by the window
    fader = Fader(backend)
    corrections = Corrections()
    keyboard = None
    if isinstance(backend, GnomeBackend):
        try:
            backend.kbd_get()
            keyboard = KeyboardLight(backend.kbd_get, backend.kbd_set)
        except RuntimeError:
            pass                 # no keyboard backlight

    learned = []                 # (x, y) accepted from a notification, applied by the main loop
    learned_lock = threading.Lock()

    def remember(x, y):          # called from the notification thread
        with learned_lock:
            learned.append((x, y))
        poke()

    def status(state, **extra):
        try:
            write_status(state=state, backend=backend.name, device=meter.device,
                         live_left=max(0.0, live_until - time.monotonic()),
                         interval=cfg["interval"], interval_now=interval, smoothed=smoothed,
                         target=fader.goal, slider_offset=backend.offset,
                         screen_glow=cfg["screen_glow"], glow_result=glow_result,
                         keyboard=keyboard is not None, **last, **extra)
        except OSError as e:
            debug("could not write status:", e)

    def save():
        try:
            save_config(cfg, args.config)
        except OSError as e:
            log("could not save config:", e)

    def read_room():
        """One reading -> (raw ambient, room ambient without screen glow, details)."""
        ambient, d = meter.read()
        level = fader.value if fader.value is not None else 0.5
        return ambient, remove_glow(ambient, level, cfg["screen_glow"]), d

    def calibrate_glow():
        """Measure how much the screen's own light adds to the reading."""
        nonlocal glow_result
        status("calibrating")
        l1 = fader.value if fader.value is not None else backend.actual()
        l2 = l1 + 0.35 if l1 < 0.5 else l1 - 0.35
        a1, _ = meter.read()
        fader.go(l2, 0.3)
        fader.wait_idle(1.5)
        time.sleep(0.15)
        try:
            a2, _ = meter.read()
        finally:
            fader.go(l1, 0.3)
            fader.wait_idle(1.5)
        k = max(0.0, (a2 - a1) / (l2 - l1))
        share = min(k * l1 / a1, 0.95) if a1 > 0 else 0.0
        cfg["screen_glow"] = round(k, 2)
        save()
        glow_result = {"at": time.time(), "k": round(k, 2), "share": round(share, 3)}
        log(f"screen glow calibrated: k={k:.1f}, the screen made up {share:.0%} of the reading")

    # Start from whatever is on screen, with no manual offset left over (one
    # would pin the screen at a single level), so the first reading eases in.
    try:
        start = backend.actual()
        backend.take_over(start)
        fader.adopt(start)
    except (RuntimeError, OSError, ValueError) as e:
        log("could not take over brightness smoothly:", e)
    fader.start()

    try:
        while not stop:
            if reload:
                reload = False
                try:
                    new = load_config(args.config)
                    if args.device:
                        new["device"] = args.device
                    cfg.clear()
                    cfg.update(new)
                    meter.cfg = cfg
                    meter.reset()
                    interval = None
                    log("config reloaded")
                except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError) as e:
                    log("config reload failed, keeping the old one:", e)
                if smoothed is not None:   # show the effect of a new curve right away
                    fader.go(round(curve_target(cfg["curve"], smoothed), 3), cfg["transition"])

            with learned_lock:
                accepted, learned[:] = learned[:], []
            for x, y in accepted:
                cfg["curve"] = add_curve_point(cfg["curve"], x, y)
                save()
                log(f"remembered: at light {x:.2f} brightness {y:.2f}")
                if smoothed is not None:
                    fader.go(round(curve_target(cfg["curve"], smoothed), 3), cfg["transition"])

            if take_command() == "calibrate-glow":
                try:
                    calibrate_glow()
                except (v.CameraBusy, NoCamera, OSError, TimeoutError, RuntimeError) as e:
                    log("screen glow calibration failed:", e)
                    glow_result = {"at": time.time(), "error": str(e)}

            live = time.monotonic() < live_until

            # Pauses: locked screen, or nobody at the computer (unless a video keeps it awake).
            now_pause = None
            if cfg["pause_when_locked"] and backend.locked():
                now_pause = "locked"
            elif (cfg["pause_when_away"] and not live and backend.idle() > cfg["away_after"]
                  and not backend.idle_inhibited()):
                now_pause = "away"
            if now_pause:
                if pause != now_pause:
                    debug(f"paused: {now_pause}")
                    status(now_pause)
                pause = now_pause
                sleep(5)
                continue
            if pause:
                debug(f"resumed after {pause}")
                smoothed = None  # we may be somewhere else entirely; jump, don't ease
                interval = None
                pause = None

            status("measuring")
            try:
                ambient, room, d = read_room()
                if not live and smoothed is not None and abs(math.log10(room) - smoothed) > 0.4:
                    # Big jump: confirm it, a stale frame or a passing shadow shouldn't count.
                    debug(f"jump to {room:.0f}, re-checking")
                    ambient, room, d = read_room()
            except v.CameraBusy:
                debug("camera busy (another app is using it), skipping")
                status("busy")
                sleep(cfg["interval"])
                continue
            except NoCamera:
                if problem != "nocamera":
                    log("no camera (switched off or unplugged?), waiting for it")
                problem = "nocamera"
                status("nocamera")
                sleep(min(cfg["interval"], 10))
                continue
            except (OSError, TimeoutError, RuntimeError) as e:
                if problem != str(e):   # log each distinct problem once, not every interval
                    log("reading failed:", e)
                problem = str(e)
                status("error", message=str(e))
                sleep(cfg["interval"])
                continue
            if problem:
                log("camera readings are back")
                problem = None

            # The curve used to be made with the whole-frame average; move it once onto
            # the wall-based metric so that today's light keeps today's brightness.
            if cfg["curve_metric"] != METRIC:
                shift = math.log10(ambient) - math.log10(d["ambient_full"])
                cfg["curve"] = migrate_curve(cfg["curve"], shift)
                cfg["curve_metric"] = METRIC
                save()
                log(f"curve moved to the wall-based light metric (shift {shift:+.2f})")

            lg = math.log10(room)
            prev = smoothed
            weight = 1.0 if live else cfg["smoothing"]
            smoothed = lg if smoothed is None else smoothed + weight * (lg - smoothed)
            target = round(curve_target(cfg["curve"], smoothed), 3)
            debug(f"ambient {ambient:.0f}, without screen glow {room:.0f} (log {lg:.2f}, "
                  f"smoothed {smoothed:.2f}), exposure {d['exposure'] / 10:.1f} ms -> target {target:.2f}")
            last = {"ambient": room, "log": lg, "exposure_ms": d["exposure"] / 10,
                    "luma": d["luma"], "measured_at": time.time()}

            if preview_active():   # the settings window owns the screen for a moment
                status("ok", next_at=time.time() + 2)
                sleep(2)
                continue

            # Auto brightness is in charge: a manual change lasts only until this reading.
            # (Not while idle — GNOME dims the screen itself then.)
            overridden = False
            if fader.idle():
                try:
                    shown = backend.actual()
                    overridden = abs(shown - fader.value) > 0.04 and backend.idle() < 15
                    if overridden:
                        log(f"brightness was changed by hand ({shown:.2f}), back to automatic")
                        if cfg["suggest"]:
                            corrections.add(smoothed, shown)
                            corrections.maybe_ask(cfg["curve"], remember)
                        # Continue from the hand-picked level and ease back from there.
                        fader.adopt(backend.notice_manual(shown))
                except (RuntimeError, OSError, ValueError) as e:
                    debug("manual-change check failed:", e)
            if live:
                log(f"test: light {lg:.2f} -> brightness {target:.2f}")

            goal = fader.goal
            if goal is None or overridden or abs(target - goal) >= (0.01 if live else cfg["hysteresis"]):
                if not live:
                    log(f"ambient log {smoothed:.2f} -> brightness target {target:.2f}")
                fader.go(target, 1.0 if live else cfg["transition"])

            if keyboard:
                try:
                    keyboard.update(smoothed, cfg)
                except RuntimeError as e:
                    debug("keyboard backlight:", e)

            interval = 0.2 if live else next_interval(interval, None if prev is None else smoothed - prev, cfg)
            status("ok", next_at=time.time() + interval)
            sleep(interval)
    finally:
        try:
            # Ease to where the GNOME slider is, then let go: no jump on the way out.
            if fader.value is not None:
                fader.go(backend.manual_level(), 1.0)
                fader.wait_idle(1.5)
            fader.stop()
            backend.hand_back()
            log("released brightness to manual control")
        except Exception as e:  # noqa: BLE001 — best effort on shutdown
            log("could not release brightness:", e)
        try:
            write_status(state="stopped")
        except OSError:
            pass


def main():
    global log_verbose
    p = argparse.ArgumentParser(description="Webcam-based automatic screen brightness")
    p.add_argument("-c", "--config", default=CONFIG_PATH)
    p.add_argument("-d", "--device", help="V4L2 device, e.g. /dev/video0")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the daemon")
    r.add_argument("-i", "--interval", type=float)
    m = sub.add_parser("measure", help="print ambient light readings")
    m.add_argument("-n", "--count", type=int, default=1)
    m.add_argument("-e", "--every", type=float, default=2.0)
    sub.add_parser("probe", help="show camera info")
    sub.add_parser("off", help="return brightness to manual control")
    args = p.parse_args()

    log_verbose = args.verbose
    cfg = load_config(args.config)
    if args.device:
        cfg["device"] = args.device
    if getattr(args, "interval", None):
        cfg["interval"] = args.interval
    {"run": cmd_run, "measure": cmd_measure, "probe": cmd_probe, "off": cmd_off}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
