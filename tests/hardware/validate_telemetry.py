#!/usr/bin/env python3
"""Cross-check the telemetry layer against independent tools on the real machine.

    PYTHONPATH=src python3 tests/hardware/validate_telemetry.py [--seconds 10] [--markdown]

Read-only. Samples each source for about --seconds and compares with:
  CPU      independent /proc/stat reads in the same windows, and `top -bn2`
  per-core /proc/cpuinfo "cpu MHz" (frequency), the same /proc/stat windows (usage)
  memory   `free -b`
  NVIDIA   `nvidia-smi --query-gpu` (only if the dGPU is already awake; never woken)
  sensors  `sensors -j` (fans, CPU package, NVMe)
  disk/net /sys/block/*/stat and /sys/class/net/*/statistics over the same windows
  battery  `upower -i`
  processes `ps -o rss` for a few PIDs
Exit status 1 if any comparison is outside its tolerance.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tuf_control.telemetry import Sampler  # noqa: E402
from tuf_control.telemetry.memory import derive, parse_meminfo  # noqa: E402
from tuf_control.telemetry.pci import runtime_state  # noqa: E402
from tuf_control.telemetry.procs import ProcessWalker  # noqa: E402

ROWS: list[tuple] = []


def check(group, metric, ours, ref, tol, unit="", note=""):
    if ours is None or ref is None:
        ROWS.append((group, metric, ours, ref, None, tol, unit, "SKIP", note))
        return
    diff = abs(ours - ref)
    ROWS.append((group, metric, ours, ref, diff, tol, unit, "OK" if diff <= tol else "FAIL",
                 note))


def run(argv, timeout=10):
    if not shutil.which(argv[0]):
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def stat_busy():
    """Independent minimal /proc/stat reader: {cpu name: (busy, total)}."""
    out = {}
    for line in open("/proc/stat"):
        if line.startswith("cpu"):
            p = line.split()
            v = [int(x) for x in p[1:9]]
            idle = v[3] + v[4]
            out[p[0]] = (sum(v) - idle, sum(v))
    return out


def pct(a, b):
    db, dt = b[0] - a[0], b[1] - a[1]
    return 100.0 * db / dt if dt > 0 else None


def sys_disk(name):
    f = open(f"/sys/block/{name}/stat").read().split()
    return int(f[2]) * 512, int(f[6]) * 512


def sys_net(name):
    d = f"/sys/class/net/{name}/statistics"
    return int(open(f"{d}/rx_bytes").read()), int(open(f"{d}/tx_bytes").read())


LOAD_READ = """
import os, sys, glob
files = sorted((f for f in glob.glob('/usr/lib/x86_64-linux-gnu/*.so*') if os.path.isfile(f)),
               key=os.path.getsize, reverse=True)[:40]
while True:
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # drop clean cache: real reads
        while os.read(fd, 1 << 20):
            pass
        os.close(fd)
"""


def start_load() -> list:
    """Two busy threads and a disk reader (unprivileged, read-only), killed afterwards."""
    burn = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(2)]
    return burn + [subprocess.Popen([sys.executable, "-c", LOAD_READ])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--no-load", action="store_true",
                    help="do not generate CPU and disk-read load during the windows")
    args = ap.parse_args()
    load = [] if args.no_load else start_load()
    s = Sampler("/")
    disks = list(s.disk.disks)
    nets = [n for n in os.listdir("/sys/class/net") if n != "lo"]

    # --- CPU / disk / net over identical windows ------------------------------------
    n = max(3, int(args.seconds))
    t = time.monotonic()
    ref_stat = stat_busy()  # read right before the sampler's own /proc/stat read
    s.sample(t)
    ref_disk = {d: sys_disk(d) for d in disks}
    ref_net = {i: sys_net(i) for i in nets}
    cpu_diffs, core_max, disk_err, net_err, freq_pairs = [], 0.0, {}, {}, []
    for _ in range(n):
        time.sleep(1.0)
        t1 = time.monotonic()
        st = stat_busy()
        vals = s.sample(t1)
        mhz = [float(m) for m in re.findall(r"cpu MHz\s*:\s*([\d.]+)",
                                            open("/proc/cpuinfo").read())]
        if mhz and vals.get("cpu.freq.avg"):
            freq_pairs.append((vals["cpu.freq.avg"], sum(mhz) / len(mhz)))
        dk = {d: sys_disk(d) for d in disks}
        nt = {i: sys_net(i) for i in nets}
        dt = t1 - t
        ref = pct(ref_stat["cpu"], st["cpu"])
        if vals.get("cpu.total") is not None and ref is not None:
            cpu_diffs.append(abs(vals["cpu.total"] - ref))
        for k in st:
            if k != "cpu" and vals.get(f"cpu.{k[3:]}") is not None:
                core_max = max(core_max, abs(vals[f"cpu.{k[3:]}"] - pct(ref_stat[k], st[k])))
        for d in disks:
            for j, key in ((0, "read"), (1, "write")):
                r = (dk[d][j] - ref_disk[d][j]) / dt
                o = vals.get(f"disk.{d}.{key}")
                if o is not None:
                    e = abs(o - r) / max(r, 64 * 1024)
                    disk_err[(d, key)] = max(disk_err.get((d, key), 0), e)
        for i in nets:
            for j, key in ((0, "rx"), (1, "tx")):
                r = (nt[i][j] - ref_net[i][j]) / dt
                o = vals.get(f"net.{i}.{key}")
                if o is not None:
                    e = abs(o - r) / max(r, 16 * 1024)
                    net_err[(i, key)] = max(net_err.get((i, key), 0), e)
        ref_stat, ref_disk, ref_net, t = st, dk, nt, t1
    for proc in load:
        proc.kill()
    check("CPU", f"total % vs same-window /proc/stat (max of {n})",
          max(cpu_diffs) if cpu_diffs else None, 0.0, 0.5, "pt")
    check("CPU", "per-core % vs same-window /proc/stat (max)", core_max, 0.0, 1.0, "pt",
          "1 jiffy = 1 % of one core per second")
    for (d, key), e in sorted(disk_err.items()):
        check("Disk", f"{d} {key} rate vs /sys/block stat (max rel. err)", e * 100, 0.0, 5.0,
              "%", "floor 64 KiB/s")
    for (i, key), e in sorted(net_err.items()):
        check("Network", f"{i} {key} rate vs sysfs statistics (max rel. err)", e * 100, 0.0,
              5.0, "%", "floor 16 KiB/s")

    # --- CPU vs top (different but overlapping windows) -----------------------------
    diffs = []
    for _ in range(3):
        s.sample(time.monotonic())
        out = run(["top", "-bn2", "-d", "1"])
        vals = s.sample(time.monotonic())
        if not out:
            break
        lines = [ln for ln in out.splitlines() if ln.startswith("%Cpu(s)")]
        if len(lines) < 2:
            break
        f = dict((k, float(v)) for v, k in re.findall(r"([\d.]+)\s+(\w\w)", lines[-1]))
        top_busy = 100 - f.get("id", 0) - f.get("wa", 0)
        if vals.get("cpu.total") is not None:
            diffs.append(abs(vals["cpu.total"] - top_busy))
    check("CPU", "total % vs top -bn2 (mean |diff|, 3 runs)",
          sum(diffs) / len(diffs) if diffs else None, 0.0, 5.0, "pt",
          "windows overlap but are not identical")

    # --- frequency (paired reads taken in the loop above) -------------------------------
    if freq_pairs:
        ours = sum(a for a, _ in freq_pairs) / len(freq_pairs)
        ref = sum(b for _, b in freq_pairs) / len(freq_pairs)
        check("CPU", f"average clock vs /proc/cpuinfo cpu MHz (mean of {len(freq_pairs)})",
              ours, ref, ours * 0.15, "MHz", "both are instantaneous estimates; 15 %")

    # --- memory vs free -b -------------------------------------------------------------
    out = run(["free", "-b"])
    mi = derive(parse_meminfo(open("/proc/meminfo", "rb").read()))
    if out:
        mem = out.splitlines()[1].split()
        swp = out.splitlines()[2].split()
        total, used, free_, _sh, bc, avail = (int(x) for x in mem[1:7])
        tol = 64 << 20
        check("Memory", "total", mi["total"], total, 0, "B")
        check("Memory", "used (total - available)", mi["in_use"], used, tol, "B",
              "procps 4 definition")
        check("Memory", "available", mi["available"], avail, tol, "B")
        check("Memory", "free", mi["free"], free_, tol, "B")
        check("Memory", "buff/cache", mi["buff_cache"], bc, tol, "B")
        check("Memory", "swap used", mi["swap_used"], int(swp[2]), tol, "B")

    # --- sensors -------------------------------------------------------------------------
    vals = s.sample(time.monotonic())
    out = run(["sensors", "-j"])
    if out:
        try:
            js = json.loads(out)
        except ValueError:
            js = {}

        def find(chip_prefix, label, key_prefix):
            for chip, data in js.items():
                if chip.startswith(chip_prefix) and label in data:
                    for k, v in data[label].items():
                        if k.startswith(key_prefix) and k.endswith("_input"):
                            return v
            return None
        check("Sensors", "CPU fan rpm", vals.get("fan.cpu"), find("asus-", "cpu_fan", "fan"),
              300, "rpm", "read a moment apart")
        check("Sensors", "GPU fan rpm", vals.get("fan.gpu"), find("asus-", "gpu_fan", "fan"),
              300, "rpm")
        check("Sensors", "CPU package °C", vals.get("temp.cpu"),
              find("coretemp-", "Package id 0", "temp"), 3, "°C")
        nv = next(iter(s.sensors.nvme), None)
        check("Sensors", "NVMe composite °C",
              vals.get(f"temp.disk.{nv}.0") if nv else None,
              find("nvme-", "Composite", "temp"), 2, "°C")
    else:
        check("Sensors", "sensors -j", None, None, 0, "", "lm-sensors not installed")

    # --- NVIDIA -------------------------------------------------------------------------
    dg = s.dgpu
    if dg.dev is None:
        check("NVIDIA", "present", None, None, 0, "", "no NVIDIA GPU")
    else:
        rs, ps = runtime_state(dg.dev["dir"])
        if rs not in (None, "active"):
            check("NVIDIA", f"runtime {rs} ({ps})", None, None, 0, "",
                  "asleep: not queried, nvidia-smi not run (would wake it)")
        else:
            dg._hold_until = 0
            s.sample(time.monotonic())
            q = (s.info.get("dgpu") or {}).get("q") or {}
            out = run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,"
                       "temperature.gpu,power.draw,clocks.gr,clocks.mem,pstate",
                       "--format=csv,noheader,nounits", "-i", dg.dev["addr"]])
            if out:
                p = [x.strip() for x in out.splitlines()[0].split(",")]

                def f(x):
                    try:
                        return float(x)
                    except ValueError:
                        return None
                mib = 1 << 20
                check("NVIDIA", "utilisation %", q.get("util"), f(p[0]), 10, "pt")
                check("NVIDIA", "VRAM used MiB", (q.get("vram_used") or 0) / mib
                      if q.get("vram_used") is not None else None, f(p[1]), 32, "MiB")
                check("NVIDIA", "VRAM total MiB", (q.get("vram_total") or 0) / mib
                      if q.get("vram_total") is not None else None, f(p[2]), 1, "MiB")
                check("NVIDIA", "temperature °C", q.get("temp"), f(p[3]), 2, "°C")
                pw = f(p[4])
                check("NVIDIA", "power W", q.get("power"), pw, max(2.0, (pw or 0) * 0.15), "W")
                gr = f(p[5])
                check("NVIDIA", "graphics clock MHz", q.get("gr_mhz"), gr,
                      max(30, (gr or 0) * 0.1), "MHz")
                check("NVIDIA", "memory clock MHz", q.get("mem_mhz"), f(p[6]), 30, "MHz")
                ps_ref = int(p[7][1:]) if p[7].startswith("P") else None
                check("NVIDIA", "P-state", q.get("pstate"), ps_ref, 0, "")
            dg.release()

    # --- iGPU invariants -----------------------------------------------------------------
    s.sample(time.monotonic())
    time.sleep(1)
    vals = s.sample(time.monotonic())
    ig = s.igpu
    if ig.card:
        def rd(name):
            path = os.path.join(ig.card, name)
            return int(open(path).read()) if os.path.exists(path) else None
        check("iGPU", "requested MHz vs sysfs re-read", vals.get("igpu.freq_req"),
              rd("gt_cur_freq_mhz"), 100, "MHz", "the governor may move it between reads")
        rp0 = rd("gt_RP0_freq_mhz")
        act = vals.get("igpu.freq")
        check("iGPU", "actual MHz outside [0, RP0] (excess)",
              None if act is None or rp0 is None else max(0, act - rp0), 0, 0, "MHz",
              "actual is 0 while the GT sleeps in RC6, so it is range-checked")
        u, a = vals.get("igpu.util"), vals.get("igpu.awake")
        if u is not None and a is not None:
            check("iGPU", "render busy ≤ GT awake (excess)", max(0.0, u - a), 0.0, 5.0, "pt",
                  "busy engines imply an awake GT")

    # --- battery vs upower ---------------------------------------------------------------
    vals = s.sample(time.monotonic())
    b = s.info.get("battery") or {}
    if b.get("present"):
        out = run(["upower", "-i", f"/org/freedesktop/UPower/devices/battery_{b['name']}"])
        if out:
            def up(key):
                m = re.search(rf"^\s*{key}:\s*([\d.]+)", out, re.M)
                return float(m.group(1)) if m else None
            check("Battery", "charge %", b.get("percent"), up("percentage"), 1, "%")
            check("Battery", "voltage V", b.get("voltage"), up("voltage"), 0.15, "V",
                  "upower refreshes on its own schedule")
            check("Battery", "power W", b.get("watts"), up("energy-rate"), 1.5, "W")
            check("Battery", "energy full Wh", (b.get("energy_full") or 0) / 1e6,
                  up("energy-full"), 0.1, "Wh")

    # --- processes vs ps -----------------------------------------------------------------
    w = ProcessWalker("/")
    procs = w.walk(time.monotonic())
    sample = sorted(procs, key=lambda p: -p.rss)[:5]
    out = run(["ps", "-o", "pid=,rss=", "-p", ",".join(str(p.pid) for p in sample)])
    if out:
        ref = {int(a): int(b) * 1024 for a, b in (ln.split() for ln in out.splitlines())}
        worst = max((abs(p.rss - ref[p.pid]) for p in sample if p.pid in ref), default=None)
        check("Processes", "RSS of 5 largest vs ps -o rss (max diff)",
              worst / 1024 if worst is not None else None, 0.0, 512, "KiB",
              "read a moment apart")
    s.close()
    report(args.markdown)
    return 1 if any(r[7] == "FAIL" for r in ROWS) else 0


def fmt(v):
    if v is None:
        return "–"
    if isinstance(v, float):
        if abs(v) >= 1e6:
            return f"{v:.4g}"
        return f"{v:.2f}"
    if isinstance(v, int) and abs(v) >= 1e6:
        return f"{v / (1 << 20):.1f} Mi"
    return str(v)


def report(md: bool) -> None:
    head = ("Source", "Check", "Ours", "Reference", "|diff|", "Tolerance", "Result", "Note")
    rows = [(g, m, fmt(o), fmt(r), fmt(d), f"{fmt(t)} {u}".strip(), res, n)
            for g, m, o, r, d, t, u, res, n in ROWS]
    if md:
        print("| " + " | ".join(head) + " |")
        print("|" + "---|" * len(head))
        for r in rows:
            print("| " + " | ".join(r) + " |")
        return
    widths = [max(len(str(x[i])) for x in rows + [head]) for i in range(len(head))]
    for r in [head] + rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


if __name__ == "__main__":
    sys.exit(main())
