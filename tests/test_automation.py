# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from asusius.automation import config as C
from asusius.automation.actions import ControllerSink, PpdClient
from asusius.automation.engine import Engine, State, evaluate, in_schedule, test_rule
from asusius.automation.rules import (
    Rule,
    RuleError,
    parse_action_spec,
    parse_trigger_spec,
)
from asusius.automation.sources import ProcessScanner, StateCollector, read_power
from asusius.backends.fake import FakeBackend, build_fake_tree
from asusius.controller import FAKE_ROOT_ENV, Controller
from asusius.core import BackendUnavailable, ValidationError

FRI_NOON = datetime(2026, 10, 23, 12, 0)  # a Friday


def rule(**kw):
    base = {"name": kw.pop("name", "r"), "debounce_s": kw.pop("debounce_s", 0),
            "trigger": kw.pop("trigger"), "actions": kw.pop("actions")}
    base.update(kw)
    return Rule.from_dict(base)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeSink:
    """Records writes; holds a tiny settings store so restores are visible."""

    def __init__(self, **values):
        self.values = {"profile": "balanced", "kbd": {"mode": 0}, "charge_limit": 100,
                       "kbd_brightness": 3, "panel_overdrive": True, "ppd": "balanced",
                       "effect": "__config__", **values}
        self.calls = []
        self.fail = set()

    def read(self, key):
        return copy.deepcopy(self.values[key])

    def apply(self, key, value, dry_run=False):
        if key in self.fail:
            raise BackendUnavailable(f"{key} broken")
        self.calls.append((key, value, dry_run))
        if not dry_run:
            self.values[key] = value
        return f"{'dry run: would set' if dry_run else 'set'} {key}={value}"


class FakeTreeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = build_fake_tree(self.tmp / "root")
        os.environ[FAKE_ROOT_ENV] = str(self.root)
        self.addCleanup(os.environ.pop, FAKE_ROOT_ENV, None)
        self.ctl = Controller([FakeBackend(self.root)])


class RuleModelTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        r = rule(name="Game Mode", trigger={"type": "app", "names": ["steam"]},
                 actions=[{"type": "profile", "value": "performance"}])
        self.assertEqual(r.id, "game-mode")
        self.assertEqual((r.priority, r.restore, r.enabled), (50, True, True))
        self.assertEqual(Rule.from_dict(r.to_dict()), r)
        self.assertIn("steam is running", r.describe())

    def test_validation(self):
        bad = [
            {"trigger": {"type": "nope"}, "actions": [{"type": "profile", "value": "quiet"}]},
            {"trigger": {"type": "power", "source": "x"},
             "actions": [{"type": "profile", "value": "quiet"}]},
            {"trigger": {"type": "battery", "below": 10, "above": 90},
             "actions": [{"type": "profile", "value": "quiet"}]},
            {"trigger": {"type": "schedule", "start": "25:00", "end": "07:00"},
             "actions": [{"type": "profile", "value": "quiet"}]},
            {"trigger": {"type": "power", "source": "ac"},
             "actions": [{"type": "charge_limit", "value": 10}]},
            {"trigger": {"type": "power", "source": "ac"},
             "actions": [{"type": "kbd_colour", "value": "#zzzzzz"}]},
            {"trigger": {"type": "power", "source": "ac"},
             "actions": [{"type": "kbd_colour", "value": "#ff0000"},
                         {"type": "kbd_effect", "mode": 1}]},       # same setting twice
            {"trigger": {"type": "profile", "value": "quiet"},
             "actions": [{"type": "profile", "value": "performance"}]},  # loop
            {"trigger": {"type": "power", "source": "ac"},
             "actions": [{"type": "effect", "name": "disco"}]},
            {"trigger": {"type": "power", "source": "ac"}, "actions": []},
            {"trigger": {"type": "power", "source": "ac"}, "priority": 101,
             "actions": [{"type": "profile", "value": "quiet"}]},
        ]
        for d in bad:
            with self.subTest(d=d), self.assertRaises(ValidationError):
                Rule.from_dict({"name": "x", **d})

    def test_specs(self):
        self.assertEqual(parse_trigger_spec("battery<20"), {"type": "battery", "below": 20})
        self.assertEqual(parse_trigger_spec("ac"), {"type": "power", "source": "ac"})
        t = parse_trigger_spec("time=fri-mon@22:00-07:00")
        self.assertEqual(t["days"], ["fri", "sat", "sun", "mon"])
        self.assertEqual(parse_action_spec("panel_overdrive=off"),
                         {"type": "panel_overdrive", "value": False})
        self.assertEqual(parse_action_spec("kbd_effect=breathe:#ff0000")["mode"], "breathe")
        r = Rule.from_dict({"name": "b", "trigger": parse_trigger_spec("app=steam"),
                            "actions": [parse_action_spec("kbd_effect=breathe:#ff0000")]})
        self.assertEqual(r.actions[0].params["mode"], 1)
        with self.assertRaises(RuleError):
            parse_action_spec("fan=max")

    def test_schedule_overnight(self):
        p = Rule.from_dict({"name": "n", "trigger": parse_trigger_spec(
            "time=fri@22:00-07:00"), "actions": [{"type": "profile", "value": "quiet"}]}
        ).trigger.params
        self.assertTrue(in_schedule(p, datetime(2026, 10, 23, 23, 0)))   # Fri 23:00
        self.assertTrue(in_schedule(p, datetime(2026, 10, 24, 6, 59)))   # Sat 06:59, from Fri
        self.assertFalse(in_schedule(p, datetime(2026, 10, 24, 23, 0)))  # Sat night
        self.assertFalse(in_schedule(p, datetime(2026, 10, 23, 6, 0)))   # Fri morning (Thu)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sink = FakeSink()
        self.eng = Engine(self.sink, clock=self.clock, wall=lambda: FRI_NOON)

    def st(self, **kw):
        return State(kw.pop("now", FRI_NOON), **kw)

    def test_app_rule_restores_previous_profile(self):
        self.eng.set_rules([rule(name="game", trigger={"type": "app", "names": ["Steam"]},
                                 actions=[{"type": "profile", "value": "performance"}],
                                 debounce_s=3)])
        self.eng.step(self.st(apps=frozenset({"steam"})))
        self.assertEqual(self.sink.values["profile"], "balanced")  # debounce not yet over
        self.clock.t = 3
        self.eng.step(self.st(apps=frozenset({"steam"})))
        self.assertEqual(self.sink.values["profile"], "performance")
        self.clock.t = 4
        self.eng.step(self.st(apps=frozenset()))
        self.clock.t = 5
        self.eng.step(self.st(apps=frozenset({"steam"})))  # flap: no change
        self.assertEqual(self.sink.values["profile"], "performance")
        self.clock.t = 6
        self.eng.step(self.st())
        self.clock.t = 9
        res = self.eng.step(self.st())
        self.assertEqual(self.sink.values["profile"], "balanced")
        self.assertEqual([e.kind for e in res.log], ["deactivate", "restore"])
        self.assertEqual(len([c for c in self.sink.calls if c[0] == "profile"]), 2)

    def test_priority_conflict_and_fallback(self):
        self.eng.set_rules([
            rule(name="battery", trigger={"type": "power", "source": "battery"},
                 actions=[{"type": "profile", "value": "quiet"},
                          {"type": "charge_limit", "value": 80}]),
            rule(name="game", priority=90, trigger={"type": "app", "names": ["steam"]},
                 actions=[{"type": "profile", "value": "performance"}]),
        ])
        res = self.eng.step(self.st(on_ac=False, apps=frozenset({"steam"})))
        self.assertEqual(self.sink.values["profile"], "performance")
        self.assertEqual(self.sink.values["charge_limit"], 80)
        self.assertTrue(any(e.kind == "conflict" and "battery" in e.detail for e in res.log))
        self.assertEqual(res.holders, {"profile": "game", "charge_limit": "battery"})
        self.eng.step(self.st(on_ac=False))  # game exits: the lower rule takes over
        self.assertEqual(self.sink.values["profile"], "quiet")
        self.eng.step(self.st(on_ac=True))   # plugged in: the original values come back
        self.assertEqual((self.sink.values["profile"], self.sink.values["charge_limit"]),
                         ("balanced", 100))
        self.assertEqual(self.eng.holds, {})

    def test_battery_hysteresis(self):
        self.eng.set_rules([rule(name="low", trigger={"type": "battery", "below": 20,
                                                      "hysteresis": 5},
                                 actions=[{"type": "kbd_brightness", "value": 0}])])
        for pct, on in ((25, False), (20, True), (22, True), (24, True), (25, False),
                        (21, False), (19, True)):
            self.eng.step(self.st(battery=pct))
            self.assertEqual(self.eng.active_ids() == ["low"], on, pct)

    def test_unknown_input_keeps_state_and_no_restore(self):
        self.eng.set_rules([rule(name="bat", restore=False,
                                 trigger={"type": "power", "source": "battery"},
                                 actions=[{"type": "profile", "value": "quiet"}])])
        self.eng.step(self.st(on_ac=False))
        self.eng.step(self.st(on_ac=None))
        self.assertEqual(self.eng.active_ids(), ["bat"])
        self.eng.step(self.st(on_ac=True))
        self.assertEqual(self.sink.values["profile"], "quiet")  # restore=False keeps it

    def test_dry_run_and_errors_and_ppd_sync(self):
        self.eng.dry_run = True
        self.eng.sync_ppd = True
        self.eng.set_rules([rule(name="q", trigger={"type": "power", "source": "battery"},
                                 actions=[{"type": "profile", "value": "quiet"},
                                          {"type": "panel_overdrive", "value": False}])])
        self.sink.fail.add("panel_overdrive")
        res = self.eng.step(self.st(on_ac=False))
        self.assertEqual(self.sink.values["profile"], "balanced")
        self.assertIn(("ppd", "power-saver", True), self.sink.calls)
        kinds = [(e.kind, e.key) for e in res.log]
        self.assertIn(("error", "panel_overdrive"), kinds)
        self.assertTrue(all(e.dry_run for e in res.log))

    def test_disable_removal_and_shutdown_release(self):
        r = rule(name="ac", trigger={"type": "power", "source": "ac"},
                 actions=[{"type": "profile", "value": "performance"}])
        self.eng.set_rules([r])
        self.eng.step(self.st(on_ac=True))
        self.assertEqual(self.sink.values["profile"], "performance")
        self.eng.set_rules([Rule.from_dict({**r.to_dict(), "enabled": False})])
        self.eng.step(self.st(on_ac=True))
        self.assertEqual(self.sink.values["profile"], "balanced")
        self.eng.set_rules([r])
        self.eng.step(self.st(on_ac=True))
        self.eng.shutdown()
        self.assertEqual(self.sink.values["profile"], "balanced")

    def test_schedule_and_profile_triggers(self):
        self.eng.set_rules([
            rule(name="night", trigger={"type": "schedule", "start": "22:00", "end": "07:00"},
                 actions=[{"type": "kbd_brightness", "value": 0}]),
            rule(name="perf-red", trigger={"type": "profile", "value": "performance"},
                 actions=[{"type": "kbd_colour", "value": "#ff0000"}]),
        ])
        self.eng.step(self.st(now=datetime(2026, 10, 23, 23, 30), profile="performance"))
        self.assertEqual(self.sink.values["kbd_brightness"], 0)
        self.assertEqual(self.sink.values["kbd"]["colour1"], [255, 0, 0])

    def test_evaluate_and_test_rule(self):
        rules = [rule(name="a", trigger={"type": "power", "source": "battery"},
                      actions=[{"type": "profile", "value": "quiet"}]),
                 rule(name="b", priority=70, trigger={"type": "battery", "below": 30},
                      actions=[{"type": "profile", "value": "quiet"}])]
        ev = evaluate(rules, self.st(on_ac=False, battery=20))
        self.assertEqual(ev["matched"], ["a", "b"])
        self.assertEqual(ev["winners"]["profile"]["rule"], "b")
        out = test_rule(rules[0], self.sink)
        self.assertTrue(out[0]["ok"] and out[0]["detail"].startswith("dry run"))
        self.assertEqual(self.sink.values["profile"], "balanced")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cfg" / "config.json"

    def test_defaults_atomic_and_watch(self):
        self.assertEqual(C.load_config(self.path), C.DEFAULTS)
        w = C.ConfigWatcher(self.path)
        w.changed()
        cfg = C.update_config(lambda c: c["automation"]["rules"].append(
            {"name": "ac", "trigger": {"type": "power", "source": "ac"},
             "actions": [{"type": "profile", "value": "performance"}]}), self.path)
        self.assertEqual(cfg["automation"]["rules"][0]["id"], "ac")
        self.assertTrue(w.changed())
        self.assertFalse(w.changed())
        self.assertEqual(json.loads(self.path.read_text())["automation"]["rules"][0]["priority"],
                         50)
        self.assertEqual([p.name for p in self.path.parent.iterdir()
                          if p.name.startswith(".config.json.")], [])  # no temp left over

    def test_rejects_bad_documents(self):
        for doc in ({"bogus": 1}, {"version": 9}, {"automation": {"poll_s": 0}},
                    {"automation": {"rules": [{"name": "x"}]}},
                    {"effects": {"active": "disco"}},
                    {"effects": {"params": {"audio": {"gain": 99}}}},
                    {"automation": {"rules": [
                        {"id": "a", "trigger": {"type": "power", "source": "ac"},
                         "actions": [{"type": "profile", "value": "quiet"}]},
                        {"id": "a", "trigger": {"type": "power", "source": "ac"},
                         "actions": [{"type": "profile", "value": "quiet"}]}]}}):
            with self.subTest(doc=doc), self.assertRaises(C.ConfigError):
                C.validate_config(doc)
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{oops")
        with self.assertRaises(C.ConfigError):
            C.load_config(self.path)
        with self.assertRaises(C.ConfigError):  # a failing edit leaves the file untouched
            C.update_config(lambda c: c.update(bogus=1), self.path)
        self.assertEqual(self.path.read_text(), "{oops")


class SourcesTests(FakeTreeCase):
    def test_power_from_sysfs(self):
        self.assertEqual(read_power(self.root), (False, 51))
        ac = self.root / "sys/class/power_supply/AC0"
        ac.mkdir()
        (ac / "type").write_text("Mains\n")
        (ac / "online").write_text("1\n")
        self.assertEqual(read_power(self.root), (True, 51))
        self.assertEqual(read_power(self.tmp / "nothing"), (None, None))

    def test_process_scanner_caches_and_reads_names(self):
        proc = self.tmp / "proc"
        def mk(pid, comm, argv0, env=b""):
            d = proc / str(pid)
            d.mkdir(parents=True)
            (d / "comm").write_text(comm + "\n")
            (d / "cmdline").write_bytes(argv0.encode() + b"\0--flag\0")
            (d / "environ").write_bytes(env)
        mk(10, "steam", "/home/u/.steam/steam")
        mk(11, "wine64-preload", "Z:\\games\\Hades.exe")
        mk(12, "gjs", "/usr/bin/gjs",
           b"A=1\0GIO_LAUNCHED_DESKTOP_FILE=/usr/share/applications/org.gnome.Maps.desktop\0")
        sc = ProcessScanner(proc)
        names = sc.scan()
        self.assertTrue({"steam", "hades.exe", "org.gnome.maps", "gjs"} <= names)
        (proc / "10" / "comm").write_text("changed\n")  # cached: not re-read
        self.assertIn("steam", sc.scan())
        import shutil
        shutil.rmtree(proc / "10")
        self.assertNotIn("steam", sc.scan())
        st = StateCollector(self.ctl, root=self.root, scanner=sc, wall=lambda: FRI_NOON).collect()
        self.assertEqual((st.on_ac, st.battery, st.profile), (False, 51, "balanced"))


class ControllerSinkTests(FakeTreeCase):
    def test_reads_and_writes_go_through_controller(self):
        sink = ControllerSink(self.ctl)
        self.assertEqual(sink.read("profile"), "balanced")
        self.assertEqual(sink.read("charge_limit"), 90)
        self.assertTrue(sink.read("panel_overdrive"))
        self.assertIn("dry run", sink.apply("profile", "performance", dry_run=True))
        self.assertEqual((self.root / "sys/firmware/acpi/platform_profile").read_text().strip(),
                         "balanced")
        sink.apply("profile", "quiet")
        sink.apply("charge_limit", 60)
        sink.apply("panel_overdrive", False)
        sink.apply("kbd", {"mode": 0, "colour1": [1, 2, 3]})
        self.assertEqual((self.root / "sys/firmware/acpi/platform_profile").read_text().strip(),
                         "quiet")
        self.assertEqual(sink.read("charge_limit"), 60)
        self.assertFalse(sink.read("panel_overdrive"))
        self.assertEqual(sink.read("kbd")["colour1"], [1, 2, 3])
        with self.assertRaises(ValidationError):
            sink.apply("charge_limit", 5)  # Controller bounds
        with self.assertRaises(ValidationError):
            sink.apply("kbd", {"mode": 0, "colour1": [300, 0, 0]})
        self.assertIn("dry run", sink.apply("effect", {"name": "audio"}, dry_run=True))
        with self.assertRaises(BackendUnavailable):
            sink.apply("effect", {"name": "audio"})

    def test_ppd_fake(self):
        ppd = PpdClient(fake_root=str(self.root))
        self.assertEqual(ppd.get(), "balanced")
        ppd.set("power-saver")
        self.assertEqual(ppd.get(), "power-saver")
        with self.assertRaises(ValidationError):
            ppd.set("turbo")


if __name__ == "__main__":
    unittest.main()
