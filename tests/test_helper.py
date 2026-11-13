# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asusius import helper
from asusius.backends.util import FallbackWriter, RecordingWriter
from asusius.core import PrivilegeRequired


def mk(root: Path, rel: str, text: str = "0") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        a = "sys/class/firmware-attributes/asus-armoury/attributes/ppt_pl1_spl"
        for f, v in (("type", "integer"), ("min_value", "5"), ("max_value", "80"),
                     ("scalar_increment", "1"), ("current_value", "40")):
            mk(self.root, f"{a}/{f}", v)
        self.attr = f"/{a}/current_value"
        mk(self.root, "sys/firmware/acpi/platform_profile", "balanced")
        mk(self.root, "sys/firmware/acpi/platform_profile_choices", "quiet balanced performance")
        mk(self.root, "sys/class/power_supply/BAT0/charge_control_end_threshold", "100")
        mk(self.root, "etc/passwd", "root")

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, path, value):
        helper.write(path, value, self.root)

    def test_valid_writes(self):
        self.w(self.attr, "60")
        self.assertEqual((self.root / self.attr[1:]).read_text(), "60")
        self.w("/sys/firmware/acpi/platform_profile", "quiet")
        self.w("/sys/class/power_supply/BAT0/charge_control_end_threshold", "80")

    def test_bounds(self):
        for v in ("4", "81", "-1", "abc", "6 ", "", "1e2"):
            with self.assertRaises(helper.Rejected, msg=v):
                self.w(self.attr, v)

    def test_profile_and_battery(self):
        for path, v in (("/sys/firmware/acpi/platform_profile", "cool"),
                        ("/sys/firmware/acpi/platform_profile", "turbo"),
                        ("/sys/class/power_supply/BAT0/charge_control_end_threshold", "19"),
                        ("/sys/class/power_supply/BAT0/charge_control_end_threshold", "101")):
            with self.assertRaises(helper.Rejected):
                self.w(path, v)

    def test_not_on_allow_list(self):
        for path in ("/etc/passwd", "/sys/power/state", "relative/path",
                     "/sys/class/power_supply/BAT0/status",
                     "/sys/class/firmware-attributes/asus-armoury/attributes/x/../../../../../../etc/passwd",
                     "/sys/../etc/passwd"):
            with self.assertRaises(helper.Rejected, msg=path):
                self.w(path, "1")
        self.assertEqual((self.root / "etc/passwd").read_text(), "root")

    def test_symlink_escape(self):
        attrs = self.root / "sys/class/firmware-attributes/asus-armoury/attributes"
        (attrs / "evil").mkdir()
        (attrs / "evil" / "current_value").symlink_to(self.root / "etc/passwd")
        with self.assertRaises(helper.Rejected):
            self.w("/sys/class/firmware-attributes/asus-armoury/attributes/evil/current_value", "1")
        self.assertEqual((self.root / "etc/passwd").read_text(), "root")

    def test_preexisting_class_symlink_resolves_to_allowed_device(self):
        device = self.root / "sys/devices/virtual/power_supply/BAT0"
        device.mkdir(parents=True)
        target = device / "charge_control_end_threshold"
        target.write_text("90")
        link = self.root / "sys/class/power_supply/BAT0"
        (link / target.name).unlink()
        link.rmdir()
        link.symlink_to(device, target_is_directory=True)
        path = "/sys/class/power_supply/BAT0/charge_control_end_threshold"
        self.assertEqual(helper.validate(path, "80", self.root), target)
        self.w(path, "80")
        self.assertEqual(target.read_text(), "80")

    def test_sink_rechecks_containment(self):
        outside = self.root / "etc/passwd"
        with mock.patch.object(helper, "_validate", return_value=(outside, outside.stat())):
            with self.assertRaisesRegex(helper.Rejected, "not under /sys"):
                self.w(self.attr, "60")
        self.assertEqual(outside.read_text(), "root")

    def test_intermediate_symlink_swap_after_validation_cannot_escape(self):
        path = "/sys/class/power_supply/BAT0/charge_control_end_threshold"
        parent = self.root / "sys/class/power_supply/BAT0"
        saved = parent.with_name("BAT0-saved")
        outside = mk(self.root, "etc/charge_control_end_threshold", "private")
        original_open = helper._open_attribute

        def swap(real):
            parent.rename(saved)
            parent.symlink_to(outside.parent, target_is_directory=True)
            return original_open(real)

        with mock.patch.object(helper, "_open_attribute", side_effect=swap):
            with self.assertRaises((OSError, helper.Rejected)):
                self.w(path, "80")
        self.assertEqual(outside.read_text(), "private")
        self.assertEqual((saved / outside.name).read_text(), "100")

    def test_intermediate_directory_replacement_rejects_new_inode(self):
        path = "/sys/class/power_supply/BAT0/charge_control_end_threshold"
        parent = self.root / "sys/class/power_supply/BAT0"
        original_open = helper._open_attribute
        saved = parent.with_name("BAT0-saved")

        def swap(real):
            parent.rename(saved)
            parent.mkdir()
            (parent / real.name).write_text("private")
            return original_open(real)

        with mock.patch.object(helper, "_open_attribute", side_effect=swap):
            with self.assertRaisesRegex(helper.Rejected, "changed after validation"):
                self.w(path, "80")
        self.assertEqual((parent / "charge_control_end_threshold").read_text(), "private")
        self.assertEqual((saved / "charge_control_end_threshold").read_text(), "100")

    def test_attribute_replacement_rejects_new_inode(self):
        path = "/sys/class/power_supply/BAT0/charge_control_end_threshold"
        target = self.root / path[1:]
        original_open = helper._open_attribute

        def swap(real):
            # Keep the old inode alive so the filesystem cannot immediately reuse it.
            target.rename(target.with_name("old"))
            target.write_text("private")
            return original_open(real)

        with mock.patch.object(helper, "_open_attribute", side_effect=swap):
            with self.assertRaisesRegex(helper.Rejected, "changed after validation"):
                self.w(path, "80")
        self.assertEqual(target.read_text(), "private")

    def test_value_charset_and_fan(self):
        with self.assertRaises(helper.Rejected):
            self.w(self.attr, "5; rm -rf /")
        mk(self.root, "sys/class/hwmon/hwmon3/name", "asus_custom_fan_curve")
        mk(self.root, "sys/class/hwmon/hwmon3/pwm1_auto_point1_pwm", "0")
        mk(self.root, "sys/class/hwmon/hwmon4/name", "other")
        mk(self.root, "sys/class/hwmon/hwmon4/pwm1_auto_point1_pwm", "0")
        self.w("/sys/class/hwmon/hwmon3/pwm1_auto_point1_pwm", "200")
        with self.assertRaises(helper.Rejected):
            self.w("/sys/class/hwmon/hwmon3/pwm1_auto_point1_pwm", "256")
        with self.assertRaises(helper.Rejected):
            self.w("/sys/class/hwmon/hwmon4/pwm1_auto_point1_pwm", "10")

    def test_main_usage(self):
        self.assertEqual(helper.main(["read", "x"]), 2)
        self.assertEqual(helper.main(["write", "/etc/passwd", "1"]), 3)


class FallbackTests(unittest.TestCase):
    def test_fallback_only_on_privilege(self):
        class Deny:
            def write(self, p, v): raise PrivilegeRequired("no")
        rec = RecordingWriter()
        FallbackWriter(Deny(), rec).write(Path("/x"), "1")
        self.assertEqual(len(rec.writes), 1)
        rec2 = RecordingWriter()
        FallbackWriter(rec2, RecordingWriter()).write(Path("/x"), "1")
        self.assertEqual(len(rec2.writes), 1)


if __name__ == "__main__":
    unittest.main()
