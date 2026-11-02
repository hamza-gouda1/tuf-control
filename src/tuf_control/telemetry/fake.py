# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""A fake /proc + /sys for the telemetry layer (development and screenshots only).

`ensure(root)` adds the files the readers need to a tree made by
backends.fake.build_fake_tree; `FakeWorld.advance(now)` moves the counters like a short
gaming burst so graphs have shape. Everything is written inside `root`.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

NCPU = 24
P_THREADS = 16
CLK = 100
PAGE = 4096
GIB = 1 << 30
MEM_TOTAL = 16 * GIB - 420 * (1 << 20)

# (pid, ppid, comm, cmdline, cgroup unit, base cpu % of one core, private MiB, gpu)
PROCS = [
    (1, 0, "systemd", "/sbin/init splash", "init.scope", 0.1, 12, None),
    (2, 0, "kthreadd", "", "", 0.0, 0, None),
    (85, 2, "kworker/3:1-events", "", "", 0.3, 0, None),
    (412, 1, "systemd-journal", "/usr/lib/systemd/systemd-journald",
     "systemd-journald.service", 0.2, 30, None),
    (1180, 1, "systemd", "/usr/lib/systemd/systemd --user",
     "user@1000.service/init.scope", 0.0, 9, None),
    (1240, 1180, "pipewire", "/usr/bin/pipewire",
     "user@1000.service/session.slice/pipewire.service", 1.4, 22, None),
    (1252, 1180, "wireplumber", "/usr/bin/wireplumber",
     "user@1000.service/session.slice/wireplumber.service", 0.2, 18, None),
    (1460, 1180, "gnome-shell", "/usr/bin/gnome-shell",
     "user@1000.service/session.slice/org.gnome.Shell@wayland.service", 6.0, 380, "igpu"),
    (2101, 1180, "firefox", "/usr/lib/firefox/firefox",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 9.0, 640, "igpu"),
    (2150, 2101, "Isolated Web Co", "/usr/lib/firefox/firefox -contentproc -isForBrowser",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 14.0, 420, None),
    (2163, 2101, "Isolated Web Co", "/usr/lib/firefox/firefox -contentproc -isForBrowser",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 4.0, 260, None),
    (2170, 2101, "RDD Process", "/usr/lib/firefox/firefox -contentproc rdd",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 2.0, 40, "igpu"),
    (2201, 2101, "Web Content", "/usr/lib/firefox/firefox -contentproc",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 0.5, 90, None),
    (2610, 1180, "code", "/usr/share/code/code --unity-launch",
     "user@1000.service/app.slice/app-gnome-code-2610.scope", 3.0, 310, "igpu"),
    (2633, 2610, "code", "/usr/share/code/code --type=renderer",
     "user@1000.service/app.slice/app-gnome-code-2610.scope", 5.0, 520, None),
    (2655, 2610, "code", "/usr/share/code/code --type=utility",
     "user@1000.service/app.slice/app-gnome-code-2610.scope", 1.0, 180, None),
    (3010, 1180, "nautilus", "/usr/bin/nautilus --gapplication-service",
     "user@1000.service/app.slice/app-gnome-org.gnome.Nautilus-3010.scope", 0.1, 95, None),
    (3330, 1180, "gnome-terminal-", "/usr/libexec/gnome-terminal-server",
     "user@1000.service/app.slice/app-gnome-org.gnome.Terminal-3330.scope", 0.4, 60, None),
    (3351, 3330, "bash", "bash", "user@1000.service/app.slice/app-gnome-org.gnome.Terminal-"
     "3330.scope", 0.0, 5, None),
    (3420, 1180, "steam", "/home/user/.steam/ubuntu12_32/steam",
     "user@1000.service/app.slice/app-gnome-steam-3420.scope", 2.0, 410, None),
    (3488, 3420, "Cyberpunk2077.e", "Z:\\Cyberpunk2077\\bin\\x64\\Cyberpunk2077.exe",
     "user@1000.service/app.slice/app-gnome-steam-3420.scope", 160.0, 5200, "dgpu"),
    (3502, 3420, "wineserver", "/usr/bin/wineserver",
     "user@1000.service/app.slice/app-gnome-steam-3420.scope", 3.0, 30, None),
    (4100, 1180, "python3", "python3 -m tuf_control.ui",
     "user@1000.service/app.slice/app-gnome-io.github.hamzagouda.TufControl-4100.scope",
     0.6, 70, "igpu"),
]


def _w(path: Path, text) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n")


def load_at(t: float) -> float:
    return max(0.0, min(1.0, 0.5 + 0.45 * math.sin(t / 14.0) + 0.15 * math.sin(t / 3.1)))


def ensure(root: str | Path) -> None:
    """Add telemetry files to a fake tree (idempotent)."""
    root = Path(root)
    if (root / "fake/telemetry-ready").exists():
        return
    cpu = root / "sys/devices/system/cpu"
    _w(cpu / "online", f"0-{NCPU - 1}")
    _w(root / "sys/devices/cpu_core/cpus", f"0-{P_THREADS - 1}")
    _w(root / "sys/devices/cpu_atom/cpus", f"{P_THREADS}-{NCPU - 1}")
    for i in range(NCPU):
        d = cpu / f"cpu{i}"
        p = i < P_THREADS
        core = (i // 2) * 4 if p else 32 + (i - P_THREADS)
        _w(d / "topology/core_id", core)
        _w(d / "topology/physical_package_id", 0)
        _w(d / "topology/thread_siblings_list", f"{i - i % 2}-{i - i % 2 + 1}" if p else i)
        _w(d / "cpufreq/cpuinfo_max_freq", 5_000_000 if p else 3_700_000)
        _w(d / "cpufreq/cpuinfo_min_freq", 800_000)
        _w(d / "cpufreq/base_frequency", 2_200_000 if p else 1_600_000)
        _w(d / "cpufreq/scaling_cur_freq", 800_000)
    c0 = cpu / "cpu0/cpufreq"
    _w(c0 / "scaling_driver", "intel_pstate")
    _w(c0 / "scaling_governor", "powersave")
    _w(c0 / "energy_performance_preference", "balance_performance")
    _w(cpu / "intel_pstate/status", "active")
    _w(cpu / "intel_pstate/no_turbo", 0)
    _w(root / "proc/cpuinfo", "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i7-14650HX\n")
    hw = root / "sys/class/hwmon/hwmon7"
    _w(hw / "temp1_label", "Package id 0")
    cores = sorted({(i // 2) * 4 for i in range(P_THREADS)} | set(range(32, 40)))
    for n, core in enumerate(cores, start=2):
        _w(hw / f"temp{n}_label", f"Core {core}")
        _w(hw / f"temp{n}_input", 50000)
    nv = root / "sys/class/hwmon/hwmon4"
    _w(nv / "name", "nvme")
    for n, lab in enumerate(("Composite", "Sensor 1", "Sensor 2"), 1):
        _w(nv / f"temp{n}_label", lab)
        _w(nv / f"temp{n}_input", 38850 + 4000 * (n == 2))
    (nv / "device").mkdir(parents=True, exist_ok=True)
    (nv / "device/nvme0n1").mkdir(exist_ok=True)
    _w(root / "sys/class/hwmon/hwmon6/name", "spd5118")
    _w(root / "sys/class/hwmon/hwmon6/temp1_input", 43500)
    _w(root / "sys/class/hwmon/hwmon2/name", "acpitz")
    _w(root / "sys/class/hwmon/hwmon2/temp1_input", 27800)
    bat = root / "sys/class/power_supply/BAT0"
    _w(bat / "type", "Battery")
    _w(bat / "voltage_now", 15_820_000)
    _w(bat / "energy_now", 40_380_000)
    _w(bat / "model_name", "GU60558")
    _w(root / "sys/class/power_supply/ADP0/type", "Mains")
    _w(root / "sys/class/power_supply/ADP0/online", 0)
    blk = root / "sys/block/nvme0n1"
    _w(blk / "device/model", "WD PC SN5000S SDEQNSJ-512G-1002")
    _w(blk / "size", 1_000_215_216)
    _w(blk / "queue/rotational", 0)
    _w(blk / "removable", 0)
    (root / "sys/block/loop0").mkdir(parents=True, exist_ok=True)
    for name, wifi, state in (("wlp0s20f3", True, "up"), ("enp0s31f6", False, "down"),
                              ("docker0", None, "down")):
        d = root / "sys/class/net" / name
        _w(d / "type", 1)
        _w(d / "operstate", state)
        _w(d / "speed", -1)
        if wifi is not None:
            (d / "device").mkdir(parents=True, exist_ok=True)
        if wifi:
            (d / "wireless").mkdir(parents=True, exist_ok=True)
    _w(root / "sys/class/net/lo/type", 772)
    card = root / "sys/class/drm/card1"
    for k, v in (("act", 0), ("cur", 700), ("max", 1600), ("min", 300), ("RP0", 1600),
                 ("boost", 1600)):
        _w(card / f"gt_{k}_freq_mhz", v)
    _w(card / "gt/gt0/rc6_residency_ms", 0)
    _w(card / "gt/gt0/throttle_reason_status", 0)
    _w(card / "device/driver_name", "i915")
    _w(card / "device/address", "0000:00:02.0")
    _w(card / "device/device", "0xa78b")
    pci = root / "sys/bus/pci/devices"
    _w(pci / "0000:00:02.0/device", "0xa78b")
    _w(pci / "0000:01:00.0/device", "0x2dd8")
    _w(pci / "0000:00:02.0/power_state", "D0")
    _w(pci / "0000:01:00.0/power_state", "D0")
    _w(root / "fake/mounts.json", json.dumps([
        {"source": "/dev/nvme0n1p5", "mount": "/", "fstype": "ext4", "size": 468 * GIB,
         "used": 291 * GIB, "avail": 153 * GIB, "disk": "nvme0n1"},
        {"source": "/dev/nvme0n1p1", "mount": "/boot/efi", "fstype": "vfat",
         "size": 256 << 20, "used": 38 << 20, "avail": 218 << 20, "disk": "nvme0n1"}]))
    for pid, ppid, comm, cmd, unit, _cpu, mem, gpu in PROCS:
        d = root / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + (b"\0" if cmd else b""))
        _w(d / "cgroup", f"0::/user.slice/user-1000.slice/{unit}" if unit.startswith("user@")
           else (f"0::/system.slice/{unit}" if unit else "0::/"))
        (d / "fd").mkdir(exist_ok=True)
        (d / "fdinfo").mkdir(exist_ok=True)
        if gpu == "igpu":
            link = d / "fd/9"
            if not link.is_symlink():
                os.symlink("/dev/dri/renderD129", link)
    (root / "proc/net").mkdir(parents=True, exist_ok=True)
    _w(root / "fake/telemetry-ready", 1)


class FakeWorld:
    """Integrates fake load into monotonic counters (jiffies, sectors, bytes, ns)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        ensure(self.root)
        self.t: float | None = None
        self.cpu = [[0] * 8 for _ in range(NCPU)]  # user nice system idle iowait irq soft st
        self.ctxt = self.intr = 0
        self.disk = [0, 0, 0, 0, 0]
        self.net = {"wlp0s20f3": [0, 0, 0, 0], "enp0s31f6": [0, 0, 0, 0],
                    "docker0": [0, 0, 0, 0]}
        self.proc_ticks = {p[0]: 0 for p in PROCS}
        self.proc_io = {p[0]: [0, 0] for p in PROCS}
        self.render_ns = {p[0]: 0 for p in PROCS}
        self.rc6_ms = 0
        self.uptime = 18_450.0

    def advance(self, now: float) -> None:
        if self.t is None:
            self.t = now - 1.0
        dt = max(0.0, now - self.t)
        self.t = now
        self.uptime += dt
        L = load_at(now)
        r = self.root
        lines = []
        tot = [0] * 8
        for i, c in enumerate(self.cpu):
            p = i < P_THREADS
            u = min(0.98, 0.04 + L * (0.75 if p else 0.45) * (0.6 + 0.4 * math.sin(i * 1.7 +
                                                                                      now / 5)))
            ticks = dt * CLK
            c[0] += int(ticks * u * 0.8)
            c[2] += int(ticks * u * 0.2)
            c[4] += int(ticks * 0.004)
            c[3] += int(ticks * (1 - u))
            mhz = 800 + (4200 if p else 2900) * min(1.0, u * 1.1)
            _w(r / f"sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", int(mhz * 1000))
            for k in range(8):
                tot[k] += c[k]
            lines.append(f"cpu{i} " + " ".join(map(str, c)) + " 0 0")
        self.ctxt += int(dt * (9000 + 30000 * L))
        self.intr += int(dt * (6000 + 20000 * L))
        (r / "proc/stat").write_text(
            "cpu  " + " ".join(map(str, tot)) + " 0 0\n" + "\n".join(lines) +
            f"\nintr {self.intr} 0\nctxt {self.ctxt}\nbtime 1792705358\nprocesses 1096000\n"
            f"procs_running {1 + int(L * 9)}\nprocs_blocked 0\n")
        _w(r / "proc/loadavg", f"{0.6 + 7 * L:.2f} {0.5 + 5 * L:.2f} {1.9:.2f} "
           f"{1 + int(L * 9)}/1874 41022")
        _w(r / "proc/uptime", f"{self.uptime:.2f} {self.uptime * 20:.2f}")
        hw = r / "sys/class/hwmon/hwmon7"
        pkg = 48 + 34 * L
        _w(hw / "temp1_input", int(pkg * 1000))
        for n in range(2, 18):
            _w(hw / f"temp{n}_input", int((pkg - 6 + 5 * math.sin(n + now / 7)) * 1000))
        _w(r / "sys/class/hwmon/hwmon8/fan1_input", int(2600 + 3200 * L))
        _w(r / "sys/class/hwmon/hwmon8/fan2_input", int(2300 + 3000 * L))
        used = int(MEM_TOTAL * (0.48 + 0.22 * L))
        free = int(MEM_TOTAL * 0.09)
        cached = MEM_TOTAL - used - free
        kb = 1024
        (r / "proc/meminfo").write_text(
            f"MemTotal: {MEM_TOTAL // kb} kB\nMemFree: {free // kb} kB\n"
            f"MemAvailable: {(free + cached) // kb} kB\nBuffers: {180 << 10} kB\n"
            f"Cached: {(cached - (500 << 20)) // kb} kB\nSwapCached: {72 << 10} kB\n"
            f"Shmem: {1300 << 10} kB\nSReclaimable: {320 << 10} kB\nSUnreclaim: {210 << 10} kB\n"
            f"Dirty: {int(2 + 30 * L) << 10} kB\nWriteback: 0 kB\nAnonPages: {used // kb} kB\n"
            f"Mapped: {900 << 10} kB\nSwapTotal: {4 << 20} kB\n"
            f"SwapFree: {int((4 << 20) * (0.62 - 0.05 * L))} kB\nZswap: 0 kB\nZswapped: 0 kB\n"
            f"CommitLimit: {12 << 20} kB\nCommitted_AS: {int((14 + 6 * L) * (1 << 20))} kB\n")
        d = self.disk
        rd = (2 + 180 * L ** 3) * (1 << 20) * dt
        wr = (1 + 40 * L * abs(math.sin(now / 9))) * (1 << 20) * dt
        d[0] += int(rd / 65536) + 1
        d[1] += int(rd / 512)
        d[2] += int(wr / 32768) + 1
        d[3] += int(wr / 512)
        d[4] += int(dt * 1000 * min(1.0, 0.02 + 0.5 * L ** 2))
        (r / "proc/diskstats").write_text(
            f" 259 0 nvme0n1 {d[0]} 0 {d[1]} 0 {d[2]} 0 {d[3]} 0 0 {d[4]} {d[4]} 0 0 0 0 0 0\n"
            f" 259 3 nvme0n1p5 {d[0]} 0 {d[1]} 0 {d[2]} 0 {d[3]} 0 0 {d[4]} {d[4]} 0 0 0 0\n"
            "   7 0 loop0 3 0 24 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n")
        w = self.net["wlp0s20f3"]
        w[0] += int(dt * (40e3 + 4.2e6 * L * (0.5 + 0.5 * math.sin(now / 4))))
        w[1] += int(dt * 900)
        w[2] += int(dt * (12e3 + 0.6e6 * L))
        w[3] += int(dt * 400)
        rows = []
        for name, v in [("lo", [123456, 900, 123456, 900])] + list(self.net.items()):
            rows.append(f"{name:>8}: {v[0]} {v[1]} 0 0 0 0 0 0 {v[2]} {v[3]} 0 0 0 0 0 0")
        (r / "proc/net/dev").write_text(
            "Inter-|   Receive\n face |bytes    packets\n" + "\n".join(rows) + "\n")
        (r / "proc/net/wireless").write_text(
            "Inter-| sta-|   Quality\n face | tus | link level noise\n"
            " wlp0s20f3: 0000   58.  -52.  -256        0      0      0      0      0        0\n")
        # iGPU
        card = r / "sys/class/drm/card1"
        busy = 0.10 + 0.35 * L
        self.rc6_ms += int(dt * 1000 * (1 - busy - 0.08))
        _w(card / "gt/gt0/rc6_residency_ms", self.rc6_ms)
        _w(card / "gt_act_freq_mhz", int(300 + 1100 * busy))
        _w(card / "gt_cur_freq_mhz", int(350 + 1150 * busy))
        # processes
        game_dgpu = 0.25 + 0.7 * L
        for pid, ppid, comm, cmd, unit, cpu_pct, mem, gpu in PROCS:
            k = cpu_pct * (0.4 + 1.2 * L) if pid in (3488, 2150, 1460) else cpu_pct
            self.proc_ticks[pid] += int(dt * CLK * k / 100 + 0.5)
            ticks = self.proc_ticks[pid]
            pd = r / "proc" / str(pid)
            rss = int(mem * (1 << 20) * (1 + (0.1 * L if pid == 3488 else 0)) / PAGE)
            (pd / "stat").write_text(
                f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0 0 0 0 {ticks * 3 // 4} "
                f"{ticks // 4} 0 0 20 0 {6 if rss else 1} 0 {1000 + pid} 0 {rss} "
                "18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0\n")
            shared = rss // 6
            (pd / "statm").write_text(f"{rss * 3} {rss + shared} {shared} 10 0 {rss} 0\n")
            io = self.proc_io[pid]
            if pid in (3488, 2610, 2101):
                io[0] += int(dt * L * (9e6 if pid == 3488 else 2e5))
                io[1] += int(dt * L * (1.5e6 if pid == 3488 else 8e4))
            (pd / "status").write_text(
                f"Name:\t{comm}\nVmRSS:\t{rss * PAGE // 1024} kB\n"
                f"VmSwap:\t{(mem * 1024 // 3) if mem else 0} kB\n")
            (pd / "io").write_text(f"rchar: {io[0]}\nwchar: {io[1]}\nread_bytes: {io[0]}\n"
                                   f"write_bytes: {io[1]}\n")
            if gpu == "igpu":
                share = {1460: 0.55, 2101: 0.3, 2170: 0.1}.get(pid, 0.05)
                self.render_ns[pid] += int(dt * 1e9 * busy * share)
                (pd / "fdinfo/9").write_text(
                    "pos:\t0\nflags:\t02104002\ndrm-driver:\ti915\n"
                    f"drm-client-id:\t{pid}\ndrm-pdev:\t0000:00:02.0\n"
                    f"drm-total-system0:\t{mem * 300} KiB\n"
                    f"drm-engine-render:\t{self.render_ns[pid]} ns\n"
                    f"drm-engine-copy:\t{self.render_ns[pid] // 50} ns\n"
                    f"drm-engine-video:\t{self.render_ns[pid] // 9 if pid == 2170 else 0} ns\n"
                    "drm-engine-video-enhance:\t0 ns\n")
        _w(r / "sys/class/power_supply/BAT0/power_now", int((14 + 46 * L) * 1e6))
        vram = int((2.1 + 3.6 * L) * GIB)
        (r / "fake/nvml.json").write_text(json.dumps({
            "static": {"name": "NVIDIA GeForce RTX 5050 Laptop GPU",
                       "driver_version": "580.178.04", "max_gr_mhz": 3090,
                       "max_mem_mhz": 12001},
            "query": {"util": round(100 * game_dgpu), "mem_util": round(60 * game_dgpu),
                      "vram_used": vram, "vram_total": 8151 << 20, "gr_mhz": int(
                          1200 + 1500 * game_dgpu), "mem_mhz": 12001, "video_mhz": 1800,
                      "temp": int(46 + 32 * L), "power": round(12 + 78 * game_dgpu, 1),
                      "power_limit": 100.0, "fan": None, "pstate": 0 if L > 0.15 else 2,
                      "enc": 0.0, "dec": 0.0},
            "procs": {"3488": {"type": "C+G", "vram": vram - (380 << 20),
                               "sm": round(100 * game_dgpu)},
                      "1460": {"type": "G", "vram": 2 << 20, "sm": None}}}))
