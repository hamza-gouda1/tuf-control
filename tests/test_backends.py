# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
import os
import tempfile
import unittest
from pathlib import Path

from tuf_control.backends.base import Backend
from tuf_control.backends.cli import check_args
from tuf_control.backends.dbus import DBusBackend
from tuf_control.backends.fake import FakeBackend, build_fake_tree
from tuf_control.backends.sysfs import SysfsBackend
from tuf_control.backends.util import PkexecWriter, RecordingWriter
from tuf_control.controller import FAKE_ROOT_ENV, Controller, default_backends
from tuf_control.core import (
    BackendUnavailable, Capability, CapStatus, GpuMode, PrivilegeRequired, Profile, ValidationError,
)


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_fake_tree(Path(self._tmp.name) / "root")


class PrivilegePolicyTests(unittest.TestCase):
    def test_no_new_privileges_never_attempts_helper(self):
        from unittest.mock import patch
        writer = PkexecWriter()
        with patch("tuf_control.backends.util.read_text", return_value="NoNewPrivs:\t1\n"), \
                patch("tuf_control.backends.util.subprocess.run") as run:
            self.assertFalse(writer.available())
            with self.assertRaisesRegex(PrivilegeRequired, "NoNewPrivileges"):
                writer.write(Path("/sys/fake"), "1")
            run.assert_not_called()


class SysfsParsing(TreeCase):
    def test_limits_real_format(self):
        b = FakeBackend(self.root)
        lims = {x.name: x for x in b.get_limits()}
        self.assertIn("ppt_pl1_spl", lims)
        self.assertNotIn("nv_base_tgp", lims)
        self.assertEqual((lims["ppt_pl1_spl"].min, lims["ppt_pl1_spl"].max), (5, 115))
        attrs = {a.name: a for a in b.get_attributes()}
        self.assertEqual(attrs["charge_mode"].possible_values, ["0", "1", "2"])
        self.assertNotIn("pending_reboot", attrs)

    def test_unreadable_ppt_group(self):
        root = build_fake_tree(Path(self._tmp.name) / "r2", ppt_readable=False)
        b = FakeBackend(root)
        lim = next(x for x in b.get_limits() if x.name == "ppt_pl1_spl")
        self.assertIsNone(lim.max)
        self.assertFalse(b.probe()[Capability.POWER_LIMITS].available)
        with self.assertRaises(ValidationError):
            b.set_limit("ppt_pl1_spl", 50)

    def test_readings(self):
        b = FakeBackend(self.root)
        self.assertIs(b.get_profile(), Profile.BALANCED)
        self.assertEqual(b.get_battery_threshold(), 90)
        self.assertEqual(b.get_battery_health().wear_percent, 12.0)
        self.assertIs(b.get_gpu_mode(), GpuMode.HYBRID)
        self.assertEqual(b.get_kbd_brightness(), (3, 3))
        t = b.get_telemetry()
        self.assertEqual(t.cpu_temp_c, 52.0)
        self.assertEqual(t.fan_rpm, {"cpu_fan": 4200, "gpu_fan": 3900})
        self.assertEqual(t.battery_power_w, 34.87)
        curves = b.get_fan_curves()
        curves["CPU"].validate()
        self.assertEqual(curves["GPU"].points[0].temp, 40)


class SysfsWrites(TreeCase):
    def test_writes_stay_in_fake_tree(self):
        b = FakeBackend(self.root)
        b.set_profile(Profile.PERFORMANCE)
        b.set_limit("ppt_pl1_spl", 100)
        b.set_battery_threshold(80)
        self.assertEqual(b.get_profile(), Profile.PERFORMANCE)
        self.assertEqual(b.get_battery_threshold(), 80)
        self.assertEqual(
            (self.root / "sys/class/firmware-attributes/asus-armoury/attributes/ppt_pl1_spl"
             / "current_value").read_text().strip(), "100")

    def test_bounds_enforced(self):
        w = RecordingWriter()
        b = SysfsBackend(self.root, writer=w, use_nvidia_smi=False)
        for name, val in (("ppt_pl1_spl", 116), ("ppt_pl1_spl", 4), ("nv_tgp", 26)):
            with self.assertRaises(ValidationError):
                b.set_limit(name, val)
        for bad in (19, 101, True):
            with self.assertRaises(ValidationError):
                b.set_battery_threshold(bad)
        with self.assertRaises(ValidationError):
            b.set_limit("../x", 1)
        with self.assertRaises(ValidationError):
            b.set_kbd_brightness(4)
        self.assertEqual(w.writes, [])


class Stub(Backend):
    def __init__(self, name, caps, profile=None, fail=False):
        self.name, self._caps, self._p, self.fail = name, caps, profile, fail
        self.set_calls = []

    def probe(self):
        return {c: CapStatus(c in self._caps, "" if c in self._caps else f"{self.name} lacks {c}")
                for c in Capability}

    def get_profile(self):
        if self.fail:
            raise BackendUnavailable("daemon vanished")
        return self._p

    def set_profile(self, p):
        self.set_calls.append(p)


class ControllerTests(TreeCase):
    def test_probe_cache_recovers_when_service_appears(self):
        now = [0.0]
        backend = Stub("late-service", set(), Profile.QUIET)
        ctl = Controller([backend], clock=lambda: now[0])
        self.assertFalse(ctl.capabilities()["profile"]["available"])
        backend._caps.add(Capability.PROFILE)
        now[0] = 59.0
        self.assertFalse(ctl.capabilities()["profile"]["available"])
        now[0] = 60.0
        self.assertEqual(ctl.get_profile(), (Profile.QUIET, "late-service"))
        backend._caps.clear()
        ctl.invalidate()
        self.assertFalse(ctl.capabilities()["profile"]["available"])

    def test_order_and_fallback(self):
        a = Stub("dbus", {Capability.PROFILE}, Profile.QUIET)
        b = Stub("sysfs", {Capability.PROFILE}, Profile.BALANCED)
        c = Stub("cli", set())
        ctl = Controller([a, b, c])
        self.assertEqual(ctl.get_profile(), (Profile.QUIET, "dbus"))
        caps = ctl.capabilities()["profile"]
        self.assertEqual((caps["backend"], caps["fallbacks"]), ("dbus", ["sysfs"]))
        self.assertIn("cli", caps["unavailable"])
        a.fail = True
        self.assertEqual(ctl.get_profile(), (Profile.BALANCED, "sysfs"))

    def test_unavailable_reports_reasons(self):
        ctl = Controller([Stub("dbus", set()), Stub("sysfs", set())])
        with self.assertRaises(BackendUnavailable) as cm:
            ctl.get_profile()
        self.assertIn("dbus lacks", str(cm.exception))

    def test_dry_run_does_not_write(self):
        a = Stub("dbus", {Capability.PROFILE}, Profile.QUIET)
        res = Controller([a]).set_profile(Profile.PERFORMANCE, dry_run=True)
        self.assertFalse(res.applied)
        self.assertEqual(a.set_calls, [])

    def test_set_limit_validates_before_backend(self):
        ctl = Controller([FakeBackend(self.root)])
        with self.assertRaises(ValidationError):
            ctl.set_limit("ppt_pl1_spl", 999)
        res = ctl.set_limit("ppt_pl1_spl", 999, clamp=True)
        self.assertTrue(res.applied)
        self.assertEqual({x.name: x.current for x in ctl.get_limits()[0]}["ppt_pl1_spl"], 115)

    def test_dbus_backend_with_fake_transport(self):
        class T:
            def name_has_owner(self, n): return n == "net.hadess.PowerProfiles"
            def get_property(self, d, p, i, prop): return "power-saver"
        be = DBusBackend(T())
        caps = be.probe()
        self.assertTrue(caps[Capability.PROFILE].available)
        self.assertFalse(caps[Capability.POWER_LIMITS].available)
        self.assertIs(be.get_profile(), Profile.QUIET)

    def test_children_parses_standard_introspection_xml(self):
        """asusd's replies carry the standard D-Bus DOCTYPE; _children must read them
        (a past hardening rejected the DOCTYPE outright and broke every introspected
        capability) while still refusing entity declarations."""
        STANDARD = ('<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection '
                    '1.0//EN" "http://www.freedesktop.org/standards/dbus-1.0/'
                    'introspect.dtd">\n<node><node name="attributes"/><node name="leds"/>'
                    '</node>')
        class T:
            def name_has_owner(self, n): return True
            def get_property(self, d, p, i, prop): return ""
            def introspect(self, d, p): return STANDARD
        be = DBusBackend(T())
        self.assertEqual(be._children("xyz.ljones.Asusd", "/"), ["attributes", "leds"])

        class Evil(T):
            def introspect(self, d, p):
                return '<!DOCTYPE node [<!ENTITY x "y">]><node>&x;</node>'
        self.assertRaises(Exception, DBusBackend(Evil())._children, "d", "/")

    def test_fake_root_never_uses_real_backends(self):
        old = os.environ.get(FAKE_ROOT_ENV)
        os.environ[FAKE_ROOT_ENV] = str(self.root)
        try:
            bs = default_backends()
            self.assertEqual([b.name for b in bs], ["fake"])
            self.assertIsInstance(bs[0], FakeBackend)
            self.assertEqual(bs[0].root, self.root)
            self.assertFalse(bs[0].use_nvidia_smi)
            self.assertEqual(Controller().get_profile()[1], "fake")
        finally:
            if old is None:
                os.environ.pop(FAKE_ROOT_ENV, None)
            else:
                os.environ[FAKE_ROOT_ENV] = old


class CliAllowList(unittest.TestCase):
    def test_allowed(self):
        for ok in (["profile", "get"], ["profile", "set", "Quiet"], ["battery", "limit", "80"],
                   ["leds", "set", "high"], ["armoury", "set", "ppt_pl1_spl", "80"]):
            self.assertEqual(check_args(ok), ok)

    def test_rejected(self):
        for bad in ([], ["rm", "-rf"], ["profile", "set"], ["profile", "set", "Quiet; reboot"],
                    ["battery", "limit", "80%"], ["battery", "limit", "-1"],
                    ["armoury", "set", "../x", "1"], ["profile", "get", "extra"],
                    ["fan-curve", "set"], ["aura", "effect"]):
            with self.assertRaises(ValidationError, msg=str(bad)):
                check_args(bad)
