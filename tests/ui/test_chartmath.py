# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Chart maths without a display: ticks, windowing, gaps, no-overshoot curves."""
import math
import unittest
from array import array

from tuf_control.ui.chartmath import (
    bezier_controls,
    monotone_tangents,
    nice_range,
    nice_step,
    segments,
    window_points,
)


class ChartMathTests(unittest.TestCase):
    def test_nice_ticks(self):
        self.assertEqual(nice_step(100, 4), 25)
        self.assertEqual(nice_step(15.6, 3), 10)
        lo, hi, step = nice_range(43.0, 71.0, floor=20, min_span=20)
        self.assertLessEqual(lo, 43)
        self.assertGreaterEqual(hi, 71)
        self.assertGreaterEqual(lo, 20)
        self.assertAlmostEqual((hi - lo) / step, round((hi - lo) / step))
        self.assertEqual(nice_range(0, 0, floor=0, min_span=1)[0], 0)

    def _series(self, times, vals):
        t = array("d", times)
        v = array("f", vals)
        return t, v, range(len(times)), (lambda k: k)

    def test_window_and_gaps(self):
        t, v, idx, slot = self._series([0, 1, 2, 3, 10, 11], [1, 2, float("nan"), 4, 5, 6])
        xs, ys = [], []
        window_points(t, v, idx, slot, 0.0, 11.0, 1.0, xs, ys, bucket_px=0.001)
        runs = segments(xs, ys)
        # NaN sample and the 7 s pause both break the line
        self.assertEqual([(ys[a], ys[b - 1]) for a, b in runs], [(1, 2), (4, 4), (5, 6)])
        self.assertAlmostEqual(xs[-1], 1.0)

    def test_buckets_keep_peaks(self):
        n = 100
        vals = [0.0] * n
        vals[37] = 99.0
        t, v, idx, slot = self._series(list(range(n)), vals)
        xs, ys = [], []
        window_points(t, v, idx, slot, 0.0, n - 1.0, 10.0, xs, ys, bucket_px=1.0)
        self.assertLessEqual(len(xs), 12)
        self.assertEqual(max(ys), 99.0)  # a one-sample spike survives downsampling

    def test_monotone_curve_never_overshoots(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [0.0, 0.0, 100.0, 100.0, 100.0]
        m = monotone_tangents(xs, ys, 0, len(xs))
        self.assertEqual(m[0], 0.0)
        segs = bezier_controls(xs, ys, 0, len(xs), lambda v: v)
        for x1, y1, x2, y2, x3, y3 in segs:  # control points stay inside [0, 100]
            for y in (y1, y2, y3):
                self.assertTrue(-1e-9 <= y <= 100 + 1e-9)
        self.assertEqual(segs[-1][4:], (4.0, 100.0))  # passes through the last sample
        self.assertTrue(math.isfinite(sum(m)))


if __name__ == "__main__":
    unittest.main()
