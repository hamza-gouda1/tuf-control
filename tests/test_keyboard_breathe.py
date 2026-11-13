# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Two-colour Breathe: the exact asusd call, read-back of both colours, and the software
Gradient breathing that gives a single-colour (TUF) keyboard a real Colour 1 <-> Colour 2.

Owner report (FX608JHR, asusd 6.3.5): hardware Breathe shows one colour on the keys.
asusd's TUF path (asusd/src/aura_laptop/mod.rs, write_effect_and_apply) writes
kbd_rgb_mode = [1, mode, colour1.r, colour1.g, colour1.b, speed]: colour2 is stored and read
back over D-Bus, but never reaches the keyboard."""
import unittest

from test_keyboard_fx import STATIC, AsusdAura

from asusius.backends.dbus import DBusBackend
from asusius.controller import Controller
from asusius.core import BackendError, KbdEffect
from asusius.effects.plugins import GradientBreatheEffect, two_colour_breathe
from asusius.effects.registry import EFFECTS, coerce_params
from asusius.effects.runner import KbdWriter


class TufAura(AsusdAura):
    def __init__(self, apply=True, drop_colour2=False):
        super().__init__(apply)
        self.props["DeviceType"] = 2  # LaptopKeyboardTuf, as /xyz/ljones/aura/tuf reports
        self.drop_colour2 = drop_colour2

    def set_property(self, d, p, i, prop, sig, value):
        if self.drop_colour2 and prop == "LedModeData":
            value = (*value[:3], (0, 0, 0), *value[4:])
            self.sets.append((d, p, i, prop, sig, value))
            self.props[prop] = value
            return
        super().set_property(d, p, i, prop, sig, value)


class BreatheCall(unittest.TestCase):
    def test_breathe_two_colours_is_exactly_the_asusctl_call(self):
        # strace of `asusctl aura effect breathe --colour ff0000 --colour2 0000ff --speed high`
        # against asusd 6.3.5 (asusctl drops --speed, so it sends "Med"):
        # Properties.Set("xyz.ljones.Aura", "LedModeData",
        #                <(uu(yyy)(yyy)ss) 1 0 (255,0,0) (0,0,255) "Med" "Right">)
        t = TufAura()
        res = Controller([DBusBackend(t)]).set_kbd_effect(
            KbdEffect(1, 0, (255, 0, 0), (0, 0, 255), 1, 0), verify=True)
        self.assertTrue(res.applied)
        self.assertEqual(t.sets, [("xyz.ljones.Asusd", "/xyz/ljones/aura/tuf", "xyz.ljones.Aura",
                                   "LedModeData", "(uu(yyy)(yyy)ss)",
                                   (1, 0, (255, 0, 0), (0, 0, 255), "Med", "Right"))])

    def test_breathe_speed_is_sent(self):
        t = TufAura()
        DBusBackend(t).set_kbd_effect(KbdEffect(1, 0, (142, 42, 68), (201, 174, 134), 2, 0))
        self.assertEqual(t.sets[0][5], (1, 0, (142, 42, 68), (201, 174, 134), "High", "Right"))

    def test_read_back_has_both_colours_and_the_device_type(self):
        t = TufAura()
        be = DBusBackend(t)
        be.set_kbd_effect(KbdEffect(1, 0, (255, 0, 0), (0, 0, 255), 1, 0))
        info = be.get_kbd_rgb()
        self.assertEqual(info["mode_data"]["colour1"], [255, 0, 0])
        self.assertEqual(info["mode_data"]["colour2"], [0, 0, 255])
        self.assertEqual(info["device_type"], 2)
        self.assertIsNone(DBusBackend(AsusdAura()).get_kbd_rgb()["device_type"])

    def test_verify_fails_when_colour2_is_not_kept(self):
        t = TufAura(drop_colour2=True)
        want = (r"colour \[255, 0, 0\] / \[0, 0, 0\] instead "
                r"of mode 1 colour \[255, 0, 0\] / \[0, 0, 255\]")
        with self.assertRaisesRegex(BackendError, want):
            Controller([DBusBackend(t)]).set_kbd_effect(
                KbdEffect(1, 0, (255, 0, 0), (0, 0, 255), 1, 0), verify=True)


class GradientBreathing(unittest.TestCase):
    def test_registered_with_two_colours(self):
        self.assertIn("gradient", EFFECTS)
        keys = [p.key for p in EFFECTS["gradient"].params]
        self.assertEqual(keys[:2], ["colour1", "colour2"])
        self.assertEqual(coerce_params("gradient", {"colour1": "#FF0000", "period": "6"}),
                         {"colour1": "#ff0000", "period": 6.0})

    def test_fades_colour1_to_colour2_and_back(self):
        a, b = (255, 0, 0), (0, 0, 255)
        self.assertEqual(two_colour_breathe(a, b, 0.0, 4.0), a)
        self.assertEqual(two_colour_breathe(a, b, 2.0, 4.0), b)
        self.assertEqual(two_colour_breathe(a, b, 4.0, 4.0), a)
        mid = two_colour_breathe(a, b, 1.0, 4.0)
        for got, want in zip(mid, (128, 0, 128)):  # a pure gradient, never black
            self.assertLessEqual(abs(got - want), 1)
        dim = two_colour_breathe(a, b, 1.0, 4.0, dip=0.5)
        for got, want in zip(dim, (64, 0, 64)):
            self.assertLessEqual(abs(got - want), 1)

    def test_effect_ticks_through_both_colours(self):
        e = GradientBreatheEffect({"colour1": "#ff0000", "colour2": "#0000ff", "period": 4,
                                   "dip": 0})
        frames = [e.tick(0.5) for _ in range(8)]
        self.assertEqual(frames[3], (0, 0, 255))   # t = 2 s: Colour 2
        self.assertEqual(frames[7], (255, 0, 0))   # t = 4 s: back to Colour 1
        self.assertTrue(all(sum(f) > 0 for f in frames))

    def test_runs_through_the_daemon_writer_and_restores_breathe(self):
        t = TufAura()
        breathe = (1, 0, (142, 42, 68), (201, 174, 134), "Med", "Right")
        t.props["LedModeData"] = breathe
        ctl = Controller([DBusBackend(t)])
        clock = [0.0]
        w = KbdWriter(ctl, clock=lambda: clock[0])
        w.save()
        e = GradientBreatheEffect({"colour1": "#8e2a44", "colour2": "#c9ae86", "dip": 0})
        for _ in range(9):
            clock[0] += 0.5
            w.push(e.tick(0.5))
        frames = [s[5] for s in t.sets]
        self.assertTrue(all(f[0] == 0 for f in frames))  # Static writes, colour1 = frame
        self.assertIn((201, 174, 134), [f[2] for f in frames])
        w.restore()
        self.assertEqual([s[5] for s in t.sets[-2:]], [STATIC, breathe])


if __name__ == "__main__":
    unittest.main()
