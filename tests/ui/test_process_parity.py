# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Process page parity helpers: VmSwap parsing, app identity keys, stop refusals, totals."""
import unittest

from tuf_control.telemetry.procs import app_from_cgroup, parse_vmswap
from tuf_control.ui import viewmodels as vm

STATUS = (b"Name:\tfirefox\nVmPeak:\t 100 kB\nVmRSS:\t 2048 kB\nVmSwap:\t  512 kB\n"
          b"Threads:\t5\n")


class Swap(unittest.TestCase):
    def test_vmswap_in_kib(self):
        self.assertEqual(parse_vmswap(STATUS), 512 * 1024)

    def test_missing_or_garbled(self):
        self.assertIsNone(parse_vmswap(b"Name:\tkthreadd\nThreads:\t1\n"))  # kernel thread
        self.assertIsNone(parse_vmswap(b"Name:\tx\nVmSwap:\tbad kB\n"))
        self.assertEqual(parse_vmswap(b"Name:\tx\nVmSwap:\t0 kB\n"), 0)


class Identity(unittest.TestCase):
    def test_scope_name_of_our_own_launcher(self):
        cg = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/" \
             "app-gnome-tuf\\x2dcontrol\\x2dgui-12345.scope\n"
        app, unit = app_from_cgroup(cg)
        self.assertEqual(app, "tuf-control-gui")
        self.assertIn(vm.norm_app_key(app), vm.OWN_APP_KEYS)

    def test_keys_unify_spellings(self):
        for a in ("io.github.hamzagouda.TufControl", "io.github.hamzagouda.TufControl.desktop",
                  "tuf-control", "TUF_Control"):
            self.assertEqual(vm.norm_app_key(a), "tufcontrol", a)
        self.assertEqual(vm.norm_app_key("org.gnome.Nautilus.desktop"), "nautilus")
        self.assertEqual(vm.norm_app_key("brave-browser"), "bravebrowser")

    def test_index_by_id_wm_class_and_exec(self):
        idx = vm.desktop_index([
            ("A", "io.github.hamzagouda.TufControl.desktop", None, "tuf-control-gui %U"),
            ("B", "org.mozilla.firefox.desktop", "Navigator", "/usr/lib/firefox/firefox %u"),
            ("C", "other.desktop", "Firefox", "firefox")])
        self.assertEqual(idx["tufcontrolgui"], "A")  # by executable
        self.assertEqual(idx["tufcontrol"], "A")  # by desktop id
        self.assertEqual(idx["navigator"], "B")  # by StartupWMClass
        self.assertEqual(idx["firefox"], "B")  # first entry wins


class Refusals(unittest.TestCase):
    def test_init_and_self_are_refused(self):
        self.assertIn("init", vm.signal_refusal(1, {42}))
        self.assertIn("itself", vm.signal_refusal(42, {42}))
        self.assertIsNone(vm.signal_refusal(4242, {42}))


class Totals(unittest.TestCase):
    def test_header_figures(self):
        t = vm.column_totals(4.2, 50.1, 89.0, 3.0, None, 12.4)
        self.assertEqual(t, {"cpu": "4%", "mem": "50%", "swap": "89%", "disk": "3%",
                             "gpu": "", "vram": "12%"})


if __name__ == "__main__":
    unittest.main()
