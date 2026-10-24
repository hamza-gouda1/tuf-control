"""Update sources (parsers on fixtures), the report, change detection and notifications, the
background scheduler, actions in fake mode, settings and the CLI. Nothing here touches the
real package managers or firmware: sources are fed fixtures, actions run in fake mode."""
import contextlib
import http.server
import io
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tuf_control import cli
from tuf_control.automation.config import CONFIG_DIR_ENV, ConfigError, validate_config
from tuf_control.controller import FAKE_ROOT_ENV
from tuf_control.updates import actions, apt, check, firmware, flatpak, notify, repos, snap, \
    system
from tuf_control.updates.model import Report, SourceResult, UpdateItem, age_text, human_size
from tuf_control.updates.scheduler import UpdateScheduler, mark_seen
from tuf_control.updates.settings import validate_updates_section

FIX = Path(__file__).resolve().parent / "fixtures" / "updates"


def fx(name: str) -> str:
    return (FIX / name).read_text()


class FakeEnv(unittest.TestCase):
    """A fake hardware root: fake update sources, cache inside the temporary tree."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for k, v in ((FAKE_ROOT_ENV, self.tmp / "root"), (CONFIG_DIR_ENV, self.tmp / "cfg")):
            os.environ[k] = str(v)
            self.addCleanup(os.environ.pop, k, None)
        (self.tmp / "root").mkdir()


# -- parsers ---------------------------------------------------------------------------------
class AptTests(unittest.TestCase):
    def test_apt_list(self):
        items = apt.parse_apt_list(fx("apt_list.txt"))
        self.assertEqual([i.name for i in items], ["containerd", "linux-headers-generic",
                                                   "linux-image-generic", "libnvidia-gl-580"])
        c, h, k, nv = items
        self.assertEqual((c.current, c.available, c.security), (
            "2.2.1-0ubuntu1~24.04.3", "2.2.1-0ubuntu1~24.04.4", False))
        self.assertTrue(k.security and k.needs_reboot and k.kind == "kernel")
        self.assertEqual((nv.kind, nv.extra["arch"], nv.id), ("driver", "i386",
                                                              "apt:libnvidia-gl-580:i386"))
        self.assertTrue(all(i.apply_method == "packagekit" for i in items))

    def test_apt_get_simulation(self):
        items = apt.parse_apt_get_sim(fx("apt_get_sim.txt"))
        by = {i.name: i for i in items}
        self.assertEqual(set(by), {"containerd", "linux-image-6.8.0-147-generic",
                                   "linux-image-generic", "openssl"})
        self.assertIsNone(by["linux-image-6.8.0-147-generic"].current)  # a new install
        self.assertTrue(by["openssl"].security)
        self.assertFalse(by["containerd"].security)
        self.assertEqual(by["openssl"].extra["arch"], "amd64")
        self.assertEqual(by["linux-image-generic"].current, "6.8.0-146.146")

    def test_check_falls_back_to_apt_get(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        (root / "var/lib/dpkg").mkdir(parents=True)
        (root / "var/lib/dpkg/status").write_text("")
        (root / "var/lib/apt/periodic").mkdir(parents=True)
        stamp = root / "var/lib/apt/periodic/update-success-stamp"
        stamp.write_text("")
        os.utime(stamp, (1_700_000_000, 1_700_000_000))
        with mock.patch.object(apt, "_run", return_value=fx("apt_get_sim.txt")) as run, \
                mock.patch.object(apt.shutil, "which", return_value="/usr/bin/apt-get"):
            res = apt.check(root, use_python_apt=False)
        self.assertIn("Debug::NoLocking=1", run.call_args[0][0])
        self.assertEqual(res.info["method"], "apt-get -s")
        self.assertEqual(len(res.items), 3)  # the new kernel package rides on its meta
        self.assertEqual(res.info["lists_refreshed_ts"], 1_700_000_000)

    def test_not_debian(self):
        res = apt.check(Path(tempfile.mkdtemp()))
        self.assertFalse(res.available)
        self.assertIn("dpkg", res.reason)

    def test_classify(self):
        self.assertEqual(apt.classify("linux-image-7.0.13-3-liquorix-amd64"), ("kernel", True))
        self.assertEqual(apt.classify("nvidia-driver-580-open"), ("driver", True))
        self.assertEqual(apt.classify("libc6"), ("system", True))
        self.assertEqual(apt.classify("vim"), ("package", False))


class SnapTests(unittest.TestCase):
    def test_parse_refresh(self):
        items = snap.parse_refresh(json.loads(fx("snap_installed.json"))["result"],
                                   json.loads(fx("snap_refresh.json"))["result"])
        core, ff = items
        self.assertEqual((core.name, core.current, core.available, core.kind),
                         ("core22", "20260824", "20260909", "runtime"))
        self.assertEqual(core.size, 77619200)
        self.assertEqual((ff.kind, ff.extra["publisher"], ff.apply_method), ("app", "Mozilla",
                                                                              "snapd"))

    def test_parse_refresh_list(self):
        (i,) = snap.parse_refresh_list(fx("snap_refresh_list.txt"))
        self.assertEqual((i.name, i.available, i.extra["revision"]), ("core22", "20260909",
                                                                     "2987"))
        self.assertEqual(snap.parse_refresh_list("All snaps up to date.\n"), [])

    def test_rest_over_a_unix_socket(self):
        """check() and refresh() against a stand-in snapd on a private socket."""
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, doc):
                body = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("X-Allow-Interaction")))
                if self.path == "/v2/snaps":
                    self._send(json.loads(fx("snap_installed.json")))
                elif self.path.startswith("/v2/find"):
                    self._send(json.loads(fx("snap_refresh.json")))
                elif self.path == "/v2/changes/42":
                    self._send({"result": {"ready": True, "summary": "Refresh core22"}})

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                seen.append(("POST", self.path, self.headers.get("X-Allow-Interaction"),
                             json.loads(self.rfile.read(n))))
                self._send({"type": "async", "change": "42"})

        class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def get_request(self):
                req, _ = super().get_request()
                return req, ("local", 0)

        d = tempfile.mkdtemp()
        sock = Path(d) / "snapd.socket"
        srv = S(str(sock), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        res = snap.check(sock)
        self.assertEqual([i.name for i in res.items], ["core22", "firefox"])
        self.assertEqual(snap.refresh(["core22"], sock=sock), "Refresh core22")
        post = next(s for s in seen if s[0] == "POST")
        self.assertEqual(post[2], "true")  # snapd may ask polkit
        self.assertEqual(post[3], {"action": "refresh", "snaps": ["core22"]})

    def test_missing_socket(self):
        res = snap.check(Path("/nonexistent/snapd.socket"))
        self.assertFalse(res.available)


class FlatpakTests(unittest.TestCase):
    def test_parse(self):
        installed = flatpak.parse_list(fx("flatpak_list.txt"))
        self.assertEqual(installed[("io.missioncenter.MissionCenter", "stable", "system")],
                         "1.1.0")
        items = flatpak.parse_remote_ls(fx("flatpak_remote_ls_system.txt"), "system",
                                        installed)
        mc, plat, gl = items
        self.assertEqual((mc.current, mc.available, mc.size, mc.kind), ("1.1.0", "1.2.0",
                                                                        4_200_000, "app"))
        self.assertEqual(plat.kind, "runtime")
        self.assertEqual(gl.kind, "runtime")
        self.assertEqual(gl.size, 298_100_000)  # decimal comma
        self.assertEqual(mc.apply_method, "flatpak-system")
        self.assertEqual(mc.extra["ref"], "io.missioncenter.MissionCenter/x86_64/stable")
        (u,) = flatpak.parse_remote_ls(fx("flatpak_remote_ls_user.txt"), "user", installed)
        self.assertEqual((u.apply_method, u.current, u.size), ("flatpak-user", "3.0", 1100))

    def test_check_runs_cached_per_installation(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            out = fx("flatpak_list.txt") if cmd[1] == "list" else fx(
                "flatpak_remote_ls_user.txt" if "--user" in cmd else
                "flatpak_remote_ls_system.txt")
            return mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(flatpak, "_run", run), \
                mock.patch.object(flatpak.shutil, "which", return_value="/usr/bin/flatpak"):
            res = flatpak.check()
        self.assertEqual(len(res.items), 4)
        self.assertTrue(all("--cached" in c for c in calls if "remote-ls" in c))
        self.assertEqual(flatpak.update_cmd("user", ["a/x/b"]),
                         ["flatpak", "update", "--user", "-y", "--noninteractive", "a/x/b"])


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.d = json.loads(fx("fwupd.json"))

    def test_parse(self):
        items, info = firmware.parse(self.d["devices"], self.d["upgrades"], self.d["remotes"])
        by = {i.name: i for i in items}
        bios = by["System Firmware"]
        self.assertEqual((bios.current, bios.available, bios.kind), ("309", "310", "bios"))
        self.assertTrue(bios.security and bios.needs_reboot and bios.extra["require_ac"])
        self.assertEqual(bios.extra["issues"], ["CVE-2026-1234"])
        self.assertIn("• Updates the Intel microcode", bios.extra["details"])
        self.assertEqual(bios.apply_method, "fwupd")
        self.assertFalse(by["UEFI dbx"].security)  # medium urgency, no CVEs
        self.assertEqual(by["UEFI dbx"].kind, "secure-boot")
        self.assertEqual(info["updatable"], 3)
        self.assertEqual(info["metadata_ts"], 1792710267)  # disabled/unset remotes ignored

    def test_check_with_a_client(self):
        class Client:
            asked = []

            def devices(s):
                return self.d["devices"]

            def remotes(s):
                return self.d["remotes"]

            def upgrades(s, did):
                s.asked.append(did)
                return self.d["upgrades"].get(did, [])
        c = Client()
        res = firmware.check(c)
        self.assertEqual(len(res.items), 2)
        self.assertNotIn("4bde70ba00000000000000000000000000000000", c.asked)  # not updatable

    def test_errors_become_a_message(self):
        class Broken:
            def devices(self):
                raise RuntimeError("fwupd is busy")
        res = firmware.check(Broken())
        self.assertIn("fwupd is busy", res.error)


class SystemTests(unittest.TestCase):
    def root(self):
        r = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(r))
        return r

    def test_kernel(self):
        self.assertEqual(system.parse_kernel("7.0.13-3-liquorix-amd64"),
                         ((7, 0, 13, 3), "liquorix-amd64"))
        self.assertEqual(system.parse_kernel("6.8.0-147-generic"), ((6, 8, 0, 147), "generic"))
        inst = ["6.8.0-146-generic", "7.0.0-38-generic", "7.0.13-3-liquorix-amd64",
                "7.0.14-1-liquorix-amd64"]
        k = system.kernel_item("7.0.13-3-liquorix-amd64", inst)
        self.assertEqual(k.available, "7.0.14-1-liquorix-amd64")
        self.assertEqual(k.apply_method, "reboot")
        self.assertIsNone(system.kernel_item("7.0.13-3-liquorix-amd64", inst[:3]))
        # a newer kernel of another flavour is not "pending"
        self.assertIsNone(system.kernel_item("6.8.0-146-generic", ["6.8.0-146-generic",
                                                                   "7.0.13-3-liquorix-amd64"]))

    def test_reboot_required_and_nvidia(self):
        r = self.root()
        (r / "run").mkdir()
        (r / "boot").mkdir()
        (r / "run/reboot-required").write_text("*** System restart required ***\n")
        (r / "run/reboot-required.pkgs").write_text("linux-image-6.8.0-147-generic\nlibc6\n")
        (r / "sys/module/nvidia").mkdir(parents=True)
        (r / "sys/module/nvidia/version").write_text("580.178.04\n")
        lib = r / "usr/lib/x86_64-linux-gnu"
        lib.mkdir(parents=True)
        (lib / "libnvidia-ml.so.580.182.01").write_text("")
        res = system.check(r, running="7.0.13-3-liquorix-amd64", modinfo=lambda: "580.182.01")
        by = {i.id: i for i in res.items}
        self.assertEqual(by["system:reboot-required"].extra["packages"],
                         ["libc6", "linux-image-6.8.0-147-generic"])
        nv = by["system:nvidia"]
        self.assertEqual((nv.current, nv.available), ("580.178.04", "580.182.01"))
        self.assertFalse(any(i.counts for i in res.items))  # hints, not updates

    def test_nvidia_matching_is_quiet(self):
        self.assertIsNone(system.nvidia_item({"loaded": "580.178.04", "module": "580.178.04",
                                              "userspace": "580.178.04"}))
        self.assertIsNone(system.nvidia_item({"loaded": None}))


class ReposTests(unittest.TestCase):
    def test_behind(self):
        answers = {("rev-parse", "--abbrev-ref", "@{u}"): "origin/main",
                   ("rev-list", "--count", "HEAD..@{u}"): "3",
                   ("rev-parse", "--short", "HEAD"): "67fe971",
                   ("rev-parse", "--short", "@{u}"): "a1b2c3d"}
        it = repos.repo_item(Path("/x/tuf-control"), lambda p, *a: answers[a])
        self.assertEqual((it.name, it.current, it.available, it.extra["behind"]),
                         ("tuf-control", "67fe971", "a1b2c3d", 3))
        answers[("rev-list", "--count", "HEAD..@{u}")] = "0"
        self.assertIsNone(repos.repo_item(Path("/x/tuf-control"), lambda p, *a: answers[a]))


# -- report, notifications, scheduler -----------------------------------------------------------
def report(*pairs, ts=1000.0, error_source=None) -> Report:
    srcs: dict[str, SourceResult] = {}
    for src, name, ver, *sec in pairs:
        srcs.setdefault(src, SourceResult(src)).items.append(UpdateItem(
            id=f"{src}:{name}", source=src, name=name, current="1", available=ver,
            security=bool(sec and sec[0]), apply_method="packagekit"))
    if error_source:
        srcs[error_source] = SourceResult(error_source, error="offline")
    return Report(checked_ts=ts, sources=list(srcs.values()))


class ReportTests(unittest.TestCase):
    def test_summary_and_roundtrip(self):
        r = report(("apt", "a", "2"), ("apt", "b", "3", True), ("firmware", "bios", "310"))
        self.assertEqual(r.summary(), "3 updates available (apt 2, firmware 1)")
        self.assertEqual(r.security_count, 1)
        back = Report.from_dict(json.loads(json.dumps(r.to_dict())))
        self.assertEqual(back.signature(), r.signature())
        self.assertEqual(Report().summary(), "Everything is up to date")
        r.sources.append(SourceResult("system", items=[UpdateItem(
            id="system:kernel", source="system", name="Linux kernel",
            apply_method="reboot")]))
        self.assertEqual(r.total, 3)  # a pending restart is not an update

    def test_age_and_size(self):
        self.assertEqual(age_text(None), "never")
        self.assertEqual(age_text(100, now=130), "just now")
        self.assertEqual(age_text(0.5, now=7200.5), "2 hours ago")
        self.assertEqual(age_text(1, now=1 + 86400), "1 day ago")
        self.assertEqual(human_size(77_619_200), "77.6 MB")
        self.assertEqual(human_size(None), "")


class NotifyTests(unittest.TestCase):
    def test_announce_once(self):
        r1 = report(("apt", "a", "2"), ("snap", "core22", "9"))
        text, st = notify.decide(r1, None)
        self.assertEqual(text, "2 updates available (apt 1, snap 1)")
        self.assertIsNone(notify.decide(r1, st)[0])               # same set: quiet
        r2 = report(("apt", "a", "2"))                            # one installed: quiet
        self.assertIsNone(notify.decide(r2, st)[0])
        r3 = report(("apt", "a", "2"), ("snap", "core22", "9"),   # source fails then returns
                    error_source="flatpak")
        self.assertIsNone(notify.decide(r3, st)[0])
        r4 = report(("apt", "a", "3"), ("snap", "core22", "9"), ("firmware", "bios", "310", True))
        text, st = notify.decide(r4, st)                          # newer version + new item
        self.assertEqual(text, "3 updates available (apt 1, snap 1, firmware 1) · 1 security")
        self.assertIsNone(notify.decide(r4, st)[0])
        self.assertIsNone(notify.decide(Report(), st)[0])         # all done: quiet

    def test_body(self):
        r = report(*[("apt", f"p{i}", "2") for i in range(5)])
        r.reboot_required = True
        self.assertIn("and 2 more", notify.body_text(r))
        self.assertIn("restart", notify.body_text(r))


class SchedulerTests(FakeEnv):
    def make(self, metered=False, runner=None, **settings):
        self.clock = [100_000.0]
        self.shown = []
        s = UpdateScheduler(clock=lambda: self.clock[0], runner=runner or self.runner,
                            notifier=lambda t, b: self.shown.append(t),
                            metered=lambda: metered, first_delay_s=60)
        s.configure({"enabled": True, "interval_h": 6, "respect_metered": True,
                     "notify": True, **settings})
        return s

    def runner(self):
        return check.run_check()

    def run_tick(self, s):
        started = s.tick()
        if started:
            s.worker.join(10)
        return started

    def test_cadence_and_dedupe(self):
        s = self.make()
        self.assertFalse(s.tick())               # first delay after start
        self.clock[0] += 61
        self.assertTrue(self.run_tick(s))
        self.assertEqual(len(self.shown), 1)
        self.assertIn("updates available", self.shown[0])
        # the cache says "checked just now" (wall clock): not due for 6 hours
        s.last_ts = self.clock[0]
        with mock.patch("tuf_control.updates.scheduler.load_cache", return_value={}):
            self.clock[0] += 3600
            self.assertFalse(s.due())
            self.clock[0] += 5 * 3600
            self.assertTrue(s.due())
        s.last_ts = time.time() + 10 ** 6
        self.assertFalse(s.due())

    def test_second_check_does_not_nag(self):
        s = self.make()
        self.clock[0] += 61
        self.run_tick(s)
        s.after_check(check.load_report())
        self.assertEqual(len(self.shown), 1)

    def test_metered_and_disabled(self):
        s = self.make(metered=True)
        self.clock[0] += 61
        self.assertFalse(s.tick())
        self.assertEqual(s.skipped, "metered connection")
        self.assertFalse(s.due(self.clock[0] + 60))  # backs off
        s = self.make(metered=True, respect_metered=False)
        self.clock[0] += 61
        self.assertTrue(self.run_tick(s))
        s = self.make(enabled=False)
        self.clock[0] += 61
        self.assertFalse(s.tick())

    def test_failure_backs_off(self):
        def boom():
            raise RuntimeError("no network")
        s = self.make(runner=boom)
        self.clock[0] += 61
        self.assertTrue(self.run_tick(s))
        self.assertEqual(s.last_error, "no network")
        self.assertFalse(s.due())

    def test_notify_off_still_remembers(self):
        s = self.make(notify=False)
        self.clock[0] += 61
        self.run_tick(s)
        self.assertEqual(self.shown, [])
        s.configure({"notify": True})
        self.assertIsNone(s.after_check(check.load_report()))

    def test_gui_check_marks_seen(self):
        rep = check.run_check()
        mark_seen(rep)
        s = self.make()
        self.assertIsNone(s.after_check(rep))


# -- check, cache, actions, settings, CLI -----------------------------------------------------------
class CheckTests(FakeEnv):
    def test_fake_report_and_cache(self):
        self.assertEqual(check.cache_dir(), self.tmp / "root" / "fake-cache")
        rep = check.run_check()
        self.assertTrue(rep.fake)
        self.assertEqual(rep.total, 9)
        self.assertTrue(rep.reboot_required)
        self.assertEqual(check.load_report().signature(), rep.signature())
        os.environ["TUF_CONTROL_FAKE_UPDATES"] = "none"
        self.addCleanup(os.environ.pop, "TUF_CONTROL_FAKE_UPDATES", None)
        self.assertEqual(check.run_check().summary(), "Everything is up to date")

    def test_child_process(self):
        rep = check.check_in_child(timeout_s=60)
        self.assertEqual(rep.total, 9)

    def test_lock_waits_for_the_running_check(self):
        with check.check_lock() as mine:
            self.assertTrue(mine)
            self.assertTrue(check.is_checking())
            with check.check_lock(wait=False) as other:
                self.assertIsNone(other)
        self.assertFalse(check.is_checking())

    def test_broken_source_does_not_break_the_report(self):
        res = check._timed("apt", lambda: 1 / 0)
        self.assertIn("ZeroDivisionError", res.error)


class ActionTests(FakeEnv):
    def test_fake_actions_are_recorded_only(self):
        rep = check.collect()
        out = actions.apply(rep.items())
        self.assertEqual(set(out), {"packagekit", "snapd", "flatpak-system", "fwupd"})
        log = (check.cache_dir() / "actions.log").read_text()
        self.assertIn("apply fwupd: firmware:3b27a1b5c0de", log)
        self.assertNotIn("system:kernel", log)  # reboot hints are never "applied"
        self.assertIn("refresh apt", "".join(actions.refresh_sources(["apt"]).values()))
        self.assertIn("restart", actions.restart())

    def test_plan_and_describe(self):
        rep = check.collect()
        plan = actions.plan(rep.items())
        self.assertNotIn("reboot", plan)
        self.assertIn("Firmware: System Firmware", actions.describe(rep.items()))

    def test_firmware_download_rules(self):
        self.assertTrue(actions.firmware_url_ok("https://fwupd.org/downloads/x.cab"))
        self.assertFalse(actions.firmware_url_ok("http://fwupd.org/downloads/x.cab"))
        self.assertFalse(actions.firmware_url_ok("https://evil.example/x.cab"))
        p = self.tmp / "x.cab"
        p.write_bytes(b"cab")
        import hashlib
        self.assertTrue(actions._checksum_ok(p, [hashlib.sha256(b"cab").hexdigest()]))
        self.assertFalse(actions._checksum_ok(p, ["00"]))
        self.assertFalse(actions._checksum_ok(p, []))

    def test_notification_in_fake_mode_is_logged(self):
        notify.send("3 updates available")
        self.assertIn("3 updates", (check.cache_dir() / "notifications.log").read_text())


class SettingsTests(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(validate_config({})["updates"]["interval_h"], 6.0)
        out = validate_updates_section({"interval_h": 12, "sources": {"snap": False}})
        self.assertFalse(out["sources"]["snap"])
        self.assertTrue(out["sources"]["apt"])
        for bad in ({"interval_h": 1}, {"interval_h": True}, {"nope": 1},
                    {"sources": {"pip": True}}, {"repos": "x"}, {"enabled": "yes"}):
            with self.assertRaises(ConfigError, msg=bad):
                validate_updates_section(bad)


class CliTests(FakeEnv):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_list_check_apply(self):
        code, out = self.run_cli("updates")
        self.assertIn("No update check yet", out)
        code, out = self.run_cli("updates", "check")
        self.assertIn("9 updates available", out)
        self.assertIn("System Firmware", out)
        code, out = self.run_cli("updates", "list", "--json")
        self.assertEqual(json.loads(out)["total"], 9)
        code, out = self.run_cli("updates", "apply", "--source", "snap")
        self.assertIn("Re-run with --yes", out)
        self.assertFalse((check.cache_dir() / "actions.log").exists())
        code, out = self.run_cli("updates", "apply", "--source", "firmware")
        self.assertIn("charger", out)  # the firmware warning
        code, out = self.run_cli("updates", "apply", "--source", "snap", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("apply snapd: snap:core22",
                      (check.cache_dir() / "actions.log").read_text())


class AgentToolTests(FakeEnv):
    def test_get_updates(self):
        from tuf_control.agent.store import STATE_DIR_ENV
        os.environ[STATE_DIR_ENV] = str(self.tmp / "state")
        self.addCleanup(os.environ.pop, STATE_DIR_ENV, None)
        from tuf_control.agent.tools import TOOLS, AgentService
        self.assertEqual(TOOLS["get_updates"].tier, "read")
        svc = AgentService()
        res, err = svc.call("get_updates", {})
        self.assertFalse(err)
        self.assertFalse(res["checked"])
        check.run_check()
        res, err = svc.call("get_updates", {"source": "firmware"})
        self.assertEqual([i["name"] for i in res["items"]], ["System Firmware"])
        self.assertTrue(res["restart_needed"])
        self.assertIn("items[].name", res["untrusted_fields"])


if __name__ == "__main__":
    unittest.main()


class DaemonIntegrationTests(unittest.TestCase):
    def test_daemon_configures_and_reports_the_scheduler(self):
        from tuf_control.automation.config import RUNTIME_DIR_ENV, update_config
        from tuf_control.backends.fake import FakeBackend, build_fake_tree
        from tuf_control.controller import Controller
        from tuf_control.daemon import Daemon
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = build_fake_tree(tmp / "root")
        for k, v in ((FAKE_ROOT_ENV, root), (CONFIG_DIR_ENV, tmp / "cfg"),
                     (RUNTIME_DIR_ENV, tmp / "run")):
            os.environ[k] = str(v)
            self.addCleanup(os.environ.pop, k, None)
        update_config(lambda c: c["updates"].update(interval_h=12, notify=False))
        ran = []
        sched = UpdateScheduler(runner=lambda: ran.append(1) or check.run_check(),
                                notifier=lambda *a: None, metered=lambda: False,
                                first_delay_s=0)
        d = Daemon(Controller([FakeBackend(root)]), updates=sched, dry_run=True)
        d.reload(force=True)
        self.assertEqual(sched.settings["interval_h"], 12)
        self.assertTrue(sched.tick())
        sched.worker.join(10)
        st = d.status()["updates"]
        self.assertEqual(ran, [1])
        self.assertFalse(st["checking"])
        self.assertEqual(sched.status(refresh=True)["total"], 9)
        self.assertFalse(sched.tick())  # just checked: not due for 12 hours
        d.engine.shutdown()
