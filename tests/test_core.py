"""Unit tests for autobright's logic — no camera, no D-Bus, no GNOME needed.

Run from the project folder:  python3 -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import autobright as ab  # noqa: E402

CFG = dict(ab.DEFAULTS)


class Curve(unittest.TestCase):
    curve = [(1.0, 0.1), (3.0, 0.5), (5.0, 1.0)]

    def test_interpolates_and_clamps(self):
        self.assertAlmostEqual(ab.curve_target(self.curve, 2.0), 0.3)
        self.assertEqual(ab.curve_target(self.curve, 0.0), 0.1)
        self.assertEqual(ab.curve_target(self.curve, 9.0), 1.0)

    def test_add_point_replaces_neighbours(self):
        out = ab.add_curve_point(self.curve, 3.1, 0.7)
        self.assertEqual(out, [(1.0, 0.1), (3.1, 0.7), (5.0, 1.0)])

    def test_migrate_shifts_x_only(self):
        self.assertEqual(ab.migrate_curve(self.curve, 0.5), [(1.5, 0.1), (3.5, 0.5), (5.5, 1.0)])


class RoiStats(unittest.TestCase):
    W, H = 10, 10

    def frame(self, top, bottom, top_rows):
        return bytes([top] * (self.W * top_rows) + [bottom] * (self.W * (self.H - top_rows)))

    def test_measures_the_top_band_not_the_person(self):
        # wall (top 4 rows) at 60, a white T-shirt below at 200
        iqm, sat, full = ab.roi_stats([self.frame(60, 200, 4)], self.W, self.H, 0.35)
        self.assertAlmostEqual(iqm, 60)
        self.assertGreater(full, 100)
        self.assertEqual(sat, 0)

    def test_ignores_a_small_bright_lamp(self):
        f = bytearray(self.frame(50, 50, 4))
        f[0:3] = bytes([255, 255, 255])        # lamp pixels in the top band
        iqm, sat, _ = ab.roi_stats([bytes(f)], self.W, self.H, 0.4)
        self.assertAlmostEqual(iqm, 50)
        self.assertGreater(sat, 0)


class Glow(unittest.TestCase):
    def test_subtracts_screen_light(self):
        self.assertAlmostEqual(ab.remove_glow(300, 0.5, 200), 200)

    def test_never_removes_everything(self):
        self.assertAlmostEqual(ab.remove_glow(100, 1.0, 1000), 5)


class Interval(unittest.TestCase):
    cfg = {**CFG, "interval": 30.0, "max_interval": 180.0, "fast_interval": 8.0}

    def test_stretches_while_steady_up_to_max(self):
        iv = ab.next_interval(None, None, self.cfg)
        seen = [iv]
        for _ in range(8):
            iv = ab.next_interval(iv, 0.01, self.cfg)
            seen.append(iv)
        self.assertEqual(seen[0], 30)
        self.assertEqual(seen[1], 45)
        self.assertEqual(max(seen), 180)
        self.assertEqual(seen[-1], 180)

    def test_big_change_checks_soon_then_base(self):
        self.assertEqual(ab.next_interval(180, 0.5, self.cfg), 8)
        self.assertEqual(ab.next_interval(8, 0.1, self.cfg), 30)

    def test_after_test_mode_stretches_from_base(self):
        self.assertEqual(ab.next_interval(0.2, 0.0, self.cfg), 45)


class Suggest(unittest.TestCase):
    curve = [(1.0, 0.1), (3.0, 0.5), (5.0, 1.0)]

    def test_three_brighter_corrections_trigger(self):
        now = time.time()
        ev = [(now - 10, 3.0, 0.70), (now - 5, 3.1, 0.75), (now, 2.9, 0.72)]
        x, y, direction, group = ab.suggest(ev, self.curve, now)
        self.assertEqual(direction, 1)
        self.assertAlmostEqual(x, 3.0)
        self.assertAlmostEqual(y, 0.72)
        self.assertEqual(len(group), 3)

    def test_mixed_directions_or_too_few_do_not(self):
        now = time.time()
        self.assertIsNone(ab.suggest([(now, 3.0, 0.7), (now, 3.0, 0.3), (now, 3.0, 0.7)], self.curve, now))
        self.assertIsNone(ab.suggest([(now, 3.0, 0.7), (now, 3.0, 0.7)], self.curve, now))

    def test_old_events_are_ignored(self):
        now = time.time()
        old = now - 30 * 86400
        self.assertIsNone(ab.suggest([(old, 3.0, 0.7)] * 3, self.curve, now))


class FakeBackend:
    def __init__(self):
        self.sets = []

    def set(self, level):
        self.sets.append(level)


class FaderTest(unittest.TestCase):
    def test_smooth_and_retargets_without_jumps(self):
        b = FakeBackend()
        f = ab.Fader(b)
        f.adopt(0.3)
        f.start()
        try:
            f.go(0.8, 1.0)               # 1 s = the shortest fade used outside calibration
            time.sleep(0.4)
            f.go(0.2, 1.0)               # change of mind halfway
            f.wait_idle(4)
        finally:
            f.stop()
        self.assertAlmostEqual(b.sets[-1], 0.2, places=3)
        steps = [abs(a - c) for a, c in zip([0.3] + b.sets, b.sets)]
        self.assertLess(steps[0], 0.005)    # starts gently, no jolt
        self.assertLess(max(steps), 0.03)
        self.assertGreaterEqual(min(b.sets), 0.2 - 0.005)   # no overshoot below the goal


class FakeGnome(ab.GnomeBackend):
    """GNOME's rule without GNOME: screen = clamp(target + slider - 0.5)."""
    def __init__(self, slider):
        self.offset, self.sent, self.slider = 0.0, None, slider

    def _send(self, target):
        self.sent = None if target < 0 else target

    def actual(self):
        if self.sent is None:
            return self.slider
        return min(max(self.sent + self.slider - 0.5, 0.0), 1.0)


class GnomeOffset(unittest.TestCase):
    def test_take_over_keeps_screen(self):
        g = FakeGnome(slider=0.8)
        g.take_over(g.actual())
        self.assertAlmostEqual(g.actual(), 0.8)

    def test_slider_move_is_compensated(self):
        g = FakeGnome(slider=0.5)
        g.take_over(g.actual())
        g.set(0.4)
        self.assertAlmostEqual(g.actual(), 0.4)
        g.slider = 0.7                     # you drag the GNOME slider
        g.notice_manual(g.actual())
        g.set(0.4)                         # auto brightness puts it back
        self.assertAlmostEqual(g.actual(), 0.4)
        self.assertAlmostEqual(g.manual_level(), 0.7)


class Keyboard(unittest.TestCase):
    def make(self):
        state = {"v": 0}
        kb = ab.KeyboardLight(lambda: state["v"], lambda p: state.update(v=p))
        return kb, state

    def test_on_in_dark_off_in_light_with_hysteresis(self):
        kb, st = self.make()
        cfg = {**CFG, "kbd_auto": True, "kbd_level": 33, "kbd_dark_below": 1.8}
        kb.update(2.5, cfg)
        self.assertEqual(st["v"], 0)
        kb.update(1.5, cfg)
        self.assertEqual(st["v"], 33)
        kb.update(1.9, cfg)                # inside hysteresis: stays on
        self.assertEqual(st["v"], 33)
        kb.update(2.1, cfg)
        self.assertEqual(st["v"], 0)

    def test_manual_change_respected_until_next_crossing(self):
        kb, st = self.make()
        cfg = {**CFG, "kbd_auto": True, "kbd_level": 33}
        kb.update(1.0, cfg)
        st["v"] = 100                      # you press the key
        kb.update(1.1, cfg)
        self.assertEqual(st["v"], 100)

    def test_daylight_start_does_not_switch_off_your_light(self):
        kb, st = self.make()
        st["v"] = 66
        kb.update(3.0, {**CFG, "kbd_auto": True})
        self.assertEqual(st["v"], 66)


class Config(unittest.TestCase):
    def test_roundtrip_keeps_new_keys(self):
        cfg = {**CFG, "screen_glow": 12.5, "kbd_auto": True, "curve_metric": ab.METRIC,
               "curve": [(1.0, 0.2), (4.0, 0.9)]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.toml")
            ab.save_config(cfg, path)
            back = ab.load_config(path)
        for key in ("screen_glow", "kbd_auto", "curve_metric", "curve"):
            self.assertEqual(back[key], cfg[key], key)

    def test_meter_reset_gives_usable_exposure_map(self):
        # regression: a config reload used to set exposure to None and crash the next reading
        m = ab.LightMeter(dict(CFG))
        m.exposure[100] = 300
        m.reset()
        self.assertEqual(m.exposure.get(100), None)


class CorrectionsStore(unittest.TestCase):
    def test_add_forget_persist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            c = ab.Corrections(path)
            c.add(3.0, 0.7)
            c.add(3.1, 0.7)
            again = ab.Corrections(path)
            self.assertEqual(len(again.events), 2)
            again.forget(again.events[:1])
            self.assertEqual(len(ab.Corrections(path).events), 1)


if __name__ == "__main__":
    unittest.main()
