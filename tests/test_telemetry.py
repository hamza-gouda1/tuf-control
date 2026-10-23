"""Telemetry layer without hardware: parsers, rate maths, ring buffers, power gating."""
import math
import os
import tempfile
import unittest
from pathlib import Path

from tuf_control.backends.fake import build_fake_tree
from tuf_control.telemetry import Sampler, Store, fmt
from tuf_control.telemetry.cpu import parse_cpulist, parse_loadavg, parse_stat, usage
from tuf_control.telemetry.gpu_intel import DrmClients, parse_fdinfo
from tuf_control.telemetry.gpu_nvidia import NvidiaGpu, parse_smi
from tuf_control.telemetry.memory import derive, parse_meminfo, parse_psi
from tuf_control.telemetry.network import parse_net_dev, parse_wireless
from tuf_control.telemetry.procs import app_from_cgroup, parse_io, parse_pid_stat
from tuf_control.telemetry.storage import disk_rates, parse_diskstats, parse_mounts
from tuf_control.telemetry.util import nice_ceiling, rate

FIX = Path(__file__).parent / "fixtures" / "telemetry"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


class CpuTests(unittest.TestCase):
    def test_parse_stat(self):
        agg, per, misc = parse_stat(fx("proc_stat_1"))
        self.assertEqual(agg, (1000, 10, 500, 8000, 100, 20, 30, 0))
        self.assertEqual(sorted(per), [0, 1])
        self.assertEqual(misc["ctxt"], 900000)
        self.assertEqual(misc["intr"], 123456)
        self.assertEqual(misc["procs_running"], 2)

    def test_usage_matches_top_definition(self):
        a, pa, _ = parse_stat(fx("proc_stat_1"))
        b, pb, _ = parse_stat(fx("proc_stat_2"))
        busy, user, system, iowait = usage(b, a)
        # deltas: user 300 system 100 idle 500 -> busy 400 of 900 (guest not double counted)
        self.assertAlmostEqual(busy, 400 / 900 * 100)
        self.assertAlmostEqual(user, 300 / 900 * 100)
        self.assertAlmostEqual(system, 100 / 900 * 100)
        self.assertEqual(iowait, 0.0)
        self.assertAlmostEqual(user + system + iowait, busy)

    def test_counter_going_backwards_is_clamped(self):
        _, pa, _ = parse_stat(fx("proc_stat_1"))
        _, pb, _ = parse_stat(fx("proc_stat_2"))
        # cpu0: user went 500 -> 450 and nothing else moved: clamped, so no sample at all
        self.assertIsNone(usage(pb[0], pa[0]))
        self.assertIsNone(usage(pa[1], pa[1]))  # no time passed

    def test_cpulist_and_loadavg(self):
        self.assertEqual(parse_cpulist("0-3,8,10-11"), [0, 1, 2, 3, 8, 10, 11])
        self.assertEqual(parse_cpulist(""), [])
        self.assertEqual(parse_loadavg("0.18 0.50 0.85 1/2564 1095978"),
                         (0.18, 0.5, 0.85, 1, 2564))


class MemoryTests(unittest.TestCase):
    def test_meminfo_units_and_partition(self):
        mi = parse_meminfo(fx("meminfo"))
        self.assertEqual(mi["MemTotal"], 15958804 * 1024)
        self.assertEqual(mi["HugePages_Total"], 0)  # unitless stays as is
        d = derive(mi)
        self.assertEqual(d["in_use"] + d["reclaimable"] + d["free"], d["total"])
        self.assertEqual(d["in_use"], (15958804 - 7360708) * 1024)  # procps 4 "used"
        self.assertEqual(d["buff_cache"], (380420 + 5793804 + 487980) * 1024)
        self.assertEqual(d["swap_used"], (4194300 - 1672332) * 1024)

    def test_psi(self):
        p = parse_psi("some avg10=1.50 avg60=0.20 avg300=0.00 total=123\n"
                      "full avg10=0.10 avg60=0.00 avg300=0.00 total=5")
        self.assertEqual(p["some"]["avg10"], 1.5)
        self.assertEqual(p["full"]["total"], 5)
        self.assertIsNone(parse_psi(None))


class DiskNetTests(unittest.TestCase):
    def test_diskstats_rates(self):
        a = parse_diskstats(fx("diskstats_1"))
        b = parse_diskstats(fx("diskstats_2"))
        self.assertIn("loop0", a)
        r = disk_rates(b["nvme0n1"], a["nvme0n1"], 2.0)
        self.assertAlmostEqual(r["read"], 4096 * 512 / 2.0)  # sectors are always 512 B
        self.assertAlmostEqual(r["write"], 2048 * 512 / 2.0)
        self.assertAlmostEqual(r["busy"], 250 / 2000 * 100)
        self.assertIsNone(disk_rates(a["nvme0n1"], b["nvme0n1"], 2.0))  # counter reset

    def test_mounts(self):
        text = ("/dev/nvme0n1p5 / ext4 rw 0 0\n/dev/loop3 /snap/x squashfs ro 0 0\n"
                "tmpfs /run tmpfs rw 0 0\n/dev/nvme0n1p5 /var/snap ext4 rw 0 0\n"
                "/dev/sda1 /media/My\\040Disk vfat rw 0 0\n")
        self.assertEqual(parse_mounts(text), [("/dev/nvme0n1p5", "/", "ext4"),
                                              ("/dev/sda1", "/media/My Disk", "vfat")])

    def test_net(self):
        n = parse_net_dev(fx("net_dev"))
        self.assertEqual(n["wlp108s0"], (2383341107, 2950516, 495606546, 1435522))
        self.assertIn("CloudflareWARP", n)
        self.assertEqual(parse_wireless(fx("net_wireless").decode())["wlp108s0"], (51.0, -59.0))

    def test_rate_helpers(self):
        self.assertEqual(rate(150, 100, 2.0), 25.0)
        self.assertIsNone(rate(50, 100, 1.0))
        self.assertIsNone(rate(150, 100, 0))
        self.assertEqual(nice_ceiling(3.2), 5)
        self.assertEqual(nice_ceiling(0.7), 1)


class GpuTests(unittest.TestCase):
    def test_fdinfo_i915_and_xe(self):
        i = parse_fdinfo(fx("fdinfo_i915"))
        self.assertEqual(i["client"], 38)
        self.assertEqual(i["pdev"], "0000:00:02.0")
        self.assertEqual(i["engines"]["render"], 1418503036860)
        self.assertEqual(i["mem"]["resident-system0"], 184836 * 1024)
        x = parse_fdinfo(fx("fdinfo_xe"))
        self.assertEqual(x["cycles"]["rcs"], 5000)
        self.assertEqual(x["capacity"]["ccs"], 2)
        self.assertIsNone(parse_fdinfo(b"pos:\t0\nflags:\t0\n"))

    def test_drm_client_busy_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for pid in (100, 200):
                (root / f"proc/{pid}/fd").mkdir(parents=True)
                (root / f"proc/{pid}/fdinfo").mkdir()
            # pid 100 has the same client open twice; pid 200 is another client
            for pid, fd, client in ((100, 3, 1), (100, 4, 1), (200, 5, 2)):
                os.symlink("/dev/dri/renderD128", root / f"proc/{pid}/fd/{fd}")

            def write(ns1, ns2):
                for pid, fd, client, ns in ((100, 3, 1, ns1), (100, 4, 1, ns1),
                                            (200, 5, 2, ns2)):
                    (root / f"proc/{pid}/fdinfo/{fd}").write_text(
                        f"drm-driver:\ti915\ndrm-client-id:\t{client}\n"
                        f"drm-pdev:\t0000:00:02.0\ndrm-engine-render:\t{ns} ns\n")
            write(0, 0)
            c = DrmClients(str(root), "0000:00:02.0")
            c.sample(10.0)
            self.assertEqual(len(c.paths), 2)  # one path per client, not per fd
            write(250_000_000, 100_000_000)  # 0.25 s and 0.1 s busy over 1 s
            eng, per_pid, _ = c.sample(11.0)
            self.assertAlmostEqual(eng["render"], 35.0)
            self.assertAlmostEqual(per_pid[100], 25.0)
            self.assertAlmostEqual(per_pid[200], 10.0)

    def test_parse_smi(self):
        q = parse_smi("NVIDIA GeForce RTX 5050 Laptop GPU, 580.178.04, 3, 1, 23, 8151, 180, "
                      "405, 46, 3.31, 100.00, [N/A], P8")
        self.assertEqual(q["vram_used"], 23 << 20)
        self.assertEqual(q["pstate"], 8)
        self.assertIsNone(q["fan"])
        self.assertEqual(parse_smi("too, short"), {})

    def test_sleeping_dgpu_is_never_queried(self):
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root", dgpu_runtime="suspended")
            (root / "sys/bus/pci/devices/0000:01:00.0/power_state").write_text("D3cold\n")
            g = NvidiaGpu(str(root))

            class Boom:
                def __getattr__(self, name):
                    raise AssertionError(f"NVML {name} called on a sleeping GPU")
            g.nvml, g.backend = Boom(), "nvml"
            g.release = lambda: None
            vals, info = {}, {}
            g.sample(1.0, vals, info)
            self.assertEqual(info["dgpu"]["state"], "sleeping")
            self.assertEqual(info["dgpu"]["power_state"], "D3cold")
            self.assertFalse(any(k.startswith("dgpu.") for k in vals))

    def test_idle_awake_dgpu_is_released(self):
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root")
            g = NvidiaGpu(str(root), fake=True)
            (root / "fake/nvml.json").write_text(
                '{"query": {"util": 0, "mem_util": 0, "temp": 40}, "procs": {}}')
            released = []
            g.release = lambda: released.append(1)
            for i in range(3):
                info = {}
                g.sample(float(i), {}, info)
            self.assertTrue(info["dgpu"].get("released"))
            info = {}
            g.sample(4.0, {}, info)
            self.assertEqual(info["dgpu"]["state"], "idle-released")  # backing off


class ProcessTests(unittest.TestCase):
    def test_pid_stat_with_spaces_and_parens(self):
        st = parse_pid_stat(fx("pid_stat"))
        self.assertEqual(st[0], "Isolated Web Co")
        self.assertEqual((st[3], st[4], st[5], st[6], st[7]), (750, 250, 6, 99999, 51200))
        weird = b"7 (a) b (c)) R 1 1 1 0 -1 0 0 0 0 0 1 2 0 0 20 0 1 0 5 0 9 0\n"
        self.assertEqual(parse_pid_stat(weird)[0], "a) b (c)")

    def test_io(self):
        self.assertEqual(parse_io(fx("pid_io")), (4096, 8192))

    def test_app_from_cgroup(self):
        base = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
        cases = {
            "app-gnome-org.gnome.Nautilus-3010.scope": "org.gnome.Nautilus",
            "app-gnome-google\\x2dchrome-1234.scope": "google-chrome",
            "app-flatpak-io.missioncenter.MissionCenter-42.scope":
                "io.missioncenter.MissionCenter",
            "app-org.gnome.Terminal.slice/vte-spawn-1.scope": "org.gnome.Terminal",
            "app-gnome-blueman@autostart.service": "blueman",
        }
        for unit, want in cases.items():
            self.assertEqual(app_from_cgroup(base + unit)[0], want, unit)
        self.assertEqual(app_from_cgroup("0::/system.slice/cron.service"),
                         (None, "cron.service"))
        self.assertEqual(app_from_cgroup("0::/"), (None, None))


class StoreTests(unittest.TestCase):
    def test_ring_wraps_and_keeps_gaps(self):
        s = Store(capacity=4)
        for t in range(1, 7):
            s.push(float(t), {"a": t * 10, "b": None if t == 5 else 1})
        self.assertEqual(s.count, 4)
        self.assertEqual([v for _, v in s.values("a")], [30, 40, 50, 60])
        self.assertTrue(math.isnan(s.values("b")[2][1]))
        self.assertIsNone(s.last("missing"))
        self.assertEqual(s.stats("a", since=4.5), (50, 60, 55))
        self.assertEqual(list(s.index_range(4.5)), [2, 3])

    def test_late_series_and_clock(self):
        s = Store(capacity=8)
        s.push(1.0, {"a": 1})
        s.push(2.0, {"a": 2, "b": 5})
        self.assertTrue(math.isnan(s.values("b")[0][1]))  # history before it existed
        s.push(2.0, {"a": 99})  # duplicate tick dropped
        self.assertEqual(s.last("a"), 2)
        s.push(3.0, {"b": 6})  # a missing this tick -> NaN, not a stale repeat
        self.assertIsNone(s.last("a"))

    def test_memory_is_preallocated(self):
        s = Store(capacity=300)
        s.push(1.0, {f"k{i}": i for i in range(100)})
        self.assertEqual(s.nbytes(), 300 * 8 + 100 * 300 * 4)


class SamplerOnFakeTreeTests(unittest.TestCase):
    def test_rates_from_fake_world(self):
        from tuf_control.telemetry.fake import FakeWorld
        from tuf_control.telemetry.procs import ProcessWalker
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root")
            w = FakeWorld(root)
            w.advance(100.0)
            s = Sampler(str(root), fake=True)
            for t in (101.0, 102.0, 103.0):
                w.advance(t)
                s.sample(t)
            st = s.store
            self.assertEqual(s.info["errors"], {})
            self.assertTrue(0 <= st.last("cpu.total") <= 100)
            self.assertEqual(s.cpu.static["count"], 24)
            self.assertTrue(s.cpu.static["hybrid"])
            self.assertEqual(s.cpu.static["p_cores"], 8)
            self.assertGreater(st.last("disk.nvme0n1.read"), 0)
            self.assertGreater(st.last("net.wlp108s0.rx"), 0)
            self.assertNotIn("net.lo.rx", st.series)
            self.assertIn("docker0", s.info["net"]["ifaces"])
            self.assertEqual(st.last("net.rx"), st.last("net.wlp108s0.rx"))  # no virtual
            self.assertGreater(st.last("igpu.util"), 0)
            self.assertEqual(s.info["dgpu"]["state"], "awake")
            self.assertEqual(set(dict(s.info["sensors"]["fans"])), {"cpu", "gpu"})
            self.assertIn("temp.core.0", st.series)
            walker = ProcessWalker(str(root))
            walker.walk(103.0)
            w.advance(104.0)
            procs = {p.pid: p for p in walker.walk(104.0)}
            self.assertEqual(procs[3488].app, "steam")
            self.assertTrue(procs[85].kthread)
            self.assertGreater(procs[3488].cpu, 0)


class FormatTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(fmt.bytes_(1536), "1.5 KiB")
        self.assertEqual(fmt.bytes_(None), "–")
        self.assertEqual(fmt.rate(2_500_000), "2.5 MB/s")
        self.assertEqual(fmt.rate(512), "512 B/s")
        self.assertEqual(fmt.mhz(4200), "4.20 GHz")
        self.assertEqual(fmt.mhz(700), "700 MHz")
        self.assertEqual(fmt.duration(56940), "15 h 49 min")
        self.assertEqual(fmt.duration(90061), "1 d 1 h 1 min")
        self.assertEqual(fmt.num(float("nan")), "–")
        self.assertEqual(fmt.axis_rate(2e6), "2 MB/s")


if __name__ == "__main__":
    unittest.main()
