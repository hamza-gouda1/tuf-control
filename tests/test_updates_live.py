# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Blocker classification, post-apply verification, the run-state/progress plumbing, the
history file and the end-to-end runner (fake mode: nothing real is installed)."""
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from asusius import cli
from asusius.controller import FAKE_ROOT_ENV
from asusius.automation.config import CONFIG_DIR_ENV
from asusius.updates import actions, blockers, check, history, progress, runner, verify
from asusius.updates.model import Report, SourceResult, UpdateItem

FIX = Path(__file__).resolve().parent / "fixtures" / "updates"


def fx(name: str) -> str:
    return (FIX / name).read_text()


def item(name="openssl", source="apt", cur="1.0", new="2.0", method="packagekit",
         needs_reboot=False, **extra):
    return UpdateItem(id=f"{source}:{name}", source=source, name=name, current=cur,
                      available=new, apply_method=method, needs_reboot=needs_reboot,
                      extra=dict(extra))


class FakeEnv(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for k, v in ((FAKE_ROOT_ENV, self.tmp / "root"), (CONFIG_DIR_ENV, self.tmp / "cfg")):
            os.environ[k] = str(v)
            self.addCleanup(os.environ.pop, k, None)
        os.environ.pop("ASUSIUS_FAKE_PENDING", None)
        self.addCleanup(os.environ.pop, "ASUSIUS_FAKE_PENDING", None)
        (self.tmp / "root").mkdir()
        os.environ[history.STATE_DIR_ENV] = str(self.tmp / "state")
        self.addCleanup(os.environ.pop, history.STATE_DIR_ENV, None)


# -- blockers ------------------------------------------------------------------------------
class BlockerTests(unittest.TestCase):
    def test_none_when_not_kept_back(self):
        self.assertIsNone(blockers.classify("openssl", {"kept_back": False}))
        self.assertIsNone(blockers.classify("openssl", None))

    def test_dpkg_hold_wins_with_unhold_command(self):
        bl = blockers.classify("libnvidia-gl-580", {"kept_back": True, "dpkg_hold": True})
        self.assertEqual(bl["blocker"], "held")
        self.assertIn("apt-mark unhold libnvidia-gl-580", bl["command"])
        self.assertFalse(bl["fixable"])
        self.assertIn("restart", bl["reason"])  # the NVIDIA note

    def test_hold_on_another_package(self):
        bl = blockers.classify("mesa-utils", {"kept_back": True, "held_involved":
                                              ["libnvidia-gl-580"]})
        self.assertEqual(bl["blocker"], "held-dependency")
        self.assertIn("libnvidia-gl-580", bl["command"])

    def test_removals_are_never_fixable(self):
        bl = blockers.classify("x", {"kept_back": True, "removals": ["a", "b"]})
        self.assertEqual(bl["blocker"], "conflict")
        self.assertFalse(bl["fixable"])
        self.assertIn("remove", bl["reason"])

    def test_phased(self):
        bl = blockers.classify("xz-utils", {"kept_back": True, "phased_pct": 40})
        self.assertEqual(bl["blocker"], "phased")
        self.assertIn("40 %", bl["reason"])

    def test_needs_new_is_fixable(self):
        new = ["linux-image-6.8.0-147-generic", "linux-modules-6.8.0-147-generic"]
        bl = blockers.classify("linux-image-generic",
                               {"kept_back": True, "new_packages": new})
        self.assertEqual(bl["blocker"], "needs-new")
        self.assertTrue(bl["fixable"])
        self.assertIn("linux-image-6.8.0-147-generic", bl["reason"])

    def test_broken_and_unknown(self):
        self.assertEqual(blockers.classify("x", {"kept_back": True, "broken": True})
                         ["blocker"], "conflict")
        self.assertEqual(blockers.classify("x", {"kept_back": True})["blocker"], "unknown")

    def test_parse_dpkg_holds(self):
        held = blockers.parse_dpkg_holds(fx("dpkg_status.txt"))
        self.assertEqual(held, {"libnvidia-gl-580", "libnvidia-compute-580"})


# -- verification ----------------------------------------------------------------------------
class VerifyTests(unittest.TestCase):
    def items(self):
        return [item("openssl"), item("linux-image-generic", cur="6.8.0-146",
                                      new="6.8.0-147"),
                item("System Firmware", source="firmware", cur="309", new="310",
                     method="fwupd", needs_reboot=True)]

    def test_installed_pending_failed(self):
        its = self.items()
        before = {i.id: i.current for i in its}
        after = {"apt:openssl": "2.0",                    # really landed
                 "apt:linux-image-generic": "6.8.0-146",  # backend said fine; nothing moved
                 "firmware:System Firmware": "309"}       # staged for the next restart
        out = {"packagekit": "updated 2 packages", "fwupd": "firmware installed"}
        res = verify.verify(its, before, after, out, None,
                            staged={"firmware:System Firmware": "310"})
        by = {r["name"]: r for r in res}
        self.assertEqual(by["openssl"]["result"], "installed")
        self.assertEqual(by["linux-image-generic"]["result"], "pending")
        self.assertIn("reported success but the installed version did not change",
                      by["linux-image-generic"]["reason"])
        self.assertEqual(by["System Firmware"]["result"], "restart")
        self.assertEqual(verify.summary(res), "1 of 3 installed, 1 on restart, "
                                              "1 still pending")
        self.assertFalse(verify.all_installed(res))

    def test_backend_error_is_failed(self):
        its = [item()]
        res = verify.verify(its, {"apt:openssl": "1.0"}, {"apt:openssl": "1.0"},
                            {"packagekit": "error: could not lock dpkg"}, None)
        self.assertEqual(res[0]["result"], "failed")
        self.assertEqual(res[0]["reason"], "could not lock dpkg")

    def test_cancelled_never_started(self):
        its = [item()]
        res = verify.verify(its, {"apt:openssl": "1.0"}, {"apt:openssl": "1.0"}, {},
                            None, cancelled=True)
        self.assertEqual(res[0]["result"], "pending")
        self.assertEqual(res[0]["reason"], "cancelled before it was installed")

    def test_cancelled_backend_message_is_pending_not_failed(self):
        its = [item()]
        res = verify.verify(its, {"apt:openssl": "1.0"}, {"apt:openssl": "1.0"},
                            {"packagekit": "error: PackageKit UpdatePackages was "
                                           "cancelled"}, None, cancelled=True)
        self.assertEqual(res[0]["result"], "pending")

    def test_pending_reason_from_the_recheck(self):
        its = [item("linux-image-generic", cur="6.8.0-146", new="6.8.0-147")]
        still = item("linux-image-generic", cur="6.8.0-146", new="6.8.0-147")
        still.extra["blocker"] = blockers.classify(
            "linux-image-generic", {"kept_back": True, "new_packages":
                                    ["linux-image-6.8.0-147-generic"]})
        rep = Report(sources=[SourceResult("apt", items=[still])])
        res = verify.verify(its, {"apt:linux-image-generic": "6.8.0-146"},
                            {"apt:linux-image-generic": "6.8.0-146"},
                            {"packagekit": "updated 1 package"}, rep)
        self.assertEqual(res[0]["result"], "pending")
        self.assertIn("Needs new packages", res[0]["reason"])
        self.assertIn("apt full-upgrade", res[0]["command"])

    def test_deps_method_and_summary(self):
        its = [item()]
        its[0].extra["with_deps"] = True
        res = verify.verify(its, {"apt:openssl": "1.0"}, {"apt:openssl": "2.0"},
                            {"packagekit-deps": "installed 4 packages with dependencies"},
                            None)
        self.assertEqual(res[0]["result"], "installed")
        self.assertEqual(verify.summary(res), "1 of 1 installed")
        self.assertTrue(verify.all_installed(res))
        self.assertEqual(verify.summary([]), "Nothing was installed")


# -- run state / progress ---------------------------------------------------------------------
class RunStateTests(unittest.TestCase):
    def test_snapshot_and_order(self):
        st = progress.RunState(clock=lambda: 0.0)
        w = st.watcher("packagekit")
        w.phase("Downloading", percent=40, package="openssl", speed=2.5e6, remaining_s=3.0)
        st.watcher("snapd").phase("Waiting")
        st.line("packagekit", "Get:1 openssl 40%")
        st.line("packagekit", "Get:1 openssl 40%")     # de-duplicated
        snap = st.snapshot()
        self.assertEqual([k for k, _v in snap["sources"]], ["packagekit", "snapd"])
        pk = dict(snap["sources"])["packagekit"]
        self.assertEqual((pk["phase"], pk["percent"], pk["package"]), ("Downloading", 40.0,
                                                                      "openssl"))
        self.assertEqual(len(snap["log"]), 1)
        self.assertTrue(snap["running"])
        st.finish("2 of 2 installed")
        self.assertFalse(st.snapshot()["running"])
        self.assertEqual(st.snapshot()["summary"], "2 of 2 installed")

    def test_auth_and_cancellable_flags(self):
        st = progress.RunState()
        st.update("packagekit", auth=True)
        st.update("snapd", cancellable=True)
        snap = st.snapshot()
        self.assertTrue(snap["auth"] and snap["cancellable"])
        st.sources["snapd"]["done"] = True
        self.assertFalse(st.snapshot()["cancellable"])

    def test_fmt(self):
        self.assertEqual(progress.fmt_duration(75), "1:15")
        self.assertEqual(progress.fmt_duration(3675), "1:01:15")
        self.assertEqual(progress.fmt_duration(None), "")
        self.assertEqual(progress.fmt_speed(2.5e6), "2.5 MB/s")
        self.assertEqual(progress.fmt_speed(500), "500 B/s")


# -- history ---------------------------------------------------------------------------------
class HistoryTests(FakeEnv):
    def run_doc(self, name, ts, result="installed"):
        return {"start": ts, "end": ts + 30, "trigger": "manual", "duration_s": 30.0,
                "sources": ["apt"], "cancelled": False, "reboot": False,
                "summary": "1 of 1 installed",
                "items": [{"id": f"apt:{name}", "source": "apt", "name": name,
                           "old": "1", "new": "2", "offered": "2", "result": result,
                           "reason": ""}]}

    def test_append_read_rotation(self):
        p = self.tmp / "h.jsonl"
        history.append(self.run_doc("a", 100), p, max_bytes=300)
        history.append(self.run_doc("b", 200), p, max_bytes=300)
        history.append(self.run_doc("c", 300), p, max_bytes=300)  # rotates on this write
        self.assertTrue((self.tmp / "h.jsonl.1").exists())
        runs = history.read(path=p)
        self.assertEqual([r["items"][0]["name"] for r in runs], ["c", "b"])  # newest first
        self.assertEqual(history.read(since_ts=250, path=p)[0]["items"][0]["name"], "c")
        apt_only = history.read(source="snap", path=p)
        self.assertEqual(apt_only, [])
        mode = p.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_torn_line_is_skipped(self):
        p = self.tmp / "h.jsonl"
        p.write_text('{"start": 1}\n{"start": 2, "items": []}\n{"start": 3\n')
        self.assertEqual(len(history.read(path=p)), 2)  # the torn last line is skipped

    def test_parse_since(self):
        now = 1_000_000.0
        self.assertEqual(history.parse_since("30m", now=now), now - 1800)
        self.assertEqual(history.parse_since("24h", now=now), now - 86400)
        self.assertEqual(history.parse_since("7d", now=now), now - 7 * 86400)
        self.assertEqual(history.parse_since("2w", now=now), now - 14 * 86400)
        self.assertIsNone(history.parse_since(None))
        self.assertIsNone(history.parse_since("all"))
        with self.assertRaises(ValueError):
            history.parse_since("3x", now=now)
        yd = history.parse_since("yesterday", now=now)
        self.assertLess(yd, history.parse_since("today", now=now))

    def test_changes_and_title(self):
        runs = [self.run_doc("a", 100), self.run_doc("b", 200, result="pending")]
        ch = history.changes(runs)
        self.assertEqual([c["name"] for c in ch], ["a"])  # only what really changed
        self.assertEqual(history.run_title(runs[0]), "1 of 1 installed")
        self.assertEqual(history.run_title({"trigger": "detected", "items": runs[0]["items"]}),
                         "1 updated outside Asusius")


# -- runner (fake mode end to end) ------------------------------------------------------------
class RunnerTests(FakeEnv):
    def apply(self, pending_ids=""):
        if pending_ids:
            os.environ["ASUSIUS_FAKE_PENDING"] = pending_ids
        rep = check.run_check()
        state = progress.RunState()
        res = runner.apply_run(rep.updates(), state, trigger="manual")
        return res, state

    def test_everything_installs(self):
        res, state = self.apply()
        self.assertTrue(verify.all_installed(res["results"]), res["summary"])
        self.assertEqual(res["summary"], "8 of 8 installed")
        runs = history.read()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["trigger"], "manual")
        self.assertEqual(len(runs[0]["items"]), 8)
        self.assertTrue(all(i["result"] == "installed" for i in runs[0]["items"]))
        self.assertLessEqual(runs[0]["duration_s"], 30)

    def test_pending_is_reported_not_celebrated(self):
        # linux-image-generic stays behind even though the backend reported success
        res, _state = self.apply("apt:linux-image-generic:amd64")
        by = {r["name"]: r for r in res["results"]}
        self.assertEqual(by["linux-image-generic"]["result"], "pending")
        self.assertIn("Needs new packages", by["linux-image-generic"]["reason"])
        self.assertEqual(res["summary"], "7 of 8 installed, 1 still pending")
        self.assertFalse(verify.all_installed(res["results"]))

    def test_state_collected_phases_and_log(self):
        _res, state = self.apply()
        snap = state.snapshot()
        self.assertIn("packagekit", dict(snap["sources"]))
        pk = dict(snap["sources"])["packagekit"]
        self.assertEqual(pk["phase"], "Done")
        self.assertTrue(snap["log"], "the fake backends must log lines")

    def test_cancel_stops_between_methods(self):
        rep = check.run_check()
        state = progress.RunState()
        state.cancel.set()
        res = runner.apply_run(rep.updates(), state)
        self.assertIn("cancelled by you", res["outcomes"]["packagekit"])
        results = {r["name"]: r for r in res["results"]}
        self.assertEqual(results["openssl"]["result"], "pending")
        self.assertEqual(results["openssl"]["reason"], "cancelled by you")

    def test_deps_run_for_a_kept_back_item(self):
        rep = check.run_check()
        it = next(i for i in rep.items() if i.name == "linux-image-generic")
        self.assertTrue(it.blocker["fixable"])
        res = runner.deps_run(it, progress.RunState())
        self.assertEqual(res["summary"], "1 of 1 installed")
        self.assertIn(it.name, actions.deps_names(it))
        self.assertTrue(it.extra["with_deps"])
        self.assertIn("linux-image-6.8.0-147-generic", actions.deps_names(it))
        log = (check.cache_dir() / "actions.log").read_text()
        self.assertIn("apply packagekit-deps", log)


class SchedulerDetectedTests(FakeEnv):
    def test_outside_installs_are_recorded(self):
        from asusius.updates.scheduler import UpdateScheduler
        rep1 = check.run_check()
        # make the cached check 7 hours old so the scheduler is due
        doc = rep1.to_dict()
        doc["checked_ts"] = time.time() - 7 * 3600
        check.update_cache(lambda d: d.__setitem__("report", doc))
        # openssl was installed by unattended-upgrades: the next check no longer offers it
        rep1_sources = [s for s in rep1.sources if s.source != "apt"]
        apt_src = next(s for s in rep1.sources if s.source == "apt")
        apt_src.items = [i for i in apt_src.items if i.name != "openssl"]

        def runner_fn():
            rep2 = Report(checked_ts=rep1.checked_ts + 600,
                          sources=rep1_sources + [apt_src])
            def put(d):
                d["report"] = rep2.to_dict()
            check.update_cache(put)
            return rep2
        shown = []
        s = UpdateScheduler(runner=runner_fn, notifier=lambda t, b: shown.append(t),
                            metered=lambda: False, first_delay_s=0)
        s.configure({"enabled": True, "interval_h": 6, "respect_metered": False,
                     "notify": False})
        self.assertTrue(s.tick())
        s.worker.join(10)
        runs = history.read()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["trigger"], "detected")
        self.assertEqual([i["name"] for i in runs[0]["items"]], ["openssl"])
        self.assertIn("outside Asusius", runs[0]["items"][0]["reason"])


class CliHistoryTests(FakeEnv):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def seed(self):
        history.append({"start": time.time() - 3600, "end": time.time() - 3500,
                        "trigger": "manual", "duration_s": 100.0, "sources": ["apt"],
                        "cancelled": False, "reboot": False,
                        "summary": "1 of 2 installed, 1 still pending",
                        "items": [{"id": "apt:openssl", "source": "apt", "name": "openssl",
                                   "old": "3.0.13-0ubuntu3.5", "new": "3.0.13-0ubuntu3.6",
                                   "offered": "3.0.13-0ubuntu3.6", "result": "installed",
                                   "reason": ""},
                                  {"id": "apt:linux-image-generic", "source": "apt",
                                   "name": "linux-image-generic", "old": "6.8.0-146",
                                   "new": "6.8.0-146", "offered": "6.8.0-147",
                                   "result": "pending",
                                   "reason": "Needs new packages: it needs packages that "
                                             "are not installed yet.",
                                   "command": "sudo apt full-upgrade"}]})

    def test_text_and_json(self):
        self.seed()
        code, out = self.run_cli("updates", "history")
        self.assertEqual(code, 0)
        self.assertIn("run from Asusius", out)
        self.assertIn("1 of 2 installed, 1 still pending", out)
        self.assertIn("linux-image-generic", out)
        self.assertIn("still pending", out)
        self.assertIn("$ sudo apt full-upgrade", out)
        code, out = self.run_cli("updates", "history", "--json")
        doc = json.loads(out)
        self.assertEqual(doc[0]["items"][1]["result"], "pending")

    def test_filters(self):
        self.seed()
        _c, out = self.run_cli("updates", "history", "--source", "snap")
        self.assertIn("No update runs recorded", out)
        _c, out = self.run_cli("updates", "history", "--since", "bogus")
        self.assertIn("since:", out)
        _c, out = self.run_cli("updates", "history", "--since", "5m")
        self.assertIn("No update runs recorded", out)  # the run is an hour old

    def test_apply_records_history_and_reports_pending(self):
        os.environ["ASUSIUS_FAKE_PENDING"] = "apt:linux-image-generic:amd64"
        code, out = self.run_cli("updates", "check")
        code, out = self.run_cli("updates", "apply", "--source", "apt", "--yes")
        self.assertIn("4 of 5 installed, 1 still pending", out)
        runs = history.read()
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["items"])
        self.assertIn("still pending", runs[0]["summary"])


class AgentHistoryTests(FakeEnv):
    def test_get_update_history(self):
        from asusius.agent.store import STATE_DIR_ENV
        os.environ[STATE_DIR_ENV] = str(self.tmp / "astate")
        self.addCleanup(os.environ.pop, STATE_DIR_ENV, None)
        from asusius.agent.tools import TOOLS, AgentService
        self.assertEqual(TOOLS["get_update_history"].tier, "read")
        history.append({"start": 1, "end": 2, "trigger": "detected", "duration_s": 0.0,
                        "sources": ["apt"], "cancelled": False, "reboot": False,
                        "summary": "1 updated outside Asusius",
                        "items": [{"id": "apt:openssl", "source": "apt", "name": "openssl",
                                   "old": "1", "new": "2", "offered": "2",
                                   "result": "installed", "reason": ""}]})
        svc = AgentService()
        res, err = svc.call("get_update_history", {})
        self.assertFalse(err)
        self.assertEqual(res["runs"][0]["trigger"], "detected")
        # a malformed --since is rejected by the tool schema before the tool runs
        res, err = svc.call("get_update_history", {"since": "bogus"})
        self.assertTrue(err)
        self.assertEqual(res["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
