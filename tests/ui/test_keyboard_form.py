"""Hardware-effect form without a display: mode-dependent controls, per-mode drafts,
palettes, the Two colours switch for single-colour (TUF) firmware, and what Apply writes."""
import unittest

from tuf_control.backends.fake import FakeBackend, build_fake_tree
from tuf_control.controller import Controller
from tuf_control.core import KbdEffect
from tuf_control.ui import viewmodels as vm

WINE, GOLD, BLUE = (142, 42, 68), (201, 174, 134), (0, 0, 255)


def tuf_info(mode_data=None, builtins=None, device_type=2):
    """What Controller.get_kbd_rgb() returns on the FX608JHR (asusd 6.3.5)."""
    md = mode_data or {"mode": 0, "zone": 0, "colour1": [238, 10, 10], "colour2": [0, 0, 0],
                       "speed": 1, "direction": 0}
    return {"mode_data": md, "device_type": device_type, "supported_modes": [0, 1, 2, 10],
            "builtins": builtins if builtins is not None else {
                1: {"mode": 1, "zone": 0, "colour1": [155, 31, 64],
                    "colour2": [216, 185, 138], "speed": 1, "direction": 0},
                2: {"mode": 2, "zone": 0, "colour1": [166, 0, 0], "colour2": [0, 0, 0],
                    "speed": 1, "direction": 0}}}


class Controls(unittest.TestCase):
    def test_tuf_breathe_offers_two_colours(self):
        off = vm.kbd_controls(1, 2, blend=False)
        self.assertTrue(off.blend and off.colour1 and off.speed)
        self.assertFalse(off.colour2 or off.palettes or off.direction)
        self.assertIn("colour ↔ off", off.summary)
        self.assertEqual(off.breathe, "single")
        on = vm.kbd_controls(1, 2, blend=True)
        self.assertTrue(on.colour2 and on.palettes)
        self.assertEqual(on.colour1_title, "Colour 1")
        self.assertTrue(on.summary.startswith("Breathe: Colour 1 ↔ Colour 2"))
        self.assertEqual(on.breathe, "blend")

    def test_two_colour_firmware_shows_colour2_directly(self):
        for dev in (0, None):  # LaptopKeyboard2021, or a daemon that does not say
            c = vm.kbd_controls(1, dev)
            self.assertTrue(c.colour2 and not c.blend)
            self.assertEqual(c.summary, "Breathe: Colour 1 ↔ Colour 2.")

    def test_inputs_per_mode_follow_asusctl(self):
        static, cycle, pulse = (vm.kbd_controls(m, 2) for m in (0, 2, 10))
        self.assertEqual((static.colour1, static.speed, static.colour2), (True, False, False))
        self.assertEqual(static.colour1_title, "Colour")
        self.assertEqual((cycle.colour1, cycle.speed, cycle.direction), (False, True, False))
        self.assertEqual((pulse.colour1, pulse.speed), (True, False))  # pulse: -c only
        self.assertTrue(vm.kbd_controls(3, 0).direction)
        self.assertFalse(vm.kbd_controls(5, 0).colour1)  # rain: speed only
        self.assertTrue(vm.kbd_controls(4, 0).colour2)   # stars: two colours on HID boards
        self.assertFalse(vm.kbd_controls(4, 2).colour2)


class Form(unittest.TestCase):
    def test_switching_modes_keeps_each_modes_colours(self):
        f = vm.KbdForm()
        f.load(tuf_info())
        self.assertEqual(f.current.mode, 0)
        self.assertFalse(f.dirty)
        f.edit(colour1=BLUE)
        f.select(1)
        self.assertEqual((f.current.colour1, f.current.colour2), ((155, 31, 64), (216, 185, 138)))
        f.edit(colour1=WINE, colour2=GOLD)
        f.select(0)
        self.assertEqual(f.current.colour1, BLUE)        # the unsaved Static edit is back
        f.select(1)
        self.assertEqual((f.current.colour1, f.current.colour2), (WINE, GOLD))

    def test_breathe_never_starts_as_colour1_to_black(self):
        f = vm.KbdForm()
        f.load(tuf_info(builtins={}))
        f.select(1)
        self.assertEqual(f.current.colour2, vm.DEFAULT_COLOUR2)

    def test_two_colours_plan_runs_gradient_over_hardware_breathe(self):
        f = vm.KbdForm()
        f.load(tuf_info())
        f.select(1)
        f.palette(0)  # Wine & Gold
        f.set_blend(True)
        f.edit(speed_idx=2)
        self.assertTrue(f.dirty)
        plan = f.plan()
        self.assertEqual(plan.hardware, KbdEffect(1, 0, WINE, GOLD, 2, 0))
        self.assertEqual(plan.software, {"active": "gradient", "params": {
            "colour1": "#8e2a44", "colour2": "#c9ae86", "period": 2.5, "dip": 0.3}})
        f.applied(plan)
        self.assertFalse(f.dirty)

    def test_without_two_colours_effects_go_off(self):
        f = vm.KbdForm()
        f.load(tuf_info())
        f.select(1)
        self.assertIsNone(f.plan().software)
        f2 = vm.KbdForm()
        f2.load(tuf_info(device_type=0))
        f2.select(1)
        f2.set_blend(True)  # not offered on two-colour firmware: hardware does it
        self.assertIsNone(f2.plan().software)

    def test_running_gradient_reads_as_breathe_with_two_colours(self):
        static_frame = {"mode": 0, "zone": 0, "colour1": [170, 90, 100], "colour2": [0, 0, 0],
                        "speed": 1, "direction": 0}
        fx = {"active": "gradient", "params": {"gradient": {
            "colour1": "#2f6fdf", "colour2": "#bfe6ff", "period": 6.0}}}
        f = vm.KbdForm()
        f.load(tuf_info(static_frame), fx)
        self.assertEqual(f.current, KbdEffect(1, 0, (47, 111, 223), (191, 230, 255), 0, 0))
        self.assertTrue(f.blend and not f.dirty)
        self.assertEqual(f.controls.breathe, "blend")
        f.set_blend(False)
        self.assertTrue(f.dirty)

    def test_palettes_are_valid_pairs(self):
        names = [p[0] for p in vm.PALETTES]
        for want in ("Wine & Gold", "Wine & Ivory", "Ice", "Sunset"):
            self.assertIn(want, names)
        for _n, a, b in vm.PALETTES:
            KbdEffect(1, colour1=a, colour2=b).validate()
            self.assertNotEqual(a, b)

    def test_fake_tree_round_trip_keeps_both_colours_per_mode(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d))
            ctl = Controller([FakeBackend(root)])
            ctl.set_kbd_effect(KbdEffect(1, 0, (255, 0, 0), BLUE, 1, 0), verify=True)
            ctl.set_kbd_effect(KbdEffect(0, 0, WINE, (0, 0, 0), 1, 0), verify=True)
            f = vm.KbdForm()
            f.load(ctl.get_kbd_rgb()[0])
            self.assertEqual(f.device_type, 2)
            f.select(1)
            self.assertEqual((f.current.colour1, f.current.colour2), ((255, 0, 0), BLUE))


class Preview(unittest.TestCase):
    def test_breathe_styles(self):
        e = KbdEffect(1, colour1=(255, 0, 0), colour2=(0, 0, 255), speed=1)
        # medium speed: 2.5 s per fade; at 3.75 s the alternating firmware shows Colour 2
        self.assertGreater(vm.preview_colour(e, 3.75)[2], 0.9)
        single = [vm.preview_colour(e, t / 10, breathe="single") for t in range(60)]
        self.assertTrue(all(c[2] == 0 for c in single))  # TUF firmware: never Colour 2
        self.assertEqual(vm.preview_colour(e, 0.0, breathe="blend"), (1.0, 0.0, 0.0))
        self.assertEqual(vm.preview_colour(e, 2.0, breathe="blend"), (0.0, 0.0, 1.0))


if __name__ == "__main__":
    unittest.main()
