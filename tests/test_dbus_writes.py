"""D-Bus write names and signatures, checked against asusd 6.3 / supergfxd introspection."""
import unittest

from tuf_control.backends.dbus import (ARMOURY_PATH, ASUSD, DBusBackend, direction_to_name,
                                       speed_to_name)
from tuf_control.core import FanCurve, KbdEffect, Profile


class Rec:
    def __init__(self):
        self.calls, self.sets = [], []

    def name_has_owner(self, n): return True
    def introspect(self, d, p):
        return '<node><node name="tuf"/></node>'
    def get_property(self, d, p, i, prop):
        return {"Brightness": 3, "SupportedBrightness": [0, 1, 2, 3]}[prop]
    def get_all(self, d, p, i):
        return {"LedModeData": (0, 0, (155, 31, 64), (0, 0, 0), "Med", "Right"),
                "LedMode": 0, "Brightness": 3}
    def set_property(self, d, p, i, prop, sig, value):
        self.sets.append((p, i, prop, sig, value))
    def call(self, d, p, i, method, sig=None, args=()):
        self.calls.append((i, method, sig, args))


class DBusWrites(unittest.TestCase):
    def test_fan_curve_signature(self):
        t = Rec()
        curve = FanCurve.from_lists("cpu", [30 + 10 * i for i in range(8)],
                                    [10 * i for i in range(8)], True)
        DBusBackend(t).set_fan_curve(Profile.QUIET, curve)
        iface, method, sig, args = t.calls[0]
        self.assertEqual((iface, method), ("xyz.ljones.FanCurves", "SetFanCurve"))
        self.assertEqual(sig, "(u(s(yyyyyyyy)(yyyyyyyy)b))")
        self.assertEqual(len(args[1][1]), 8)
        self.assertEqual(len(args[1][2]), 8)

    def test_reset_uses_set_curves_to_defaults(self):
        t = Rec()
        DBusBackend(t).reset_fan_curves(Profile.BALANCED)
        self.assertEqual(t.calls[0][1], "SetCurvesToDefaults")
        self.assertEqual(t.calls[0][2], "(u)")

    def test_led_mode_data_uses_string_speed_direction(self):
        t = Rec()
        be = DBusBackend(t)
        be.set_kbd_effect(KbdEffect(mode=0, colour1=(1, 2, 3), speed=2, direction=1))
        self.assertEqual(t.sets[0][3], "(uu(yyy)(yyy)ss)")
        self.assertEqual(t.sets[0][4][4:], ("High", "Left"))
        data = be.get_kbd_rgb()["mode_data"]
        self.assertEqual((data["speed"], data["direction"]), (1, 0))

    def test_name_mapping(self):
        self.assertEqual(speed_to_name(0xF5), "High")
        self.assertEqual(speed_to_name(0xE1), "Low")
        self.assertEqual(direction_to_name(9), "Right")

    def test_one_shot_full_charge(self):
        t = Rec()
        DBusBackend(t).one_shot_full_charge()
        self.assertEqual(t.calls[0][:2], ("xyz.ljones.Platform", "OneShotFullCharge"))


if __name__ == "__main__":
    unittest.main()
