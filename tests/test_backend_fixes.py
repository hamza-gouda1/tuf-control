# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Regression tests for backend, daemon, effects, services, agent, and updates fixes."""
from __future__ import annotations

import ast
import concurrent.futures
import contextlib
import io
import os
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class LayeringAndVersionTests(unittest.TestCase):
    def test_version_and_zero_ui_imports_in_backend(self):
        import asusius
        from asusius.core import AURA_MODES as CORE_MODES
        from asusius.core.models import AURA_MODES as MODEL_MODES

        self.assertEqual(asusius.__version__, "0.2.1")
        self.assertEqual(CORE_MODES, MODEL_MODES)
        self.assertIn(10, MODEL_MODES)
        self.assertNotIn(9, MODEL_MODES)

        pkg_root = Path(asusius.__file__).resolve().parent
        for rel in ("automation/rules.py", "agent/tools.py"):
            tree = ast.parse((pkg_root / rel).read_text(encoding="utf-8"), rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn("ui", node.module.split("."), rel)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("ui", alias.name.split("."), rel)


class BackendAndHelperFixTests(unittest.TestCase):
    def test_pool_is_singleton_across_threads(self):
        from asusius.backends import pool as pool_mod

        with mock.patch.object(pool_mod, "_pool", None):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futs = [ex.submit(pool_mod.run_async, lambda: 42) for _ in range(8)]
                results = [f.result().result() for f in futs]
            self.assertEqual(results, [42] * 8)
            self.assertIsNotNone(pool_mod._pool)
            pool_mod._pool.shutdown(wait=True)
            pool_mod._pool = None

    def test_helper_paths_and_write_batch(self):
        from asusius import helper
        from asusius.backends import util

        self.assertEqual(
            util.HELPER_PATHS,
            ("/usr/libexec/asusius-helper", "/usr/lib/asusius/asusius-helper"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p1 = root / "sys/class/leds/dell::kbd_backlight/brightness"
            p2 = root / "sys/class/hwmon/hwmon0/pwm1_enable"
            p3 = root / "sys/class/power_supply/BAT1/charge_control_end_threshold"
            for p, val in ((p1, "0"), (p2, "2"), (p3, "80")):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(val, encoding="utf-8")
            (root / "sys/class/leds/dell::kbd_backlight/max_brightness").write_text(
                "255", encoding="utf-8"
            )
            (root / "sys/class/hwmon/hwmon0/name").write_text(
                "asus_custom_fan_curve\n", encoding="utf-8"
            )
            pairs = [
                ("/sys/class/leds/dell::kbd_backlight/brightness", "128"),
                ("/sys/class/hwmon/hwmon0/pwm1_enable", "4"),
                ("/sys/class/power_supply/BAT1/charge_control_end_threshold", "85"),
            ]
            helper.write_batch(pairs, root=root)
            self.assertEqual(p1.read_text(encoding="utf-8"), "128")
            self.assertEqual(p2.read_text(encoding="utf-8"), "4")
            self.assertEqual(p3.read_text(encoding="utf-8"), "85")

    def test_pkexec_write_many_falls_back_on_exit_2(self):
        from asusius.backends.util import PkexecWriter

        calls = []

        def fake_run(cmd, **_kw):
            calls.append(cmd)
            if "write-batch" in cmd:
                return SimpleNamespace(returncode=2, stderr="usage", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        pw = PkexecWriter("/usr/libexec/asusius-helper")
        with mock.patch.object(pw, "available", return_value=True), \
                mock.patch.object(pw, "no_new_privileges", return_value=False), \
                mock.patch("shutil.which", return_value="/usr/bin/pkexec"), \
                mock.patch("subprocess.run", side_effect=fake_run):
            pw.write_many([(Path("/sys/a"), "1"), (Path("/sys/b"), "2")])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][2], "write-batch")

    def test_sysfs_amd_sensors_mid_fan_kbd_battery_and_dgpu(self):
        from asusius.backends.sysfs import SysfsBackend
        from asusius.core import Capability, FanCurve, FanPoint, Profile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bat = root / "sys/class/power_supply/BAT1"
            bat.mkdir(parents=True)
            (bat / "type").write_text("Battery\n", encoding="utf-8")
            (bat / "charge_full").write_text("4500000\n", encoding="utf-8")
            (bat / "charge_full_design").write_text("5000000\n", encoding="utf-8")
            (bat / "voltage_min_design").write_text("15400000\n", encoding="utf-8")
            (bat / "current_now").write_text("1000000\n", encoding="utf-8")
            (bat / "voltage_now").write_text("15000000\n", encoding="utf-8")
            (bat / "status").write_text("Discharging\n", encoding="utf-8")
            (bat / "capacity").write_text("72\n", encoding="utf-8")

            led = root / "sys/class/leds/asus::kbd_backlight_1"
            led.mkdir(parents=True)
            (led / "brightness").write_text("128\n", encoding="utf-8")
            (led / "max_brightness").write_text("255\n", encoding="utf-8")

            hw0 = root / "sys/class/hwmon/hwmon0"
            hw0.mkdir(parents=True)
            (hw0 / "name").write_text("k10temp\n", encoding="utf-8")
            (hw0 / "temp1_label").write_text("Tctl\n", encoding="utf-8")
            (hw0 / "temp1_input").write_text("68000\n", encoding="utf-8")
            (hw0 / "temp2_label").write_text("Tdie\n", encoding="utf-8")
            (hw0 / "temp2_input").write_text("62000\n", encoding="utf-8")

            hw1 = root / "sys/class/hwmon/hwmon1"
            hw1.mkdir(parents=True)
            (hw1 / "name").write_text("asus-ec-sensors\n", encoding="utf-8")
            (hw1 / "fan1_label").write_text("CPU_Opt\n", encoding="utf-8")
            (hw1 / "fan1_input").write_text("2100\n", encoding="utf-8")

            hw2 = root / "sys/class/hwmon/hwmon2"
            hw2.mkdir(parents=True)
            (hw2 / "name").write_text("asus_custom_fan_curve\n", encoding="utf-8")
            (hw2 / "pwm3_enable").write_text("1\n", encoding="utf-8")
            for idx in range(1, 9):
                (hw2 / f"pwm3_auto_point{idx}_temp").write_text(str(35 + idx * 5), encoding="utf-8")
                (hw2 / f"pwm3_auto_point{idx}_pwm").write_text(str(20 + idx * 20), encoding="utf-8")
            (root / "sys/firmware/acpi").mkdir(parents=True)
            (root / "sys/firmware/acpi/platform_profile").write_text("balanced\n", encoding="utf-8")
            (root / "sys/firmware/acpi/platform_profile_choices").write_text(
                "quiet balanced performance\n", encoding="utf-8"
            )

            gpu = root / "sys/bus/pci/devices/0000:03:00.0"
            (gpu / "power").mkdir(parents=True)
            (gpu / "class").write_text("0x030000\n", encoding="utf-8")
            (gpu / "vendor").write_text("0x1002\n", encoding="utf-8")
            (gpu / "power/runtime_status").write_text("unsupported\n", encoding="utf-8")
            (gpu / "gpu_busy_percent").write_text("37\n", encoding="utf-8")
            ghw = gpu / "hwmon/hwmon3"
            ghw.mkdir(parents=True)
            (ghw / "temp1_input").write_text("54000\n", encoding="utf-8")
            (ghw / "power1_average").write_text("25000000\n", encoding="utf-8")

            b = SysfsBackend(root=root, use_nvidia_smi=False)
            caps = b.probe()
            self.assertTrue(caps[Capability.KBD_BRIGHTNESS].available)
            self.assertEqual(b.get_kbd_brightness(), (128, 255))
            b.set_kbd_brightness(200)
            self.assertEqual(b.get_kbd_brightness(), (200, 255))

            bh = b.get_battery_health()
            self.assertAlmostEqual(bh.health_percent, 90.0)
            curves = b.get_fan_curves(Profile.BALANCED)
            self.assertEqual(list(curves), ["MID"])
            self.assertEqual(curves["MID"].fan, "MID")
            b.set_fan_curve(
                Profile.BALANCED,
                FanCurve("MID", [FanPoint(30 + i * 7, 10 + i * 25) for i in range(8)], True),
            )
            b.reset_fan_curves(Profile.BALANCED)
            self.assertEqual((hw2 / "pwm3_enable").read_text(encoding="utf-8").strip(), "3")

            tel = b.get_telemetry()
            self.assertEqual(tel.cpu_temp_c, 62.0)
            self.assertEqual(tel.fan_rpm, {"CPU_Opt": 2100})
            self.assertEqual(tel.battery_power_w, 15.0)
            self.assertEqual(tel.gpu_util_pct, 37)
            self.assertEqual(tel.gpu_temp_c, 54.0)
            self.assertEqual(tel.gpu_power_w, 25.0)

    def test_cli_backend_asusctl6_parsing_and_bounds(self):
        from asusius.backends.cli import CliBackend
        from asusius.core import KbdEffect, Profile, Unsupported

        responses = {
            ("profile", "get"): "Active profile: Performance\n",
            ("leds", "get"): "Keyboard brightness is High\n",
        }
        b = CliBackend()
        with mock.patch("shutil.which", return_value="/usr/bin/asusctl"), \
                mock.patch("asusius.backends.cli.run_cmd",
                           side_effect=lambda cmd, **_kw: responses[tuple(cmd[1:])]):
            self.assertEqual(b.get_profile(), Profile.PERFORMANCE)
            self.assertEqual(b.get_kbd_brightness(), (3, 3))
            with self.assertRaises(Unsupported):
                b.set_kbd_brightness(10)
            with self.assertRaises(Unsupported):
                b.set_kbd_effect(KbdEffect(mode=9))

    def test_dbus_set_attribute_supports_strings_and_probe_caches_reset(self):
        from asusius.backends.dbus import ARMOURY_IFACE, ARMOURY_PATH, ASUSD, DBusBackend

        class AttrTransport:
            def __init__(self):
                self.sets = []

            def name_has_owner(self, name):
                return name == ASUSD

            def introspect(self, _d, path):
                if path == ARMOURY_PATH:
                    return '<node><node name="panel_mode"/></node>'
                return "<node/>"

            def get_all(self, _d, _p, _i):
                return {"CurrentValue": "fast"}

            def get_property(self, _d, _p, _i, prop):
                if prop == "PlatformProfile":
                    return 0
                return 80

            def call(self, *_a):
                return None

            def set_property(self, d, p, i, prop, sig, value):
                self.sets.append((d, p, i, prop, sig, value))

        t = AttrTransport()
        b = DBusBackend(t)
        b.set_attribute("panel_mode", "eco")
        self.assertEqual(t.sets[-1], (ASUSD, f"{ARMOURY_PATH}/panel_mode", ARMOURY_IFACE,
                                       "CurrentValue", "s", "eco"))
        b._owners = {"x": True}
        b._aura_path = "/cached"
        b.probe()
        self.assertIsNone(b._aura_path)

    def test_controller_set_attribute_filters_by_capability(self):
        from asusius.controller import Controller
        from asusius.core import Capability, CapStatus

        class DBusLike:
            name = "dbus"
            def __init__(self):
                self.writes = 0
            def probe(self):
                return {Capability.FIRMWARE_ATTRIBUTES: CapStatus(False, "via sysfs")}
            def supports_attribute_write(self, _name):
                return False
            def set_attribute(self, _name, _val):
                self.writes += 1

        class SysfsLike:
            name = "sysfs"
            def __init__(self):
                self.writes = []
            def probe(self):
                return {Capability.FIRMWARE_ATTRIBUTES: CapStatus(True)}
            def supports_attribute_write(self, _name):
                return True
            def set_attribute(self, name, val):
                self.writes.append((name, val))

        d, s = DBusLike(), SysfsLike()
        ctl = Controller([d, s])
        ctl.set_attribute("nv_dynamic_boost", 15)
        self.assertEqual(d.writes, 0)
        self.assertEqual(s.writes, [("nv_dynamic_boost", 15)])


class TelemetryFixTests(unittest.TestCase):
    def test_nvidia_unsupported_pm_smi_fallback_and_nvml_resize(self):
        from asusius.telemetry import gpu_nvidia

        class FakeLib:
            def __init__(self):
                self.calls = []
            def nvmlDeviceGetGraphicsRunningProcesses_v3(self, _h, n_ptr, _arr):
                cap = n_ptr._obj.value
                self.calls.append(cap)
                if cap < 128:
                    return gpu_nvidia.NVML_INSUFFICIENT_SIZE
                n_ptr._obj.value = 1
                _arr[0].pid = 4242
                _arr[0].usedGpuMemory = 64 << 20
                return gpu_nvidia.NVML_OK

        nv = object.__new__(gpu_nvidia.Nvml)
        nv.lib = FakeLib()
        nv._procs = ("nvmlDeviceGetGraphicsRunningProcesses_v3",)
        nv._putil = False
        procs = nv.processes(object())
        self.assertEqual(nv.lib.calls, [64, 128])
        self.assertEqual(procs[4242]["vram"], 64 << 20)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dev = root / "sys/bus/pci/devices/0000:01:00.0"
            (dev / "power").mkdir(parents=True)
            (dev / "class").write_text("0x030200\n", encoding="utf-8")
            (dev / "vendor").write_text("0x10de\n", encoding="utf-8")
            (dev / "power/runtime_status").write_text("unsupported\n", encoding="utf-8")

            with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
                    mock.patch("asusius.telemetry.gpu_nvidia.Nvml",
                               side_effect=OSError("no nvml")):
                gpu = gpu_nvidia.NvidiaGpu(root=str(root))
            with mock.patch.object(
                gpu, "_query_smi", return_value={"util": 41, "temp": 60.0}
            ):
                info: dict = {}
                gpu.sample(100.0, {}, info)
            self.assertEqual(info["dgpu"]["state"], "awake")
            self.assertEqual(info["dgpu"]["q"]["util"], 41)

    def test_amd_gpu_igpu_dgpu_and_fdinfo_guard(self):
        from asusius.telemetry.gpu_amd import AmdDiscreteGpu, AmdGpu, find_amd_cards
        from asusius.telemetry.gpu_intel import parse_fdinfo

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            card_dev = root / "sys/class/drm/card1/device"
            (card_dev / "power").mkdir(parents=True)
            (card_dev / "driver_name").write_text("amdgpu\n", encoding="utf-8")
            (card_dev / "address").write_text("0000:04:00.0\n", encoding="utf-8")
            (card_dev / "vendor").write_text("0x1002\n", encoding="utf-8")
            (card_dev / "device").write_text("0x15bf\n", encoding="utf-8")
            (card_dev / "boot_vga").write_text("1\n", encoding="utf-8")
            (card_dev / "power/runtime_status").write_text("unsupported\n", encoding="utf-8")
            (card_dev / "gpu_busy_percent").write_text("18\n", encoding="utf-8")
            (card_dev / "mem_info_vram_used").write_text(str(512 * 1024 * 1024), encoding="utf-8")
            (card_dev / "mem_info_vram_total").write_text(str(2048 * 1024 * 1024), encoding="utf-8")
            (card_dev / "pp_dpm_sclk").write_text("0: 400Mhz\n1: 1850Mhz *\n", encoding="utf-8")
            hw = card_dev / "hwmon/hwmon0"
            hw.mkdir(parents=True)
            (hw / "temp1_input").write_text("49000\n", encoding="utf-8")
            (hw / "power1_average").write_text("14000000\n", encoding="utf-8")

            cards = find_amd_cards(str(root))
            self.assertEqual(len(cards), 1)
            igpu = AmdGpu(str(root))
            vals: dict = {}
            info: dict = {}
            igpu.sample(1.0, vals, info)
            self.assertEqual(vals["igpu.util"], 18)
            self.assertEqual(vals["igpu.freq"], 1850)
            self.assertEqual(vals["igpu.temp"], 49.0)
            self.assertEqual(vals["igpu.power"], 14.0)
            self.assertEqual(info["igpu"]["vram_used"], 512 * 1024 * 1024)

            dgpu = AmdDiscreteGpu(str(root))
            dinfo: dict = {}
            dgpu.sample(1.0, {}, dinfo)
            self.assertEqual(dinfo["dgpu"]["state"], "awake")
            (card_dev / "power/runtime_status").write_text("suspended\n", encoding="utf-8")
            dinfo2: dict = {}
            dgpu.sample(2.0, {}, dinfo2)
            self.assertEqual(dinfo2["dgpu"]["state"], "sleeping")

        parsed = parse_fdinfo(
            b"drm-driver:\tamdgpu\ndrm-engine-gfx:\tNaN ns\n"
            b"drm-resident-vram:\t1048576 KiB\n"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["engines"], {})
        self.assertEqual(parsed["mem"]["resident-vram"], 1024 << 20)

    def test_storage_rescan_and_network_iw_link_fallback(self):
        from asusius.telemetry.network import Network
        from asusius.telemetry.storage import Storage

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blk = root / "sys/block"
            blk.mkdir(parents=True)
            (root / "proc").mkdir(parents=True)
            (root / "proc/diskstats").write_text("", encoding="utf-8")
            st = Storage(root=str(root))
            self.assertEqual(st.disks, {})
            nvme = blk / "nvme0n1"
            (nvme / "device").mkdir(parents=True)
            (nvme / "device/model").write_text("Samsung SSD\n", encoding="utf-8")
            (nvme / "queue").mkdir()
            (nvme / "queue/logical_block_size").write_text("512\n", encoding="utf-8")
            (nvme / "size").write_text("1000\n", encoding="utf-8")
            st._disks_t = 0.0
            st.sample(100.0, {}, {})
            self.assertEqual(list(st.disks), ["nvme0n1"])

        net = Network()
        net._meta["stale0"] = {"ts": 0.0, "link_mbps": 100}
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            (r / "proc/net").mkdir(parents=True)
            (r / "proc/net/dev").write_text(
                "Inter-|\n face |\neth0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
                encoding="utf-8",
            )
            (r / "proc/net/wireless").write_text("", encoding="utf-8")
            (r / "sys/class/net/eth0").mkdir(parents=True)
            net.root = str(r)
            net.sample(10.0, {}, {})
            self.assertNotIn("stale0", net._meta)
        iw_out = "Connected to aa:bb (on wlan0)\n\tsignal: -58 dBm\n\ttx bitrate: 866 MBit/s\n"
        net_iw = Network(root="/")
        with mock.patch("shutil.which", return_value="/usr/sbin/iw"), \
                mock.patch("subprocess.run",
                           return_value=SimpleNamespace(returncode=0, stdout=iw_out)):
            got = net_iw._iw_link("wlan0", 10.0)
        self.assertEqual(got[1], -58.0)


class DaemonAutomationServicesEffectsAgentTests(unittest.TestCase):
    def test_daemon_sighup_reloads_in_loop_and_status_has_mono(self):
        from asusius import daemon as daemon_mod
        from asusius.automation.config import CONFIG_DIR_ENV, RUNTIME_DIR_ENV, update_config
        from asusius.backends.fake import FakeBackend, build_fake_tree
        from asusius.controller import FAKE_ROOT_ENV, Controller

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            root = build_fake_tree(t / "root")
            with mock.patch.dict(os.environ, {
                FAKE_ROOT_ENV: str(root),
                CONFIG_DIR_ENV: str(t / "cfg"),
                RUNTIME_DIR_ENV: str(t / "run"),
            }):
                update_config(lambda c: c["updates"].update(interval_h=8))
                d = daemon_mod.Daemon(Controller([FakeBackend(root)]), dry_run=True)
                handlers: dict = {}

                def capture_sig(sig, fn):
                    handlers[sig] = fn

                with mock.patch.object(daemon_mod, "Daemon", return_value=d), \
                        mock.patch("asusius.daemon.acquire_lock",
                                   return_value=SimpleNamespace(close=lambda: None)), \
                        mock.patch("signal.signal", side_effect=capture_sig), \
                        mock.patch.object(
                            d, "run", side_effect=lambda stop: handlers[signal.SIGHUP](1, None)
                        ):
                    daemon_mod.main([])
                self.assertTrue(d._reload_req.is_set())
                self.assertTrue(d.wake.is_set())
                self.assertIn("updated_mono", d.status())
                d.engine.shutdown()

    def test_process_scanner_detects_pid_reuse(self):
        from asusius.automation.sources import ProcessScanner

        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            p = proc / "555"
            p.mkdir(parents=True)
            (p / "comm").write_text("steam\n", encoding="utf-8")
            (p / "stat").write_text(
                "555 (steam) " + " ".join(["0"] * 19) + " 1000 0\n",
                encoding="utf-8",
            )
            sc = ProcessScanner(proc)
            self.assertIn("steam", sc.scan())
            (p / "comm").write_text("blender\n", encoding="utf-8")
            (p / "stat").write_text(
                "555 (blender) " + " ".join(["0"] * 19) + " 2000 0\n",
                encoding="utf-8",
            )
            seen = sc.scan()
            self.assertIn("blender", seen)
            self.assertNotIn("steam", seen)

    def test_rules_mode_9_rejected_and_high_brightness_allowed(self):
        from asusius.automation.rules import Action, parse_action_spec
        from asusius.core import ValidationError

        with self.assertRaises(ValidationError):
            Action.from_dict({"type": "kbd_effect", "mode": 9, "colour": "#ff0000"})
        act = Action.from_dict({"type": "kbd_brightness", "value": 128})
        self.assertEqual(act.describe(), "Backlight 128")
        self.assertEqual(
            Action.from_dict(parse_action_spec("kbd_brightness=255")).params["value"], 255
        )

    def test_engine_does_not_record_hold_when_apply_fails(self):
        from datetime import datetime
        from asusius.automation.engine import Engine
        from asusius.automation.rules import Action, Rule, Trigger
        from asusius.automation.sources import State

        rule = Rule("r1", "Test", Trigger("app_running", {"app": "game"}),
                    (Action("profile", {"profile": "performance"}),), restore=True)
        ctl = mock.Mock()
        ctl.get_profile.return_value = ("balanced", "sysfs")
        ctl.set_profile.side_effect = RuntimeError("write failed")
        eng = Engine(ctl, dry_run=False)
        eng.set_rules([rule])
        eng.step(State(datetime.now(), on_ac=True, battery=80, apps=frozenset({"game"})))
        self.assertEqual(eng.holds, {})

    def test_services_unit_exited_template_and_unloaded_files(self):
        from asusius.services.backend import SystemdBackend
        from asusius.services.units import Unit, template_of

        u = Unit(name="foo.service", active="active", sub="exited")
        self.assertTrue(u.running)
        self.assertEqual(template_of("openvpn-client@my.vpn.service"),
                         "openvpn-client@.service")

        b = SystemdBackend("user")
        b._conn = SimpleNamespace(is_closed=lambda: True)
        b._call = lambda method, *_a, **_kw: (
            [[("existing.service", "Desc", "loaded", "active", "exited", "", 0, "/p")]],
            [[("/etc/systemd/user/existing.service", "enabled"),
              ("/etc/systemd/user/dormant.service", "disabled"),
              ("/etc/systemd/user/tmpl@.service", "static")]],
        )[0 if method == "ListUnits" else 1]
        units = b.list_units(force_files=True)
        names = [x.name for x in units]
        self.assertIn("dormant.service", names)
        self.assertNotIn("tmpl@.service", names)

    def test_audio_odd_byte_and_wav_subchunks(self):
        from asusius.effects.audio import AudioAnalyzer, _strip_wav_header

        az = AudioAnalyzer()
        az.feed_s16le(b"\x00")
        self.assertEqual(az._tail, b"\x00")
        az.feed_s16le(b"\x10\x00\x20")
        self.assertEqual(az._tail, b"")

        wav = (b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + bytes(16)
               + b"LIST\x04\x00\x00\x00abcd" + b"data\x04\x00\x00\x00WXYZ")
        self.assertEqual(_strip_wav_header(wav), (True, b"WXYZ"))

    def test_runner_notification_without_base_effect_and_none_colour(self):
        from asusius.effects.notify import NotificationQueue
        from asusius.effects.runner import EffectsRunner, KbdWriter

        with tempfile.TemporaryDirectory() as tmp:
            ctl = mock.Mock()
            ctl.get_kbd_rgb.return_value = (
                {"mode_data": {"mode": 0, "colour1": (10, 20, 30)}, "builtins": {}},
                "dbus",
            )
            ctl.get_kbd_brightness.return_value = ((2, 3), "dbus")
            writer = KbdWriter(ctl, recovery_path=Path(tmp) / "rec.json")
            nq = NotificationQueue()
            nq.push({"app": "mail", "summary": "New", "body": ""})
            runner = EffectsRunner(ctl, writer=writer, sources={"notifications": nq},
                                   recovery_path=Path(tmp) / "rec.json")
            runner.configure({"notifications": {
                "enabled": True, "colour": "#00ff00", "flashes": 2,
                "duration_ms": 2000, "apps": ["mail"],
            }})
            runner.step(0.05)
            self.assertIsNotNone(writer.saved)
            ef = KbdWriter._effect({"mode": None, "colour1": None, "colour2": None,
                                    "zone": None, "speed": None, "direction": None})
            self.assertEqual(ef.colour1, (142, 42, 68))

    def test_agent_tools_mcp_help_and_notify_close(self):
        from asusius.agent.cli_cmds import mcp_main
        from asusius.agent.notify import DesktopNotifier
        from asusius.agent.store import STATE_DIR_ENV, audit_path
        from asusius.agent.tools import AgentService, ToolError
        from asusius.core import BackendError

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mcp_main(["--help"]), 0)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ[STATE_DIR_ENV] = tmp
            try:
                svc = AgentService(ctl=mock.Mock())
                with mock.patch("asusius.agent.tools.t_get_status",
                                side_effect=BackendError("boom")):
                    with self.assertRaises(ToolError) as err:
                        svc.read_resource("asusius://status")
                self.assertEqual(err.exception.code, "backend_error")
                self.assertIn("asusius://status", audit_path().read_text(encoding="utf-8"))
            finally:
                os.environ.pop(STATE_DIR_ENV, None)

        q = mock.Mock()
        n = DesktopNotifier(q)
        n._owner = ":1.50"
        n.ids = {42: "req-1"}
        n._on_closed(None, ":1.50", None, None, None, SimpleNamespace(unpack=lambda: (42, 1)))
        self.assertEqual(n.ids, {})
        n.ids = {43: "req-2"}
        msg = SimpleNamespace(unpack=lambda: (43, "approve"))
        n._on_action(None, ":1.999", None, None, None, msg)
        self.assertIn(43, n.ids)
        q.decide.assert_not_called()


class UpdatesFixTests(unittest.TestCase):
    def test_apt_list_double_arch_and_str_error(self):
        from asusius.updates import apt

        items = apt.parse_apt_list(
            "zlib1g:i386/noble-updates 1:1.3.dfsg-3 i386 [upgradable from: 1:1.3.dfsg-2]\n"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "zlib1g")
        self.assertEqual(items[0].id, "apt:zlib1g:i386")
        with mock.patch("subprocess.run", return_value=SimpleNamespace(
            returncode=100, stderr="E: broken lock", stdout=""
        )):
            with self.assertRaises(RuntimeError) as ctx:
                apt._run(["apt-get", "upgrade"])
            self.assertEqual(ctx.exception.args[0], "E: broken lock")

    def test_actions_arch_split_errors_flatpak_and_software_fallback(self):
        from asusius.updates import actions, apt

        class Tx:
            def __init__(self, *a, **kw):
                pass
            def run(self, method, _args):
                if method == "Resolve":
                    return [
                        (2, "libfoo;1.2;amd64;ubuntu", "foo64"),
                        (2, "libfoo;1.2;i386;ubuntu", "foo32"),
                    ]
                return [(12, "libfoo;1.2;i386;ubuntu", "install")]

        with mock.patch.object(actions, "PkTransaction", Tx), \
                mock.patch.object(apt, "dpkg_holds", return_value=set()):
            msg = actions.pk_install(["libfoo:i386"])
        self.assertIn("installed 1", msg)

        with mock.patch("subprocess.run", return_value=SimpleNamespace(
            returncode=1, stderr="refresh failed", stdout=""
        )):
            with self.assertRaises(actions.ActionError) as fw_err:
                actions.fwupd_refresh()
            self.assertEqual(fw_err.exception.args[0], "refresh failed")
            with self.assertRaises(actions.ActionError) as fp_err:
                actions.flatpak_refresh()
            self.assertEqual(fp_err.exception.args[0], "refresh failed")

        with mock.patch("shutil.which", side_effect=lambda b:
                        f"/usr/bin/{b}" if b == "plasma-discover" else None), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(actions, "_fake", return_value=False):
            self.assertTrue(actions.software_available())
            self.assertEqual(actions.open_software(), "opened Discover")
            self.assertEqual(popen.call_args.args[0][0], "plasma-discover")

    def test_verify_staged_firmware_uses_latest_history(self):
        from asusius.updates import verify
        from asusius.updates.model import UpdateItem

        it = UpdateItem(id="fw:1", source="firmware", name="BIOS", current="309",
                        available="311", apply_method="fwupd", extra={"device_id": "dev-1"})
        recs = [
            {"DeviceId": "dev-1", "Created": 100, "UpdateState": 2,
             "Releases": [{"Version": "309"}]},
            {"DeviceId": "dev-1", "Created": 200, "UpdateState": 4,
             "Releases": [{"Version": "311"}]},
        ]
        with mock.patch("asusius.updates.firmware.FwupdClient.history", return_value=recs), \
                mock.patch.object(verify, "_fake", return_value=False):
            self.assertEqual(verify.staged_firmware([it]), {"fw:1": "311"})

    def test_system_fedora_arch_opensuse_and_uki_kernels(self):
        from asusius.updates import system

        self.assertEqual(system.parse_kernel("6.11.5-300.fc41.x86_64"),
                         ((6, 11, 5, 300, 41), "x86_64"))
        self.assertEqual(system.parse_kernel("6.11.5-arch1-1"), ((6, 11, 5, 1, 1), "arch"))
        self.assertEqual(system.parse_kernel("6.11.5-zen1-1-zen"), ((6, 11, 5, 1, 1), "zen"))
        self.assertEqual(system.parse_kernel("6.11.5-lp155.2.1-default"),
                         ((6, 11, 5, 155, 2, 1), "default"))
        self.assertIsNone(system.parse_kernel("0-rescue-abc"))

        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            (r / "boot/efi/EFI/Linux").mkdir(parents=True)
            (r / "boot/efi/EFI/Linux/fedora-6.11.6-200.fc41.x86_64.efi").write_text(
                "", encoding="utf-8"
            )
            (r / "boot/vmlinuz-0-rescue-xyz").write_text("", encoding="utf-8")
            (r / "boot/vmlinuz-linux").write_text("", encoding="utf-8")
            mod = r / "usr/lib/modules/6.11.7-arch1-1"
            mod.mkdir(parents=True)
            (mod / "vmlinuz").write_text("", encoding="utf-8")
            inst = system.installed_kernels(r)
            self.assertIn("6.11.6-200.fc41.x86_64", inst)
            self.assertIn("6.11.7-arch1-1", inst)
            self.assertIsNotNone(system.kernel_item("6.11.5-300.fc41.x86_64", inst))
            self.assertIsNotNone(system.kernel_item("6.11.6-arch1-1", inst))
        self.assertIsNone(system._UKI_RE.search("0.0.0-" + "+-" * 500 + "!"))

    def test_distro_pkgs_and_agent_tool_source_filter(self):
        from asusius.updates import agent_tool, check, distro_pkgs
        from asusius.updates.model import Report, SourceResult, UpdateItem

        pac = distro_pkgs.parse_pacman("linux 6.11.5-1 -> 6.11.6-1\nvim 9.1-1 -> 9.1-2\n")
        self.assertEqual([i.name for i in pac], ["linux", "vim"])
        self.assertTrue(pac[0].needs_reboot)
        dnf = distro_pkgs.parse_dnf("openssl.x86_64 3.2.2-1.fc41 updates-security\n",
                                    {("openssl", "x86_64"): "3.2.1-1.fc41"})
        self.assertEqual(dnf[0].current, "3.2.1-1.fc41")
        self.assertTrue(dnf[0].security)
        zyp = distro_pkgs.parse_zypper(
            '<update-status><update-list><update name="bash" edition="5.2" '
            'edition-old="5.1" arch="x86_64" category="security"/></update-list></update-status>'
        )
        self.assertEqual(zyp[0].id, "zypper:bash:x86_64")
        self.assertTrue(zyp[0].security)

        rep = Report(checked_ts=100.0, sources=[
            SourceResult("apt", items=[UpdateItem("apt:a", "apt", "a", "1", "2")]),
            SourceResult("firmware", items=[UpdateItem("fw:1", "firmware", "BIOS", "1", "2",
                                                       security=True)]),
        ])
        with mock.patch("asusius.updates.check.load_report", return_value=rep):
            out = agent_tool.get_updates(None, {"source": "firmware"})
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["security"], 1)
        self.assertEqual(out["counts"], {"firmware": 1})
        self.assertIn("firmware 1", out["summary"])

        with mock.patch("asusius.updates.check._fake_root", return_value=None), \
                mock.patch("asusius.updates.apt.available", return_value=(False, "no dpkg")), \
                mock.patch("asusius.updates.distro_pkgs.detect_manager", return_value="dnf"), \
                mock.patch("asusius.updates.distro_pkgs.check",
                           return_value=SourceResult("dnf")), \
                mock.patch("asusius.updates.snap.check", return_value=SourceResult("snap")), \
                mock.patch("asusius.updates.flatpak.check",
                           return_value=SourceResult("flatpak")), \
                mock.patch("asusius.updates.firmware.check",
                           return_value=SourceResult("firmware")), \
                mock.patch("asusius.updates.system.check", return_value=SourceResult("system")):
            non_apt = check.collect({"sources": {}, "check_repos": False})
        self.assertEqual([s.source for s in non_apt.sources],
                         ["dnf", "snap", "flatpak", "firmware", "system"])

    def test_daemon_service_start_passes_log_fd_directly(self):
        from asusius.automation.service import DaemonClient

        opened = []

        def fake_popen(_cmd, **kw):
            fd = kw["stdout"]
            self.assertIsInstance(fd, int)
            self.assertEqual(kw["stderr"], -2)  # subprocess.STDOUT
            opened.append(fd)
            return SimpleNamespace(pid=99999, poll=lambda: None, stdout=None)

        with tempfile.TemporaryDirectory() as tmp:
            cli_svc = DaemonClient(runtime=Path(tmp), popen=fake_popen, sleep=lambda *_: None)
            with mock.patch.object(cli_svc, "status", return_value={"running": False}):
                res = cli_svc.start(dry_run=True)
            self.assertIn("started pid", res)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])

    def test_updates_notify_launch_uses_systemd_run_and_falls_back(self):
        from asusius.updates import notify

        with mock.patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"), \
                mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            notify._launch_app()
        self.assertEqual(run.call_args.args[0][:3], ["systemd-run", "--user", "--collect"])


if __name__ == "__main__":
    unittest.main()
