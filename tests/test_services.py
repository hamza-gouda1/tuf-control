"""Services: safety rules, filters, the systemd backend against recorded D-Bus replies, the
cgroup reader, the fake backend and the CLI. No unit on the machine is started or stopped."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from tuf_control import cli
from tuf_control.controller import FAKE_ROOT_ENV
from tuf_control.services.backend import FakeServices, RefusedError, SystemdBackend, cgroup_map
from tuf_control.services.units import Unit, allowed_ops, filter_units, is_critical, refusal, \
    sort_units, template_of


def unit(name, active="active", sub="running", fs="enabled", load="loaded", **kw):
    return Unit(name=name, active=active, sub=sub, file_state=fs, load=load, **kw)


class SafetyTests(unittest.TestCase):
    def test_critical(self):
        for n in ("dbus.service", "dbus-broker.service", "systemd-journald.service",
                  "systemd-logind.service", "polkit.service", "NetworkManager.service",
                  "NetworkManager-dispatcher.service", "gdm.service", "gdm3.service",
                  "display-manager.service", "sddm.service", "user@1000.service",
                  "getty@tty1.service", "org.gnome.Shell@wayland.service",
                  "tuf-controld.service", "app-io.github.hamzagouda.TufControl-1234.scope",
                  "init.scope", "asusd.service"):
            self.assertTrue(is_critical(n), n)
        for n in ("cups.service", "docker.service", "ssh.service", "syncthing.service",
                  "bluetooth.service", "apt-daily.timer"):
            self.assertFalse(is_critical(n), n)
        self.assertEqual(template_of("getty@tty1.service"), "getty@.service")

    def test_refusals(self):
        nm = unit("NetworkManager.service")
        self.assertIn("core part", refusal(nm, "stop"))
        self.assertIn("core part", refusal(nm, "restart"))
        self.assertIn("core part", refusal(unit("dbus.service", fs="static"), "disable"))
        self.assertEqual(refusal(nm, "start"), "already running")
        cups = unit("cups.service")
        ops = allowed_ops(cups)
        self.assertIsNone(ops["stop"])
        self.assertIsNone(ops["restart"])
        self.assertIsNone(ops["disable"])
        self.assertEqual(ops["enable"], "already enabled")
        self.assertEqual(ops["start"], "already running")
        stopped = unit("ssh.service", "inactive", "dead", "disabled")
        self.assertIsNone(refusal(stopped, "start"))
        self.assertIsNone(refusal(stopped, "enable"))
        self.assertEqual(refusal(stopped, "stop"), "not running")
        self.assertIn("static", refusal(unit("fwupd.service", fs="static"), "enable"))
        self.assertIn("masked", refusal(unit("x.service", load="masked"), "start"))
        self.assertIn("only services", refusal(unit("home.mount"), "stop"))
        self.assertIn("unusual", refusal(unit("a b.service"), "stop"))
        self.assertIn("unknown", refusal(cups, "kill"))
        failed = unit("thermald.service", "failed", "failed")
        self.assertIsNone(refusal(failed, "restart"))
        self.assertIsNone(refusal(failed, "start"))


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.units = FakeServices("system").list_units()

    def test_filters(self):
        names = lambda us: [u.name for u in us]  # noqa: E731
        self.assertNotIn("apt-daily.timer", names(filter_units(self.units)))
        self.assertEqual(names(filter_units(self.units, kind="timer")), ["apt-daily.timer"])
        self.assertEqual(names(filter_units(self.units, "BLUETOOTH")), ["bluetooth.service"])
        self.assertEqual(names(filter_units(self.units, "scheduler")), ["cups.service"])
        self.assertEqual(names(filter_units(self.units, "1650")), ["cups.service"])
        self.assertEqual(names(filter_units(self.units, state="failed")),
                         ["thermald.service"])
        stopped = names(filter_units(self.units, state="stopped"))
        self.assertIn("docker.service", stopped)
        self.assertNotIn("thermald.service", stopped)
        top = sort_units(filter_units(self.units), "memory", desc=True)[0]
        self.assertEqual(top.name, "packagekit.service")
        self.assertEqual(sort_units(filter_units(self.units), "state")[0].name,
                         "thermald.service")


class FakeBackendTests(unittest.TestCase):
    def test_actions_change_state_and_refuse(self):
        be = FakeServices("system")
        by = {u.name: u for u in be.list_units()}
        be.act(by["docker.service"], "start")
        be.act(by["docker.service"], "enable")
        by = {u.name: u for u in be.list_units()}
        self.assertEqual((by["docker.service"].active, by["docker.service"].file_state),
                         ("active", "enabled"))
        with self.assertRaises(RefusedError):
            be.act(by["polkit.service"], "stop")
        self.assertEqual(be.log, ["start docker.service", "enable docker.service"])


class SystemdBackendTests(unittest.TestCase):
    """The real backend with D-Bus replaced by a recorder."""

    def make(self, scope="system"):
        be = SystemdBackend(scope, cg_root=self.cg)
        self.calls = []

        def call(method, args=None, reply=None, interactive=False, timeout_ms=10000,
                 path=None, iface=None):
            self.calls.append((method, args.unpack() if args is not None else None,
                               interactive))
            if method == "ListUnits":
                return ([
                    ("cups.service", "CUPS Scheduler", "loaded", "active", "running", "",
                     "/org/freedesktop/systemd1/unit/cups_2eservice", 0, "", "/"),
                    ("getty@tty1.service", "Getty on tty1", "loaded", "active", "running", "",
                     "/org/freedesktop/systemd1/unit/getty_40tty1_2eservice", 0, "", "/"),
                    ("ssh.service", "OpenBSD Secure Shell server", "loaded", "inactive",
                     "dead", "", "/org/freedesktop/systemd1/unit/ssh_2eservice", 0, "", "/"),
                ],)
            if method == "ListUnitFiles":
                return ([("/usr/lib/systemd/system/cups.service", "enabled"),
                         ("/usr/lib/systemd/system/getty@.service", "enabled"),
                         ("/usr/lib/systemd/system/ssh.service", "disabled")],)
            return ("/job/1",) if method.endswith("Unit") else ()
        be._call = call
        return be

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cg = Path(d.name)
        cups = self.cg / "system.slice" / "cups.service"
        cups.mkdir(parents=True)
        (cups / "memory.current").write_text("9437184\n")
        (cups / "cgroup.procs").write_text("1702\n1650\n")
        getty = self.cg / "system.slice" / "system-getty.slice" / "getty@tty1.service"
        getty.mkdir(parents=True)
        (getty / "memory.current").write_text("524288\n")
        (getty / "cgroup.procs").write_text("2001\n")

    def test_cgroup_map(self):
        m = cgroup_map(self.cg)
        self.assertEqual(set(m), {"cups.service", "getty@tty1.service"})

    def test_list(self):
        be = self.make()
        by = {u.name: u for u in be.list_units()}
        cups = by["cups.service"]
        self.assertEqual((cups.pid, cups.memory, cups.file_state, cups.path),
                         (1650, 9437184, "enabled", "/usr/lib/systemd/system/cups.service"))
        self.assertEqual(by["getty@tty1.service"].path, "/usr/lib/systemd/system/getty@.service")
        self.assertIsNone(by["ssh.service"].pid)
        self.assertEqual([c[0] for c in self.calls], ["ListUnits", "ListUnitFiles"])
        be.list_units()  # the unit file list is not re-read every time
        self.assertEqual([c[0] for c in self.calls][2:], ["ListUnits"])

    def test_actions_ask_polkit_on_the_system_bus_only(self):
        be = self.make("system")
        by = {u.name: u for u in be.list_units()}
        self.calls.clear()
        be.act(by["cups.service"], "restart")
        self.assertEqual(self.calls, [("RestartUnit", ("cups.service", "replace"), True)])
        self.calls.clear()
        be.act(by["ssh.service"], "enable")
        self.assertEqual([c[0] for c in self.calls], ["EnableUnitFiles", "Reload"])
        self.assertEqual(self.calls[0][1], (["ssh.service"], False, False))
        self.calls.clear()
        with self.assertRaises(RefusedError):
            be.act(by["getty@tty1.service"], "stop")
        self.assertEqual(self.calls, [])  # refused before any D-Bus call
        user = self.make("user")
        u = {x.name: x for x in user.list_units()}["ssh.service"]
        self.calls.clear()
        user.act(u, "start")
        self.assertEqual(self.calls, [("StartUnit", ("ssh.service", "replace"), False)])


class CliTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        os.environ[FAKE_ROOT_ENV] = d.name
        self.addCleanup(os.environ.pop, FAKE_ROOT_ENV, None)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_cli(self):
        code, out = self.run_cli("services")
        self.assertEqual(code, 0)
        self.assertIn("cups.service", out)
        code, out = self.run_cli("services", "list", "--state", "failed")
        self.assertEqual(out.split()[0], "thermald.service")
        code, out = self.run_cli("services", "--user", "list")
        self.assertIn("syncthing.service", out)
        code, out = self.run_cli("services", "list", "--user", "--filter", "pipe")
        self.assertIn("pipewire.service", out)
        self.assertNotIn("cups", out)
        code, out = self.run_cli("services", "stop", "polkit.service")
        self.assertEqual(code, 1)
        self.assertIn("refused", out)
        code, out = self.run_cli("services", "stop", "cups.service", "--dry-run")
        self.assertIn("would stop cups.service", out)
        code, out = self.run_cli("services", "start", "--user", "syncthing.service")
        self.assertEqual(code, 0)
        self.assertIn("(fake)", out)
        code, out = self.run_cli("services", "show", "cups.service", "--json")
        self.assertIn('"stop": true', out)
        code, out = self.run_cli("services", "show", "nope.service")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
