# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tuf_control import cli
from tuf_control.backends.fake import build_fake_tree
from tuf_control.controller import FAKE_ROOT_ENV


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = build_fake_tree(Path(tmp.name) / "root")
        os.environ[FAKE_ROOT_ENV] = str(self.root)
        self.addCleanup(os.environ.pop, FAKE_ROOT_ENV, None)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_reads_json(self):
        code, out, _ = self.run_cli("--json", "profile", "get")
        self.assertEqual((code, json.loads(out)["profile"]), (0, "balanced"))
        code, out, _ = self.run_cli("--json", "limits")
        self.assertEqual(code, 0)
        self.assertTrue(any(x["name"] == "nv_tgp" for x in json.loads(out)["limits"]))
        code, out, _ = self.run_cli("--json", "status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["battery_health"]["value"]["wear_percent"], 12.0)
        self.assertEqual(self.run_cli("capabilities")[0], 0)
        self.assertEqual(self.run_cli("telemetry")[0], 0)

    def test_writes(self):
        self.assertEqual(self.run_cli("profile", "set", "performance")[0], 0)
        self.assertIn("performance", self.run_cli("profile", "get")[1])
        self.assertEqual(self.run_cli("limit", "set", "ppt_pl1_spl", "90")[0], 0)
        self.assertEqual(self.run_cli("battery", "limit", "75")[0], 0)

    def test_bounds_rejected(self):
        for argv in (("limit", "set", "ppt_pl1_spl", "999"), ("battery", "limit", "10"),
                     ("profile", "set", "turbo"), ("limit", "set", "nope", "1")):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("error", err)

    def test_dry_run_changes_nothing(self):
        before = (self.root / "sys/firmware/acpi/platform_profile").read_text()
        code, out, _ = self.run_cli("profile", "set", "quiet", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.run_cli("battery", "limit", "50", "--dry-run")
        self.assertEqual((self.root / "sys/firmware/acpi/platform_profile").read_text(), before)
        bat = self.root / "sys/class/power_supply/BAT0/charge_control_end_threshold"
        self.assertEqual(bat.read_text().strip(), "90")
