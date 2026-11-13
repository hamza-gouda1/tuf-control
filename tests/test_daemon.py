# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from asusius import cli
from asusius.automation.config import (
    CONFIG_DIR_ENV,
    RUNTIME_DIR_ENV,
    load_config,
    update_config,
)
from asusius.automation.service import DaemonClient
from asusius.automation.sources import StateCollector
from asusius.backends.fake import FakeBackend, build_fake_tree
from asusius.controller import FAKE_ROOT_ENV, Controller
from asusius.daemon import Daemon, acquire_lock
from asusius.effects.runner import EffectsRunner

SRC = str(Path(__file__).resolve().parents[1] / "src")


class Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = build_fake_tree(self.tmp / "root")
        for k, v in ((FAKE_ROOT_ENV, self.root), (CONFIG_DIR_ENV, self.tmp / "cfg"),
                     (RUNTIME_DIR_ENV, self.tmp / "run")):
            os.environ[k] = str(v)
            self.addCleanup(os.environ.pop, k, None)
        self.cfg = self.tmp / "cfg/config.json"

    def profile(self):
        return (self.root / "sys/firmware/acpi/platform_profile").read_text().strip()

    def aura(self):
        return json.loads((self.root / "fake/aura.json").read_text())

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class DaemonTests(Env):
    def make(self, apps=frozenset(), **kw):
        self.clock = SimpleNamespace(t=0.0)
        ctl = Controller([FakeBackend(self.root)])
        self.apps = set(apps)
        scanner = SimpleNamespace(scan=lambda: frozenset(self.apps))
        col = StateCollector(ctl, root=self.root, scanner=scanner,
                             wall=lambda: datetime(2026, 10, 23, 12, 0))
        fx = EffectsRunner(ctl, clock=lambda: self.clock.t,
                           sources={"theme": lambda: None, "telemetry": lambda: None})
        return Daemon(ctl, config_file=self.cfg, status_file=self.tmp / "run/status.json",
                      collector=col, effects=fx, clock=lambda: self.clock.t, **kw)

    def add_rules(self):
        def edit(c):
            c["automation"]["rules"] = [
                {"name": "game", "priority": 80, "debounce_s": 0,
                 "trigger": {"type": "app", "names": ["steam"]},
                 "actions": [{"type": "profile", "value": "performance"},
                             {"type": "effect", "name": "accent",
                              "params": {"colour": "#00ff00"}}]},
                {"name": "unplugged", "debounce_s": 0,
                 "trigger": {"type": "power", "source": "battery"},
                 "actions": [{"type": "profile", "value": "quiet"}]}]
        update_config(edit, self.cfg)

    def test_rules_effects_hot_reload_and_clean_stop(self):
        self.add_rules()
        d = self.make(apps={"steam"})
        self.assertTrue(d.reload())
        self.assertTrue(d.collector.needs_apps)
        d.step()
        self.assertEqual(self.profile(), "performance")
        d.effects.step(0.1)
        self.assertEqual(self.aura()["colour1"], [0, 255, 0])  # effect started by the rule
        self.apps.clear()
        d.step()
        self.assertEqual(self.profile(), "quiet")             # fallback rule (on battery)
        d.effects.step(0.1)
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])  # effect gone, mode back
        # hot reload: disable automation entirely
        time.sleep(0.01)
        update_config(lambda c: c["automation"].update(enabled=False), self.cfg)
        self.assertTrue(d.reload())
        d.step()
        self.assertEqual(self.profile(), "balanced")
        # a broken edit keeps the old config and reports it
        self.cfg.write_text("{nope")
        self.assertFalse(d.reload())
        self.assertIn("invalid JSON", d.status()["config_error"])
        d.write_status()
        st = json.loads((self.tmp / "run/status.json").read_text())
        self.assertTrue(st["running"] and st["fake"])

    def test_dry_run_changes_nothing(self):
        self.add_rules()
        d = self.make(apps={"steam"}, dry_run=True)
        d.reload()
        d.step()
        d.effects.step(0.1)
        self.assertEqual(self.profile(), "balanced")
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])
        self.assertTrue(all(e["dry_run"] for e in d.status()["log"]))
        self.assertTrue(any("dry run: would set profile performance" in e["detail"]
                            for e in d.status()["log"]))

    def test_run_loop_stops_and_restores(self):
        self.add_rules()
        update_config(lambda c: c["effects"].update(active="accent"), self.cfg)
        d = self.make(apps={"steam"})
        d.clock = time.monotonic
        d.effects.clock = d.effects.writer.clock = time.monotonic
        d.engine.clock = time.monotonic
        stop = threading.Event()
        t = threading.Thread(target=d.run, args=(stop,))
        t.start()
        for _ in range(40):
            time.sleep(0.05)
            if self.profile() == "performance" and self.aura()["colour1"] == [0, 255, 0]:
                break
        self.assertEqual(self.profile(), "performance")
        stop.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.profile(), "balanced")
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])
        self.assertFalse(json.loads((self.tmp / "run/status.json").read_text())["running"])

    def test_run_loop_survives_step_and_status_errors(self):
        """A raising step()/write_status() is logged, not fatal: the loop goes on."""
        d = self.make()
        d.reload()
        d.clock = time.monotonic
        d.effects.clock = d.effects.writer.clock = time.monotonic
        d.engine.clock = time.monotonic
        steps = []

        def bad_step():
            steps.append(1)
            raise ValueError("unexpected reply shape")
        d.step = bad_step
        real_write = d.write_status
        failed_write = []

        def flaky_write(running=True):
            if not failed_write:
                failed_write.append(1)
                raise OSError("disk full")
            real_write(running)
        d.write_status = flaky_write
        names = []
        real_thread = threading.Thread

        def spy(*a, **kw):  # rebrand check: no tuf-* thread names may come back
            names.append(kw.get("name"))
            return real_thread(*a, **kw)
        stop = threading.Event()
        with mock.patch("threading.Thread", spy):
            t = real_thread(target=d.run, args=(stop,))
            t.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    p = self.tmp / "run/status.json"
                    if p.exists() and json.loads(p.read_text()).get("running"):
                        break
                    time.sleep(0.02)
                st = json.loads((self.tmp / "run/status.json").read_text())
                self.assertTrue(st["running"])           # past the failed first write
                self.assertGreaterEqual(len(steps), 1)   # past the raising step()
                self.assertIn("asusius-effects", names)  # rebranded effects thread
                self.assertFalse(any(n and n.startswith("tuf-") for n in names))
            finally:
                stop.set()
                t.join(5)
        self.assertFalse(t.is_alive())
        self.assertFalse(json.loads((self.tmp / "run/status.json").read_text())["running"])

    def test_single_instance_lock(self):
        a = acquire_lock(self.tmp / "run/daemon.lock")
        self.assertIsNotNone(a)
        code = ("import sys;from pathlib import Path;from asusius.daemon import acquire_lock;"
                f"sys.exit(0 if acquire_lock(Path({str(self.tmp / 'run/daemon.lock')!r})) "
                "is None else 1)")
        r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": SRC})
        self.assertEqual(r.returncode, 0)
        a.close()


class ServiceClientTests(Env):
    def test_spawn_without_systemd_under_fake_root(self):
        calls = []
        real_thread = threading.Thread
        names = []

        def spy(*a, **kw):  # rebrand check: no tuf-* thread names may come back
            names.append(kw.get("name"))
            return real_thread(*a, **kw)
        popen = lambda cmd, **kw: (calls.append((cmd, kw)),
                                   SimpleNamespace(pid=4242, stdout=io.BytesIO(b"boot\n")))[1]
        c = DaemonClient(self.tmp / "run", run=lambda *a, **k: self.fail("no systemctl"),
                         popen=popen)
        self.assertEqual(c.status(), {"running": False})
        with mock.patch("threading.Thread", spy):
            self.assertEqual(c.start(dry_run=True), "started pid 4242")
        cmd, kw = calls[0]
        self.assertEqual(cmd[1:], ["-m", "asusius.daemon", "--dry-run"])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["env"][FAKE_ROOT_ENV], str(self.root))
        self.assertIn("asusius-daemon-log", names)
        self.assertFalse(any(n and n.startswith("tuf-") for n in names))
        deadline = time.monotonic() + 2
        content = ""
        while time.monotonic() < deadline and content != "boot\n":
            p = self.tmp / "run/daemon.log"
            content = p.read_text() if p.exists() else ""
            time.sleep(0.02)
        self.assertEqual(content, "boot\n")  # the drainer really drained the child's pipe

    def test_systemd_path_and_stale_status(self):
        os.environ.pop(FAKE_ROOT_ENV)
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            out = "active" if cmd[2] == "is-active" else ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        c = DaemonClient(self.tmp / "run", run=run, popen=lambda *a, **k: self.fail("spawned"),
                         alive=lambda pid: True)
        self.assertEqual(c.start(), "started asusiusd.service")
        self.assertEqual(c.stop(), "stopped asusiusd.service")
        self.assertIn(["systemctl", "--user", "start", "asusiusd.service"], seen)
        (self.tmp / "run").mkdir(exist_ok=True)
        (self.tmp / "run/status.json").write_text(json.dumps(
            {"pid": 1, "updated_ts": time.time() - 60, "running": True}))
        self.assertFalse(c.status()["running"])  # stale


class CliTests(Env):
    def test_automation_commands(self):
        code, out, err = self.run_cli("automation", "add", "--name", "Gaming", "--when",
                                      "app=steam", "--do", "profile=performance", "--do",
                                      "kbd_colour=#ff0000", "--priority", "80")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_cli("automation", "add", "--name", "Low", "--when",
                                      "battery<30", "--do", "profile=quiet")[0], 0)
        code, out, _ = self.run_cli("--json", "automation", "list")
        self.assertEqual([r["id"] for r in json.loads(out)["rules"]], ["gaming", "low"])
        code, out, _ = self.run_cli("automation", "test", "--dry-run",
                                    "--state", "ac=0,battery=20,apps=steam")
        self.assertEqual(code, 0)
        self.assertIn("MATCHES gaming", out)
        self.assertIn("overridden by a higher priority rule", out)
        self.assertEqual(self.profile(), "balanced")  # test never writes
        code, out, _ = self.run_cli("--json", "automation", "test", "low", "--state",
                                    "battery=80")
        self.assertEqual(json.loads(out)["matched"], [])
        self.assertEqual(self.run_cli("automation", "disable", "low")[0], 0)
        self.assertFalse(load_config(self.cfg)["automation"]["rules"][1]["enabled"])
        self.assertEqual(self.run_cli("automation", "remove", "low")[0], 0)
        code, _, err = self.run_cli("automation", "remove", "low")
        self.assertEqual(code, 1)
        self.assertIn("no rule", err)
        code, _, err = self.run_cli("automation", "add", "--name", "x", "--when", "ac",
                                    "--do", "charge_limit=5")
        self.assertEqual(code, 1)
        self.assertIn("20-100", err)
        code, _, _ = self.run_cli("automation", "add", "--name", "j", "--rule-json", json.dumps(
            {"trigger": {"type": "power", "source": "ac"},
             "actions": [{"type": "ppd", "value": "performance"}]}))
        self.assertEqual(code, 0)

    def test_kbd_effect_and_daemon_status(self):
        code, out, err = self.run_cli("kbd", "effect", "audio", "--param", "gain=2",
                                      "--notifications", "on")
        self.assertEqual(code, 0, err)
        self.assertIn("not running", out)
        fx = load_config(self.cfg)["effects"]
        self.assertEqual((fx["active"], fx["params"]["audio"]["gain"],
                          fx["notifications"]["enabled"]), ("audio", 2.0, True))
        self.assertEqual(self.run_cli("kbd", "effect", "off")[0], 0)
        self.assertIsNone(load_config(self.cfg)["effects"]["active"])
        self.assertEqual(self.run_cli("kbd", "effect", "disco")[0], 1)
        self.assertEqual(self.run_cli("kbd", "effect", "audio", "--param", "gain")[0], 1)
        code, out, _ = self.run_cli("--json", "kbd", "effect", "list")
        self.assertIn("temperature", [d["name"] for d in json.loads(out)])
        self.assertIn("stopped", self.run_cli("daemon", "status")[1])


class EndToEndTest(Env):
    """The real `python -m asusius.daemon` process against a fake tree."""

    def test_start_status_stop(self):
        self.run_cli("automation", "add", "--name", "Always", "--when", "battery",
                     "--do", "profile=performance", "--debounce", "0")
        self.run_cli("kbd", "effect", "accent", "--param", "colour=#0000ff")
        c = DaemonClient(self.tmp / "run")
        self.assertTrue(c.start().startswith("started pid"))
        try:
            for _ in range(60):
                time.sleep(0.1)
                st = c.status()
                if st["running"] and self.profile() == "performance" \
                        and self.aura()["colour1"] == [0, 0, 255]:
                    break
            self.assertTrue(st["running"], (self.tmp / "run/daemon.log").read_text())
            self.assertEqual(st["active_rules"], ["always"])
            self.assertEqual(self.aura()["colour1"], [0, 0, 255])
            self.assertIn("running", self.run_cli("daemon", "status")[1])
        finally:
            msg = c.stop()
        self.assertTrue(msg.startswith("stopped pid"), msg)
        self.assertEqual(self.profile(), "balanced")
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])


if __name__ == "__main__":
    unittest.main()
