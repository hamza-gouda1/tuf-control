"""Keyboard: the exact asusd Aura calls, read-back verification, the new software effects,
CPU load, error surfacing and restoring the hardware mode (fake transport / fake tree)."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tuf_control.automation.service import DaemonClient
from tuf_control.backends.dbus import DBusBackend
from tuf_control.controller import Controller
from tuf_control.core import BackendError, KbdEffect
from tuf_control.effects.base import Inputs
from tuf_control.effects.plugins import CpuLoadEffect, NightEffect, RainbowEffect, SpectrumEffect
from tuf_control.effects.registry import EFFECTS
from tuf_control.effects.runner import EffectsRunner, KbdWriter
from tuf_control.effects.system import CpuLoad, read_cpu_times

STATIC = (0, 0, (155, 31, 64), (0, 0, 0), "Med", "Right")
BREATHE = (1, 0, (155, 31, 64), (216, 185, 138), "Med", "Right")
PULSE_AS_LISTED = (9, 0, (10, 20, 30), (0, 0, 0), "Med", "Right")  # asusd 6.3 AllModeData


class AsusdAura:
    """A recording asusd 6.3.5 Aura object (values from the FX608JHR), introspected as tuf."""

    def __init__(self, apply=True):
        self.apply = apply  # False: accept Set and change nothing (a silent no-op)
        self.sets, self.calls, self.introspects = [], [], 0
        self.props = {"LedModeData": STATIC, "LedMode": 0, "Brightness": 3,
                      "SupportedBasicModes": [0, 1, 2, 10], "SupportedBrightness": [0, 1, 2, 3]}

    def name_has_owner(self, n):
        return n == "xyz.ljones.Asusd"

    def introspect(self, d, p):
        self.introspects += 1
        return '<node><node name="tuf"/></node>'

    def get_property(self, d, p, i, prop):
        return self.props[prop]

    def get_all(self, d, p, i):
        return dict(self.props)

    def set_property(self, d, p, i, prop, sig, value):
        self.sets.append((d, p, i, prop, sig, value))
        if self.apply:
            self.props[prop] = value
            if prop == "LedModeData":
                self.props["LedMode"] = value[0]

    def call(self, d, p, i, method, sig=None, args=()):
        self.calls.append((p, i, method))
        if method == "AllModeData":
            return ({0: STATIC, 1: BREATHE, 9: PULSE_AS_LISTED},)
        return None


class AuraCalls(unittest.TestCase):
    def test_apply_sends_exactly_what_asusctl_sends(self):
        # strace of `asusctl aura effect static -c ff0000` against asusd 6.3.5:
        # Properties.Set("xyz.ljones.Aura", "LedModeData",
        #                <(uu(yyy)(yyy)ss) 0 0 (255,0,0) (0,0,0) "Med" "Right">)
        t = AsusdAura()
        ctl = Controller([DBusBackend(t)])
        res = ctl.set_kbd_effect(KbdEffect(0, 0, (255, 0, 0), (0, 0, 0), 1, 0), verify=True)
        self.assertTrue(res.applied)
        self.assertEqual(t.sets, [("xyz.ljones.Asusd", "/xyz/ljones/aura/tuf", "xyz.ljones.Aura",
                                   "LedModeData", "(uu(yyy)(yyy)ss)",
                                   (0, 0, (255, 0, 0), (0, 0, 0), "Med", "Right"))])

    def test_each_hardware_mode_by_number(self):
        for mode in (0, 1, 2, 10):  # Static, Breathe, Colour cycle, Pulse (SupportedBasicModes)
            t = AsusdAura()
            Controller([DBusBackend(t)]).set_kbd_effect(
                KbdEffect(mode, 0, (1, 2, 3), (4, 5, 6), 2, 0), verify=True)
            self.assertEqual(t.sets[0][5], (mode, 0, (1, 2, 3), (4, 5, 6), "High", "Right"))

    def test_silent_no_op_is_an_error(self):
        t = AsusdAura(apply=False)
        with self.assertRaisesRegex(BackendError, "reports mode 0 colour \\[155, 31, 64\\]"):
            Controller([DBusBackend(t)]).set_kbd_effect(KbdEffect(1, colour1=(9, 9, 9)),
                                                        verify=True)

    def test_aura_path_cached_across_frames(self):
        t = AsusdAura()
        be = DBusBackend(t)
        for i in range(5):
            be.set_kbd_effect(KbdEffect(0, colour1=(i, i, i)))
        self.assertEqual(t.introspects, 1)

    def test_pulse_listed_as_9_reads_as_10_and_builtins(self):
        t = AsusdAura()
        info = DBusBackend(t).get_kbd_rgb()
        self.assertEqual(sorted(info["builtins"]), [0, 1, 10])
        self.assertEqual(info["builtins"][10]["mode"], 10)
        self.assertEqual(info["builtins"][1]["colour2"], [216, 185, 138])
        t.props["LedModeData"] = PULSE_AS_LISTED
        self.assertEqual(DBusBackend(t).get_kbd_rgb()["mode_data"]["mode"], 10)


class WriterRestore(unittest.TestCase):
    def test_stop_puts_breathe_and_statics_colour_back(self):
        t = AsusdAura()
        t.props["LedModeData"] = BREATHE
        ctl = Controller([DBusBackend(t)])
        clock = [0.0]
        w = KbdWriter(ctl, clock=lambda: clock[0])
        w.save()
        for i in range(3):
            clock[0] += 1
            w.push((200, i * 40, 0))
        self.assertEqual(t.props["LedModeData"][0], 0)  # frames are Static writes
        w.restore()
        self.assertEqual([s[5] for s in t.sets[-2:]],
                         [(0, 0, (155, 31, 64), (0, 0, 0), "Med", "Right"), BREATHE])


class NewEffects(unittest.TestCase):
    class Audio:
        def __init__(self, **lv):
            self.lv = lv

        def level(self, band="bass"):
            return self.lv.get(band, 0.0)

    def test_registered_and_in_picker_order(self):
        for name in ("audio", "spectrum", "ambient", "rainbow", "cpu", "night"):
            self.assertIn(name, EFFECTS)
        self.assertEqual(list(EFFECTS)[:3], ["audio", "spectrum", "ambient"])

    def test_spectrum_bands_and_meter(self):
        quiet = SpectrumEffect({}, Inputs(audio=self.Audio())).tick(0.1)
        bass = SpectrumEffect({}, Inputs(audio=self.Audio(bass=1.0))).tick(0.1)
        treble = SpectrumEffect({}, Inputs(audio=self.Audio(treble=1.0))).tick(0.1)
        self.assertLess(sum(quiet), 60)              # resting level is dim
        self.assertGreater(bass[0], bass[2])         # bass reads warm
        self.assertGreater(treble[2], treble[0])     # treble reads cool
        m = SpectrumEffect({"style": "meter"}, Inputs(audio=self.Audio(mid=0.2)))
        loud = SpectrumEffect({"style": "meter"}, Inputs(audio=self.Audio(mid=1.0)))
        self.assertLess(sum(m.tick(0.1)), sum(loud.tick(0.1)))

    def test_rainbow_walks_the_wheel(self):
        e = RainbowEffect({"period": 4.0})
        seen = {e.tick(1.0) for _ in range(4)}
        self.assertEqual(len(seen), 4)

    def test_cpu_load_gradient(self):
        load = [0.0]
        e = CpuLoadEffect({}, Inputs(cpu_load=lambda: load[0]))
        idle = e.tick(1.0)
        load[0] = 1.0
        for _ in range(5):
            busy = e.tick(1.0)
        self.assertEqual(idle, (47, 111, 223))
        self.assertEqual(busy, (224, 24, 45))

    def test_night_window_and_fade(self):
        def at(h, m=0):
            return NightEffect({"start": 21, "end": 7, "fade": 60, "level": 0.5},
                               Inputs(now=lambda: datetime(2026, 1, 1, h, m)))
        self.assertEqual(at(12).night_amount(), 0.0)
        self.assertAlmostEqual(at(21, 30).night_amount(), 0.5)
        self.assertEqual(at(1).night_amount(), 1.0)
        self.assertAlmostEqual(at(6, 30).night_amount(), 0.5)
        day, night = at(12).tick(1), at(2).tick(1)
        self.assertGreater(sum(day), 0)
        self.assertGreater(night[0], night[2])  # warm
        self.assertLess(max(night), 200)        # and dimmed

    def test_cpu_times_from_proc_stat(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stat"
            p.write_text("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 1 1 1\n")
            self.assertEqual(read_cpu_times(p), (200, 1000))
            vals = iter([(200, 1000), (450, 1500)])
            c = CpuLoad(reader=lambda: next(vals))
            self.assertIsNone(c())
            self.assertAlmostEqual(c(), 0.5)


class Visibility(unittest.TestCase):
    def test_source_errors_reach_the_status(self):
        class Broken:
            error = "parec: No such file or directory"

            def level(self, band="bass"):
                return 0.0

            def stop(self):
                pass
        r = EffectsRunner(Controller([DBusBackend(AsusdAura())]))
        r._live["audio"] = Broken()
        self.assertEqual(r.status()["error"], "audio input: parec: No such file or directory")

    def test_daemon_that_dies_on_start_raises_with_its_log(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "daemon.log").write_text("Traceback...\nModuleNotFoundError: x\n")
            c = DaemonClient(Path(d), sleep=lambda s: None)
            with self.assertRaisesRegex(BackendError, "ModuleNotFoundError: x"):
                c.wait_running(timeout_s=0.3)
            (Path(d) / "status.json").write_text(json.dumps({"pid": 1, "updated_ts": 0}))
            with self.assertRaises(BackendError):  # stale status is not "running"
                c.wait_running(timeout_s=0.3)


if __name__ == "__main__":
    unittest.main()
