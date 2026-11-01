# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free geometry of custom-drawn widgets: the fan-curve editor's drag handles and
readout bubble, number formatting that must not wrap in the middle of a reading."""
import unittest

from tuf_control.backends.fake import DEFAULT_CURVES
from tuf_control.core import FanCurve
from tuf_control.telemetry import fmt
from tuf_control.ui import viewmodels as vm

SIZES = [(300, 300), (328, 300), (600, 300), (1030, 300), (200, 120)]


def model(idx=1):
    temps, pwms = DEFAULT_CURVES[idx]
    return vm.FanCurveModel(FanCurve.from_lists("CPU", list(temps), list(pwms)))


class FanEditorGeometry(unittest.TestCase):
    def test_bubble_stays_inside_the_canvas_at_every_point_and_size(self):
        extremes = [(t, p) for t in (FanCurve.TEMP_RANGE[0], FanCurve.TEMP_RANGE[1])
                    for p in (0, FanCurve.PWM_RANGE[1])]
        for w, h in SIZES:
            g = vm.CurveGeometry(w, h)
            pts = [g.to_px(t, p) for t, p in extremes + model().points]
            for x, y in pts:
                bx, by, bw, bh = vm.bubble_rect(x, y, 70.0, 14.0, w, h, g.left, g.right)
                self.assertGreaterEqual(bx, 2 - 1e-6, (w, h, x, y))
                self.assertGreaterEqual(by, 2 - 1e-6, (w, h, x, y))
                self.assertLessEqual(bx + bw, w - 2 + 1e-6, (w, h, x, y))
                self.assertLessEqual(by + bh, h - 2 + 1e-6, (w, h, x, y))

    def test_bubble_flips_below_a_point_at_the_top_edge(self):
        g = vm.CurveGeometry(600, 300)
        x, y = g.to_px(60, FanCurve.PWM_RANGE[1])  # 100 % is the top of the plot
        _bx, by, _bw, _bh = vm.bubble_rect(x, y, 70.0, 14.0, 600, 300, g.left, g.right)
        self.assertGreater(by, y)

    def test_handles_are_hit_within_a_24px_target(self):
        """The hit radius is 16 px, a 32 px target (WCAG 2.2 minimum is 24)."""
        m = model()
        for w, h in SIZES:
            g = vm.CurveGeometry(w, h)
            for i, (t, p) in enumerate(m.points):
                x, y = g.to_px(t, p)
                for dx, dy in ((0, 0), (11, 0), (-11, 0), (0, 11), (0, -11), (8, 8)):
                    tt, pp = g.to_data(x + dx, y + dy)
                    hit = m.nearest(tt, pp, 16.0, g.scale)
                    self.assertIsNotNone(hit, (w, h, i, dx, dy))

    def test_a_click_far_from_every_point_selects_nothing(self):
        m = model()
        g = vm.CurveGeometry(600, 300)
        x, y = g.to_px(25, 255)  # empty corner
        t, p = g.to_data(x, y)
        self.assertIsNone(m.nearest(t, p, 16.0, g.scale))


class NumberFormatting(unittest.TestCase):
    SAMPLES = [fmt.rpm(5300), fmt.rpm(612), fmt.temp(61.7), fmt.pct(42.0), fmt.pct(7.55, 1),
               fmt.watts(86.1), fmt.mhz(2625), fmt.mhz(450), fmt.bytes_(5.5 * 2 ** 30),
               fmt.rate(7.9e6), fmt.duration(18450), fmt.hours(1.4)]

    def test_nb_keeps_a_number_and_its_unit_together(self):
        for s in self.SAMPLES:
            out = vm.nb(s)
            for bad in ("0 ", "1 ", "2 ", "3 ", "4 ", "5 ", "6 ", "7 ", "8 ", "9 "):
                # an ASCII space after a digit may only precede a word that is not a unit
                if bad in out:
                    rest = out.split(bad, 1)[1]
                    self.assertFalse(rest[:1].isalpha() or rest[:1] in "°%µ/", (s, out))

    def test_nb_leaves_words_alone(self):
        self.assertEqual(vm.nb("GiB of 15.6"), "GiB of 15.6")
        self.assertEqual(vm.nb("load 5.90 · 24 threads"), "load 5.90 · 24 threads")
        self.assertEqual(vm.nb("5 300 rpm"), "5 300 rpm")
        self.assertEqual(vm.nb("2560 × 1600"), "2560 × 1600")
        self.assertEqual(vm.nb("–"), "–")


if __name__ == "__main__":
    unittest.main()
