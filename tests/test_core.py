# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
import unittest

from asusius.core import (
    BatteryHealth, FanCurve, PowerLimit, Profile, ValidationError,
)


class PowerLimitTests(unittest.TestCase):
    def lim(self, **kw):
        base = dict(name="ppt_pl1_spl", current=80, min=5, max=115, default=80)
        base.update(kw)
        return PowerLimit(**base)

    def test_clamp(self):
        lim = self.lim()
        self.assertEqual(lim.clamp(500), 115)
        self.assertEqual(lim.clamp(-3), 5)
        self.assertEqual(lim.clamp(60), 60)

    def test_validate_hard_bounds(self):
        lim = self.lim()
        self.assertEqual(lim.validate(5), 5)
        self.assertEqual(lim.validate(115), 115)
        for bad in (4, 116, True, 3.5, "80"):
            with self.assertRaises(ValidationError):
                lim.validate(bad)

    def test_unknown_bounds_refuse(self):
        lim = self.lim(min=None, max=None)
        with self.assertRaises(ValidationError):
            lim.validate(50)
        with self.assertRaises(ValidationError):
            lim.clamp(50)

    def test_increment(self):
        lim = self.lim(increment=5)
        lim.validate(10)
        with self.assertRaises(ValidationError):
            lim.validate(11)


class FanCurveTests(unittest.TestCase):
    temps = [39, 48, 51, 55, 62, 71, 80, 97]
    pwms = [28, 53, 65, 116, 158, 180, 217, 255]

    def test_valid(self):
        FanCurve.from_lists("CPU", self.temps, self.pwms).validate()

    def test_wrong_count(self):
        with self.assertRaises(ValidationError):
            FanCurve.from_lists("CPU", self.temps[:7], self.pwms[:7]).validate()

    def test_non_monotonic(self):
        t = list(self.temps); t[3] = 50
        with self.assertRaises(ValidationError):
            FanCurve.from_lists("CPU", t, self.pwms).validate()
        p = list(self.pwms); p[4] = 10
        with self.assertRaises(ValidationError):
            FanCurve.from_lists("CPU", self.temps, p).validate()

    def test_ranges(self):
        p = list(self.pwms); p[-1] = 256
        with self.assertRaises(ValidationError):
            FanCurve.from_lists("CPU", self.temps, p).validate()
        t = list(self.temps); t[-1] = 120
        with self.assertRaises(ValidationError):
            FanCurve.from_lists("CPU", t, self.pwms).validate()


class MiscTests(unittest.TestCase):
    def test_wear(self):
        self.assertEqual(BatteryHealth(79172000, 90004000).wear_percent, 12.0)
        self.assertEqual(BatteryHealth(100, 90).wear_percent, 0.0)

    def test_profile_parse(self):
        self.assertIs(Profile.parse("power-saver"), Profile.QUIET)
        self.assertIs(Profile.parse("Balanced"), Profile.BALANCED)
        with self.assertRaises(ValidationError):
            Profile.parse("turbo")
        self.assertEqual(Profile.QUIET.asusd_id, 2)
