# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Preferred python-apt scanner on package-cache fixtures; no real apt/cache access."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from asusius.updates import apt

FIXTURE = Path(__file__).parent / "fixtures/updates/apt_python.json"


class AptCacheFixture:
    """Mock apt_pkg's shared DepCache marks, including scratch-simulation resets."""

    def __init__(self):
        self.data = json.loads(FIXTURE.read_text())
        self.packages = []
        self.candidates = {}
        self.rows = {}
        self.marks = {}
        self.events = []
        for index, row in enumerate(self.data["packages"]):
            name = row["name"]
            self.rows[name] = row
            cur = SimpleNamespace(ver_str=row["current"]) if row["current"] else None
            pkg = SimpleNamespace(name=name, current_ver=cur,
                                  selected_state=2 if row.get("hold") else 0)
            self.packages.append(pkg)
            pf = SimpleNamespace(archive=row.get("archive", "noble-updates"),
                                 origin=row.get("origin", "Ubuntu"), label="")
            self.candidates[name] = (SimpleNamespace(
                ver_str=row["candidate"], arch=row.get("arch", "amd64"),
                size=row.get("size", 0), file_list=[(pf, index)])
                if row["candidate"] else None)
        self.cache = SimpleNamespace(packages=self.packages)
        records = SimpleNamespace(short_desc="", record="")

        def lookup(file_entry):
            row = self.data["packages"][file_entry[1]]
            records.short_desc = row.get("description", "")
            records.record = row.get("record", "")
            return not row.get("lookup_fails")

        records.lookup = mock.Mock(side_effect=lookup)
        self.records = records
        self.module = SimpleNamespace(
            SELSTATE_HOLD=2, init_config=mock.Mock(), init_system=mock.Mock(),
            config=SimpleNamespace(set=mock.Mock()), Cache=mock.Mock(return_value=self.cache),
            DepCache=mock.Mock(side_effect=self.depcache),
            PackageRecords=mock.Mock(return_value=records),
            version_compare=mock.Mock(side_effect=lambda a, b: (int(a) > int(b)) -
                                     (int(a) < int(b))))

    def package(self, name):
        return next(p for p in self.packages if p.name == name)

    def depcache(self, cache):
        assert cache is self.cache
        dep = mock.Mock()
        dep.broken_count = 0

        def upgrade(allow_new):
            self.events.append(("upgrade", allow_new))
            self.marks = {p.name: {"upgrade": self.rows[p.name].get("safe_upgrade", False),
                                   "install": self.rows[p.name].get("safe_install", False)}
                          for p in self.packages}

        def candidate(pkg):
            self.events.append(("candidate", pkg.name))
            return self.candidates[pkg.name]

        def init():
            self.events.append(("reset", None))
            self.marks.clear()

        def install(pkg, auto_install):
            self.events.append(("install", pkg.name))
            assert auto_install is True
            simulation = self.data["simulations"].get(pkg.name, {})
            if simulation.get("raises"):
                raise SystemError("fixture has no dependency solution")
            for action in ("install", "upgrade", "remove"):
                for name in simulation.get(action, []):
                    self.marks.setdefault(name, {})[action] = True
            dep.broken_count = simulation.get("broken", 0)

        dep.upgrade.side_effect = upgrade
        dep.get_candidate_ver.side_effect = candidate
        dep.init.side_effect = init
        dep.mark_install.side_effect = install
        for method, action in (("marked_upgrade", "upgrade"), ("marked_install", "install"),
                               ("marked_delete", "remove")):
            getattr(dep, method).side_effect = lambda p, action=action: self.marks.get(
                p.name, {}).get(action, False)
        return dep


class PythonAptTests(unittest.TestCase):
    def scan(self, fixture):
        with mock.patch.dict("sys.modules", {"apt_pkg": fixture.module}):
            return {item.name: item for item in apt.check_python_apt()}

    def test_holds_phasing_kept_back_and_normal_updates(self):
        fixture = AptCacheFixture()
        items = self.scan(fixture)
        self.assertEqual(list(items), sorted(items))
        self.assertEqual(set(items), {"held-driver", "phased-tool", "kept-tool",
                                      "normal-update", "marked-install"})
        self.assertEqual(items["held-driver"].extra["blocker"]["blocker"], "held")
        self.assertTrue(items["held-driver"].extra["facts"]["dpkg_hold"])
        self.assertEqual(items["phased-tool"].extra["facts"]["phased_pct"], 30)
        self.assertEqual(items["phased-tool"].extra["blocker"]["blocker"], "phased")
        self.assertEqual(items["kept-tool"].extra["facts"], {
            "kept_back": True, "dpkg_hold": False, "phased_pct": None,
            "new_packages": ["new-runtime"], "upgrades": ["held-driver"],
            "removals": ["obsolete-runtime"], "held_involved": ["held-driver"], "broken": True})
        normal = items["normal-update"]
        self.assertEqual((normal.current, normal.available, normal.size, normal.security),
                         ("1", "2", 4096, True))
        self.assertEqual(normal.description, "A security update")
        self.assertEqual(normal.extra["origin"], "Ubuntu/noble-security")
        self.assertEqual(normal.extra["arch"], "amd64")
        for name in ("normal-update", "marked-install"):
            self.assertFalse(items[name].extra["facts"]["kept_back"])
            self.assertNotIn("held", items[name].extra)
            self.assertNotIn("blocker", items[name].extra)
        fixture.module.init_config.assert_called_once_with()
        fixture.module.config.set.assert_called_once_with("Debug::NoLocking", "1")
        fixture.module.init_system.assert_called_once_with()
        fixture.module.Cache.assert_called_once_with(None)

    def test_simulations_run_after_scan_before_shared_marks_reset(self):
        fixture = AptCacheFixture()
        items = self.scan(fixture)
        first_reset = next(i for i, event in enumerate(fixture.events) if event[0] == "reset")
        self.assertTrue(all(event[0] != "candidate" for event in fixture.events[first_reset:]))
        self.assertFalse(items["normal-update"].extra["facts"]["kept_back"])
        self.assertEqual([value for event, value in fixture.events if event == "install"],
                         ["held-driver", "phased-tool", "kept-tool"])
        self.assertEqual(fixture.events[0], ("upgrade", False))

    def test_simulation_failure_and_broken_dependency_facts(self):
        fixture = AptCacheFixture()
        fixture.data["simulations"]["kept-tool"] = {"raises": True}
        facts = apt.simulate_install(fixture.module, fixture.cache,
                                     fixture.package("kept-tool"), {"held-driver"})
        self.assertEqual(facts, {"broken": True})

    def test_missing_record_or_file_metadata_is_optional(self):
        fixture = AptCacheFixture()
        fixture.rows["phased-tool"]["lookup_fails"] = True
        fixture.candidates["normal-update"].file_list = []
        items = self.scan(fixture)
        self.assertIsNone(items["phased-tool"].extra["facts"]["phased_pct"])
        self.assertEqual(items["normal-update"].description, "")
        self.assertFalse(items["normal-update"].security)
        self.assertNotIn("origin", items["normal-update"].extra)

    def test_check_prefers_python_apt_without_running_tools(self):
        fixture = AptCacheFixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "var/lib/dpkg/status"
            status.parent.mkdir(parents=True)
            status.write_text("")
            with mock.patch.dict("sys.modules", {"apt_pkg": fixture.module}), \
                    mock.patch.object(apt, "_run", side_effect=AssertionError("no subprocess")):
                result = apt.check(root)
        self.assertTrue(result.available)
        self.assertEqual(result.info["method"], "python-apt")
        self.assertEqual(len(result.items), 5)


if __name__ == "__main__":
    unittest.main()
