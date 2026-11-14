# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Regression tests for verified update outcomes, lifecycle and safe mutation boundaries."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from asusius.updates import actions, apt, blockers, history, notify, progress, verify
from asusius.updates.model import Report, SourceResult, UpdateItem


def pkg(name="linux-image-generic", **facts):
    facts = {"kept_back": True, "new_packages": ["linux-image-6.8.0-147-generic"], **facts}
    return UpdateItem("apt:" + name + ":amd64", "apt", name, "146", "147",
                      apply_method="packagekit", extra={"arch": "amd64", "facts": facts,
                      "blocker": blockers.classify(name, facts)})


class DependencySafetyTests(unittest.TestCase):
    def test_confirmation_lists_every_package(self):
        names = ["package-" + str(i) for i in range(40)]
        item = pkg(new_packages=names, upgrades=["upgrade-" + str(i) for i in range(40)])
        text = actions.describe_deps(item)
        for name in names + item.extra["facts"]["upgrades"]:
            self.assertIn(name, text)
        self.assertNotIn(" more", text)

    def test_invalid_and_held_plans_never_apply(self):
        for facts in ({"dpkg_hold": True}, {"removals": ["essential"]},
                      {"held_involved": ["libnvidia-gl-580"]}, {"broken": True},
                      {"new_packages": ["--bad-option"]}):
            with self.subTest(facts=facts), mock.patch.object(actions, "_fake", return_value=False), \
                    mock.patch.object(actions, "pk_install") as install:
                result = actions.apply_deps(pkg(**facts))
                self.assertTrue(result["packagekit-deps"].startswith("error:"))
                install.assert_not_called()

    def test_changed_plan_fails_closed(self):
        original = pkg()
        fresh = SourceResult("apt", items=[pkg(new_packages=["different-package"])])
        with mock.patch.object(actions, "_fake", return_value=False), \
                mock.patch.object(apt, "check", return_value=fresh), \
                mock.patch.object(actions, "pk_install") as install:
            result = actions.apply_deps(original)
        install.assert_not_called()
        self.assertIn("plan changed", result["packagekit-deps"])

    def test_same_plan_calls_backend(self):
        original = pkg()
        fresh = SourceResult("apt", items=[pkg()])
        with mock.patch.object(actions, "_fake", return_value=False), \
                mock.patch.object(apt, "check", return_value=fresh), \
                mock.patch.object(actions, "pk_install", return_value="installed") as install:
            result = actions.apply_deps(original)
        self.assertEqual(result["packagekit-deps"], "installed")
        self.assertEqual(set(install.call_args.args[0]), set(actions.deps_names(original)))

    def test_cancel_does_not_recheck_or_apply(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(apt, "check") as check:
            result = actions.apply_deps(pkg(), cancel=cancel)
        check.assert_not_called()
        self.assertIn("cancelled", result["packagekit-deps"])

    def test_waiting_items_are_excluded_at_boundary(self):
        self.assertEqual(actions.plan([pkg(dpkg_hold=True), pkg(phased_pct=50)]), {})

    def test_packagekit_simulation_rejects_removal(self):
        item_id = "linux-image-6.8.0-147-generic;147;amd64;ubuntu"
        calls = []
        class Tx:
            def __init__(self, *a, **kw):
                pass
            def run(self, method, args):
                calls.append(method)
                return [(2, item_id, "kernel")] if method == "Resolve" else [
                    (6, "libnvidia-gl-580;1;amd64;installed", "remove")]
        with mock.patch.object(actions, "PkTransaction", Tx), \
                mock.patch.object(apt, "dpkg_holds", return_value=set()):
            with self.assertRaises(actions.ActionError):
                actions.pk_install(["linux-image-6.8.0-147-generic"])
        self.assertEqual(calls, ["Resolve", "InstallPackages"])

    def test_packagekit_resolve_requires_every_confirmed_package(self):
        tx = mock.Mock()
        tx.run.return_value = [(2, "one;1;amd64;ubuntu", "one")]
        with mock.patch.object(actions, "PkTransaction", return_value=tx), \
                mock.patch.object(apt, "dpkg_holds", return_value=set()):
            with self.assertRaises(actions.ActionError):
                actions.pk_install(["one", "two"])
        self.assertEqual(tx.run.call_count, 1)


class AptClassificationTests(unittest.TestCase):
    def test_hold_selection_and_targeted_simulation(self):
        self.assertEqual(apt.selection_holds("a:amd64\thold\nb\tinstall\n"), {"a"})
        text = ("Inst kernel [146] (147 Ubuntu:24.04 [amd64])\n"
                "Inst kernel-new (147 Ubuntu:24.04 [amd64])\n"
                "Inst libnvidia-gl-580 [1] (2 Ubuntu:24.04 [amd64])\n"
                "Remv conflict [1]\n")
        facts = apt.simulation_facts(text, "kernel", {"libnvidia-gl-580"})
        self.assertEqual(facts["new_packages"], ["kernel-new"])
        self.assertEqual(facts["removals"], ["conflict"])
        self.assertEqual(facts["held_involved"], ["libnvidia-gl-580"])

    def test_broken_never_fixable_even_with_new_packages(self):
        self.assertFalse(blockers.classify("kernel", {"kept_back": True,
                          "new_packages": ["kernel-new"], "broken": True})["fixable"])

    def test_fallback_keeps_and_classifies_new_dependency_candidate(self):
        item = pkg()
        text = ("Inst linux-image-generic [146] (147 Ubuntu:24.04 [amd64])\n"
                "Inst linux-image-6.8.0-147-generic (147 Ubuntu:24.04 [amd64])\n")
        with mock.patch.object(apt, "_run", side_effect=[text, "Candidate: 147\n"]):
            apt.text_blockers([item], set(), "", text)
        self.assertEqual(item.blocker["blocker"], "needs-new")
        self.assertEqual(item.extra["facts"]["new_packages"],
                         ["linux-image-6.8.0-147-generic"])

    def test_phasing_from_policy_is_distinct_from_user_hold(self):
        item = pkg()
        text = "Inst linux-image-generic [146] (147 Ubuntu:24.04 [amd64])\n"
        with mock.patch.object(apt, "_run", side_effect=[text, "147 (phased 40%)\n"]):
            apt.text_blockers([item], set(), "", text)
        self.assertEqual(item.blocker["blocker"], "phased")
        self.assertFalse(item.blocker["fixable"])


class PackageKitProgressTests(unittest.TestCase):
    def transaction(self):
        tx = object.__new__(actions.PkTransaction)
        tx.watch = progress.RunState().watcher("packagekit")
        tx.progress = mock.Mock()
        tx._percent = tx._speed = tx._remaining_bytes = None
        tx._allow_cancel = False
        tx.packages = []
        tx.error = tx.exit = None
        return tx

    def test_current_properties_drive_auth_speed_cancel_and_reset(self):
        tx = self.transaction()
        props = {"Status": 31, "Percentage": 40, "Speed": 1000,
                 "DownloadSizeRemaining": 3000, "AllowCancel": True}
        tx._properties_changed(None, None, None, None, None,
                               SimpleNamespace(unpack=lambda: (actions.PK_TX, props, [])))
        snap = tx.watch.state.snapshot()
        state = dict(snap["sources"])["packagekit"]
        self.assertTrue(snap["auth"] and snap["cancellable"])
        self.assertEqual((state["percent"], state["remaining_s"]), (40, 3))
        props = {"Status": 9, "Percentage": 101, "Speed": 0, "AllowCancel": False}
        tx._properties_changed(None, None, None, None, None,
                               SimpleNamespace(unpack=lambda: (actions.PK_TX, props, [])))
        snap = tx.watch.state.snapshot()
        state = dict(snap["sources"])["packagekit"]
        self.assertFalse(snap["auth"] or snap["cancellable"])
        self.assertIsNone(state["percent"])
        self.assertIsNone(state["remaining_s"])

    def test_item_progress_uses_third_argument_and_watch_without_callback(self):
        tx = self.transaction()
        params = SimpleNamespace(unpack=lambda: ("kernel;147;amd64;ubuntu", 9, 63))
        tx._signal(None, None, None, None, "ItemProgress", params)
        tx.progress.assert_called_once_with("kernel 63%")
        tx.progress = None
        tx._signal(None, None, None, None, "ItemProgress", params)
        self.assertEqual(dict(tx.watch.state.snapshot()["sources"])["packagekit"]["package"],
                         "kernel")

    def test_done_clears_auth_and_cancel(self):
        tx = self.transaction()
        tx.watch.phase("Waiting for password", auth=True, cancellable=True)
        tx.watch.phase("Done", done=True)
        snap = tx.watch.state.snapshot()
        self.assertFalse(snap["auth"] or snap["cancellable"])


class VerificationRegressionTests(unittest.TestCase):
    def test_firmware_reboot_flag_alone_never_proves_staging(self):
        item = UpdateItem("fw:bios", "firmware", "BIOS", "309", "310",
                          needs_reboot=True, apply_method="fwupd")
        for outcome, cancelled in (({}, False), ({"fwupd": "installed"}, False),
                                   ({"fwupd": "error: cancelled"}, True)):
            result = verify.verify([item], {item.id: "309"}, {item.id: "309"}, outcome,
                                   None, cancelled=cancelled)
            self.assertNotEqual(result[0]["result"], "restart")
        result = verify.verify([item], {item.id: "309"}, {item.id: "309"},
                               {"fwupd": "installed"}, None, staged={item.id: "310"})
        self.assertEqual(result[0]["result"], "restart")

    def test_missing_before_version_never_claims_success(self):
        item = pkg()
        result = verify.verify([item], {item.id: None}, {item.id: "147"},
                               {"packagekit": "done"}, None)
        self.assertEqual(result[0]["result"], "pending")
        self.assertIn("could not verify", result[0]["reason"])

    def test_dpkg_held_installed_status_and_missing_arch(self):
        item = pkg()
        item.extra.pop("arch")
        reply = SimpleNamespace(stdout="linux-image-generic\tamd64\t147\thi \n", returncode=0)
        with mock.patch.object(verify.subprocess, "run", return_value=reply):
            self.assertEqual(verify._apt([item])[item.id], "147")

    def test_same_snap_version_revision_is_actual_change(self):
        item = UpdateItem("snap:app", "snap", "app", "1", "1", apply_method="snapd")
        result = verify.verify([item], {item.id: "1 (revision 2)"},
                               {item.id: "1 (revision 3)"}, {"snapd": "done"}, None)
        self.assertEqual(result[0]["result"], "installed")

    def test_external_update_needs_version_proof_and_healthy_source(self):
        from asusius.updates.scheduler import UpdateScheduler
        item = pkg()
        old = Report(checked_ts=1, sources=[SourceResult("apt", items=[item])])
        new = Report(checked_ts=2, sources=[SourceResult("apt")])
        scheduler = UpdateScheduler()
        with mock.patch.object(verify, "installed_versions", return_value={item.id: "146"}), \
                mock.patch.object(history, "append") as append:
            scheduler._record_detected(new, old)
            append.assert_not_called()
        with mock.patch.object(verify, "installed_versions", return_value={item.id: "147"}), \
                mock.patch.object(history, "append") as append:
            scheduler._record_detected(new, old)
            self.assertEqual(append.call_args.args[0]["items"][0]["new"], "147")
        new.sources[0].error = "offline"
        with mock.patch.object(verify, "installed_versions") as read:
            scheduler._record_detected(new, old)
            read.assert_not_called()


class HistorySafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "history.jsonl"

    def test_symlink_lock_or_log_is_refused(self):
        target = self.path.with_name("original")
        target.write_text("preserve me")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            history.append({"start": 1}, self.path)
        self.assertEqual(target.read_text(), "preserve me")
        self.path.unlink()
        self.path.with_name(self.path.name + ".lock").unlink()
        self.path.with_name(self.path.name + ".lock").symlink_to(target)
        with self.assertRaises(OSError):
            history.append({"start": 1}, self.path)

    def test_bad_shapes_and_timestamps_are_ignored(self):
        self.path.write_text('\n'.join(json.dumps(v) for v in [
            {"start": "yesterday"}, {"start": float("nan")}, {"items": "oops"},
            {"start": 1, "items": [None, {"source": "apt"}]}]))
        rows = history.read(path=self.path)
        self.assertEqual(rows, [{"start": 1, "items": [{"source": "apt"}]}])

    def test_partial_writes_and_private_mode(self):
        self.path.write_text("")
        self.path.chmod(0o644)
        real_write = os.write
        with mock.patch.object(history.os, "write", side_effect=lambda fd, data:
                               real_write(fd, data[:7])):
            history.append({"start": 1, "items": []}, self.path)
        self.assertEqual(len(history.read(path=self.path)), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_writers_do_not_lose_or_interleave_records(self):
        errors = []
        def write(n):
            try:
                for k in range(20):
                    history.append({"start": n * 100 + k}, self.path)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertFalse(errors)
        self.assertEqual(len(history.read(path=self.path)), 80)


class NotificationTests(unittest.TestCase):
    def tearDown(self):
        notify.close_clicks()

    def test_single_watch_is_replaced_and_unsubscribed(self):
        conn = mock.Mock()
        conn.signal_subscribe.return_value = 42
        notify._watch_clicks(conn, 1)
        first = notify._click_watch
        notify._watch_clicks(conn, 2)
        self.assertFalse(first.thread.is_alive())
        conn.signal_unsubscribe.assert_called_with(42)
        notify.close_clicks()
        self.assertEqual(conn.signal_unsubscribe.call_count, 2)

    def test_only_matching_default_action_opens_app_and_closed_stops(self):
        watch = object.__new__(notify._ClickWatch)
        watch.nid, watch.stop = 3, threading.Event()
        with mock.patch.object(notify, "_launch_app") as launch:
            for nid, action in ((2, "default"), (3, "other")):
                watch._signal(None, None, None, None, "ActionInvoked",
                              SimpleNamespace(unpack=lambda: (nid, action)))
            launch.assert_not_called()
            watch._signal(None, None, None, None, "ActionInvoked",
                          SimpleNamespace(unpack=lambda: (3, "default")))
            launch.assert_called_once()
            self.assertTrue(watch.stop.is_set())
        watch.stop.clear()
        watch._signal(None, None, None, None, "NotificationClosed",
                      SimpleNamespace(unpack=lambda: (3, 2)))
        self.assertTrue(watch.stop.is_set())

    def test_desktop_action_uses_supported_list_actions_api(self):
        from gi.repository import Gio
        app = mock.Mock()
        app.list_actions.return_value = ["updates"]
        with mock.patch.object(notify.shutil, "which", return_value=None), \
                mock.patch.object(Gio.DesktopAppInfo, "new", return_value=app):
            notify._launch_app()
        app.launch_action.assert_called_once_with("updates", None)


class AgentStateSafetyTests(unittest.TestCase):
    def test_nonfinite_grants_fail_closed_and_future_not_effective(self):
        from asusius.agent import policy
        for val in (float("inf"), float("nan"), float("-inf"), 10**1000):
            with self.assertRaises(ValueError):
                policy.validate_access({"standing_grant": {
                    "level": "control-basic", "granted_at": 1, "expires_at": val}})
        with self.assertRaises(ValueError):
            policy.validate_access({"standing_grant": {
                "level": "control-basic", "granted_at": 3, "expires_at": 2}})
        acc = policy.Access(standing_grant={"level": "control-basic", "granted_at": 100,
                                           "expires_at": 200})
        self.assertIsNone(acc.standing(50))
        self.assertFalse(acc.pre_approved("basic", 50))

    def test_audit_rejects_symlinks_and_repairs_permissions(self):
        from asusius.agent.audit import AuditLog
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "audit.log"
            target = Path(tmp) / "target"
            target.write_text("original")
            p.symlink_to(target)
            log = AuditLog(p)
            with contextlib.redirect_stderr(io.StringIO()):
                log.write(client="test", kind="tool", name="read", decision="allow")
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(log.tail(), [])
            p.unlink()
            p.touch(mode=0o644)
            log.write(client="test", kind="tool", name="read", decision="allow")
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(log.tail()), 1)


class SnapProgressTests(unittest.TestCase):
    def test_real_task_dictionary_becomes_progress_text(self):
        from asusius.updates import snap
        docs = [{"change": "7"}, {"result": {"ready": False, "tasks": [
            {"status": "Do", "summary": "Download app", "progress": {"done": 1, "total": 2}}]}},
            {"result": {"ready": True, "summary": "done"}}]
        st = progress.RunState()
        with mock.patch.object(snap, "snapd", side_effect=docs), \
                mock.patch.object(snap.time, "sleep"):
            self.assertEqual(snap.refresh(["app"], watch=st.watcher("snapd")), "done")
        self.assertEqual(dict(st.snapshot()["sources"])["snapd"]["package"], "Download app")

    def test_timeout_is_not_a_success(self):
        from asusius.updates import snap
        with mock.patch.object(snap, "snapd", return_value={"change": "7"}):
            with self.assertRaises(TimeoutError):
                snap.refresh(["app"], timeout_s=0)


class GpuLostRegressionTests(unittest.TestCase):
    def test_lost_gpu_without_nvml_never_polls(self):
        from asusius.telemetry import gpu_nvidia as gpu
        dev = {"vendor": "0x10de", "device": "0x1234", "addr": "0000:01:00.0",
               "dir": "/fake/device"}
        with mock.patch.object(gpu, "display_devices", return_value=[dev]), \
                mock.patch.object(gpu, "pci_name", return_value="Fake GPU"), \
                mock.patch.object(gpu, "Nvml", side_effect=AttributeError("missing symbol")), \
                mock.patch("shutil.which", return_value=None):
            card = gpu.NvidiaGpu()
        self.assertIsNone(card.backend)
        with mock.patch.object(gpu, "runtime_state", return_value=("error", "D0")), \
                mock.patch.object(card, "_query_nvml") as nvml, \
                mock.patch.object(card, "_query_smi") as smi:
            info = {}
            card.sample(100, {}, info)
        nvml.assert_not_called()
        smi.assert_not_called()
        self.assertEqual(info["dgpu"]["state"], "lost")
        self.assertIn("Restart", info["dgpu"]["reason"])
        self.assertEqual(card.procs, {})

    def test_error_after_nvml_initialization_releases_without_query(self):
        from asusius.telemetry import gpu_nvidia as gpu
        dev = {"vendor": "0x10de", "device": "0x1234", "addr": "0000:01:00.0",
               "dir": "/fake/device"}
        with mock.patch.object(gpu, "display_devices", return_value=[dev]), \
                mock.patch.object(gpu, "pci_name", return_value="Fake GPU"), \
                mock.patch.object(gpu, "Nvml") as nv:
            card = gpu.NvidiaGpu()
        card.handle = object()
        with mock.patch.object(gpu, "runtime_state", return_value=("error", "D0")):
            info = {}
            card.sample(100, {}, info)
        nv.return_value.shutdown.assert_called_once()
        nv.return_value.init.assert_not_called()
        nv.return_value.query.assert_not_called()
        self.assertIsNone(card.handle)


class DaemonSafetyRegressionTests(unittest.TestCase):
    def test_forced_dry_run_disables_configured_effect(self):
        from asusius.automation.config import update_config
        from asusius.backends.fake import build_fake_tree, FakeBackend
        from asusius.controller import Controller
        from asusius.daemon import Daemon
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            root = build_fake_tree(path / "root")
            cfg = path / "config.json"
            update_config(lambda c: c["effects"].update(active="accent"), cfg)
            with mock.patch.dict(os.environ, {"ASUSIUS_FAKE_ROOT": str(root)}):
                daemon = Daemon(Controller([FakeBackend(root)]), config_file=cfg,
                                status_file=path / "status.json", dry_run=True)
                daemon.reload(force=True)
                daemon.effects.step(0.1)
                daemon.effects.step(0.2)
            self.assertIsNone(daemon.effects.cfg["active"])
            self.assertEqual(daemon.effects.writer.writes, 0)

    def test_detached_log_is_private_and_bounded_and_reaps_children(self):
        from asusius.automation import service
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "daemon.log"
            raw = io.BytesIO(b"x" * (2 * service.MAX_LOG_BYTES + 100))
            with mock.patch.object(service, "_SPAWNED", [
                SimpleNamespace(poll=lambda: 0), SimpleNamespace(poll=lambda: None)]):
                service._capture_log(raw, path)
                self.assertEqual(len(service._SPAWNED), 1)
            self.assertLessEqual(path.stat().st_size, service.MAX_LOG_BYTES)
            self.assertLessEqual(path.with_name("daemon.log.1").stat().st_size,
                                 service.MAX_LOG_BYTES)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class FlatpakProgressTests(unittest.TestCase):
    def test_streaming_child_reports_progress_without_flatpak(self):
        import sys
        st = progress.RunState()
        cmd = [sys.executable, "-u", "-c", "print('Downloading app 40%',flush=True);print('Done',flush=True)"]
        with mock.patch("asusius.updates.flatpak.update_cmd", return_value=cmd):
            self.assertEqual(actions.flatpak_update("user", ["demo.App"],
                             watch=st.watcher("flatpak-user")), "flatpak (user) updated")
        self.assertIn("Downloading app 40%", "\n".join(st.snapshot()["log"]))

    def test_cancel_terminates_only_the_started_child(self):
        import sys
        stop = threading.Event()
        cmd = [sys.executable, "-u", "-c",
               "import time;print('Downloading app 1%',flush=True);time.sleep(60)"]
        with mock.patch("asusius.updates.flatpak.update_cmd", return_value=cmd):
            with self.assertRaises(actions._Cancelled):
                actions.flatpak_update("user", ["demo.App"], progress=lambda _line: stop.set(),
                                       cancel=stop)

    def test_reference_options_and_traversal_rejected(self):
        from asusius.updates.flatpak import update_cmd
        for ref in ("--installation=other", "/tmp/other", "../../file", "app//stable"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                update_cmd("user", [ref])
        with self.assertRaises(ValueError):
            update_cmd("other", ["app"])


class HistoryFailureResultTests(unittest.TestCase):
    def test_history_write_failure_preserves_verified_results(self):
        from asusius.updates import runner
        item = pkg()
        state = progress.RunState()
        with mock.patch.object(verify, "before_versions", return_value={item.id: "146"}), \
                mock.patch.object(verify, "installed_versions", return_value={item.id: "147"}), \
                mock.patch.object(verify, "staged_firmware", return_value={}), \
                mock.patch.object(actions, "apply", return_value={"packagekit": "done"}), \
                mock.patch.object(history, "append", side_effect=OSError("disk full")):
            result = runner.apply_run([item], state, check_fn=lambda: Report())
        self.assertEqual(result["results"][0]["result"], "installed")
        self.assertIn("history could not be saved", result["summary"])
        self.assertEqual(result["history_error"], "disk full")
        self.assertFalse(state.running)


class UpdateCancellationBoundaryTests(unittest.TestCase):
    def test_cancel_during_discovery_prevents_package_install(self):
        stop = threading.Event()
        calls = []
        class Tx:
            def __init__(self, *args, **kwargs):
                pass
            def run(self, method, args):
                calls.append(method)
                stop.set()
                return [(8, "openssl;2;amd64;repo", "")]
        with mock.patch.object(actions, "PkTransaction", Tx), \
                mock.patch.object(apt, "dpkg_holds", return_value=set()):
            with self.assertRaises(actions.ActionError):
                actions.pk_update(["openssl"], cancel=stop)
        self.assertEqual(calls, ["GetUpdates"])

    def test_cancelled_firmware_never_downloads_or_flashes(self):
        stop = threading.Event()
        stop.set()
        item = UpdateItem("firmware:test", "firmware", "Firmware", "1", "2",
                          apply_method="fwupd")
        with mock.patch.object(actions, "_fetch_lvfs") as fetch:
            with self.assertRaises(actions.ActionError):
                actions.fwupd_install(item, cancel=stop)
        fetch.assert_not_called()

    def test_cancel_during_firmware_download_prevents_flash(self):
        from asusius import dbusutil
        stop = threading.Event()
        item = UpdateItem("firmware:test", "firmware", "Firmware", "1", "2",
                          apply_method="fwupd", extra={"locations": ["https://fwupd.org/test.cab"]})
        def fetched(*args):
            stop.set()
            return b"cabinet"
        with mock.patch.object(actions, "_fetch_lvfs", side_effect=fetched), \
                mock.patch.object(dbusutil, "call") as call:
            with self.assertRaises(actions.ActionError):
                actions.fwupd_install(item, cancel=stop)
        call.assert_not_called()

    def test_cancel_between_firmware_devices_skips_later_devices(self):
        stop = threading.Event()
        items = [UpdateItem("firmware:"+n, "firmware", n, "1", "2",
                            apply_method="fwupd") for n in ("one", "two")]
        flashed = []
        def install(item, *args, cancel=None, **kwargs):
            actions._check_cancelled(cancel)
            flashed.append(item.name)
            stop.set()
            return "installed"
        with mock.patch.object(actions, "_fake", return_value=False), \
                mock.patch.object(actions, "fwupd_install", side_effect=install):
            result = actions.apply(items, cancel=stop)
        self.assertEqual(flashed, ["one"])
        self.assertIn("cancelled", result["fwupd"])
