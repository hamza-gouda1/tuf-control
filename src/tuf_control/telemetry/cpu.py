# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""CPU: /proc/stat usage deltas, cpufreq, topology (P/E cores), load and uptime."""
from __future__ import annotations

import os

from .util import join, listdir, read_bytes, read_int, read_str

# /proc/stat cpu columns
USER, NICE, SYSTEM, IDLE, IOWAIT, IRQ, SOFTIRQ, STEAL = range(8)


def parse_stat(data: bytes) -> tuple[tuple[int, ...] | None, dict[int, tuple[int, ...]], dict]:
    """(aggregate cpu fields, {cpu index: fields}, misc counters) from /proc/stat."""
    agg = None
    per: dict[int, tuple[int, ...]] = {}
    misc: dict[str, int] = {}
    for line in data.split(b"\n"):
        if line.startswith(b"cpu"):
            parts = line.split()
            fields = tuple(int(x) for x in parts[1:9])
            if len(fields) < 8:
                fields = fields + (0,) * (8 - len(fields))
            name = parts[0]
            if name == b"cpu":
                agg = fields
            else:
                per[int(name[3:])] = fields
        elif line.startswith((b"ctxt ", b"processes ", b"procs_running ", b"procs_blocked ",
                              b"btime ")):
            k, v = line.split()[:2]
            misc[k.decode()] = int(v)
        elif line.startswith(b"intr "):
            end = line.find(b" ", 5)
            misc["intr"] = int(line[5:end if end > 0 else None])
    return agg, per, misc


def usage(cur: tuple[int, ...], prev: tuple[int, ...]) -> tuple[float, float, float, float] | None:
    """(busy, user, system, iowait) percent between two /proc/stat samples.

    busy = everything except idle and iowait over all eight time columns (the same
    definition top uses); guest time is already inside user/nice and not added again.
    """
    d = [max(0, c - p) for c, p in zip(cur[:8], prev[:8])]
    total = sum(d)
    if total <= 0:
        return None
    idle = d[IDLE] + d[IOWAIT]
    busy = total - idle
    k = 100.0 / total
    return (busy * k, (d[USER] + d[NICE]) * k, (d[SYSTEM] + d[IRQ] + d[SOFTIRQ] + d[STEAL]) * k,
            d[IOWAIT] * k)


def parse_cpulist(text: str | None) -> list[int]:
    """'0-3,8,10-11' -> [0, 1, 2, 3, 8, 10, 11]."""
    out: list[int] = []
    if not text:
        return out
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def parse_loadavg(text: str | None) -> tuple[float, float, float, int, int] | None:
    """(1, 5, 15 min load, runnable, total threads)."""
    if not text:
        return None
    p = text.split()
    run, total = p[3].split("/")
    return float(p[0]), float(p[1]), float(p[2]), int(run), int(total)


class CpuReader:
    def __init__(self, root: str = "/") -> None:
        self.root = root
        self._prev: tuple | None = None  # (t, agg, per, misc)
        self.static = self._static()
        sysd = join(root, "sys/devices/system/cpu")
        self._freq_paths = [os.path.join(sysd, f"cpu{i}/cpufreq/scaling_cur_freq")
                            for i in self.static["cpus"]]

    # -- once -------------------------------------------------------------------------
    def _static(self) -> dict:
        root = self.root
        sysd = join(root, "sys/devices/system/cpu")
        cpus = parse_cpulist(read_str(os.path.join(sysd, "online")))
        if not cpus:
            cpus = sorted(int(d[3:]) for d in listdir(sysd) if d[3:].isdigit()
                          and d.startswith("cpu"))
        model = None
        info = read_bytes(join(root, "proc/cpuinfo"), 1 << 16) or b""
        for line in info.split(b"\n"):
            if line.startswith(b"model name"):
                model = line.split(b":", 1)[1].strip().decode(errors="replace")
                break
        p_cores = set(parse_cpulist(read_str(join(root, "sys/devices/cpu_core/cpus"))))
        e_cores = set(parse_cpulist(read_str(join(root, "sys/devices/cpu_atom/cpus"))))
        topo = {}
        for i in cpus:
            d = os.path.join(sysd, f"cpu{i}")
            core = read_int(os.path.join(d, "topology/core_id"))
            pkg = read_int(os.path.join(d, "topology/physical_package_id")) or 0
            sib = parse_cpulist(read_str(os.path.join(d, "topology/thread_siblings_list")))
            kind = "P" if i in p_cores else ("E" if i in e_cores else "")
            topo[i] = {
                "core_id": core, "package": pkg, "kind": kind,
                "thread": sib.index(i) if i in sib else 0, "siblings": sib,
                "max_mhz": (read_int(os.path.join(d, "cpufreq/cpuinfo_max_freq")) or 0) / 1000
                or None,
                "min_mhz": (read_int(os.path.join(d, "cpufreq/cpuinfo_min_freq")) or 0) / 1000
                or None,
                "base_mhz": (read_int(os.path.join(d, "cpufreq/base_frequency")) or 0) / 1000
                or None,
            }
        physical = {(t["package"], t["core_id"]) for t in topo.values()}
        return {
            "cpus": cpus, "count": len(cpus), "model": model, "topology": topo,
            "cores": len(physical),
            "p_cores": len({(t["package"], t["core_id"]) for t in topo.values()
                            if t["kind"] == "P"}),
            "e_cores": len({(t["package"], t["core_id"]) for t in topo.values()
                            if t["kind"] == "E"}),
            "hybrid": bool(p_cores and e_cores),
        }

    def policy(self) -> dict:
        """Governor, EPP and driver of cpu0 (cheap; read when the CPU page refreshes)."""
        d = join(self.root, "sys/devices/system/cpu/cpu0/cpufreq")
        pst = join(self.root, "sys/devices/system/cpu/intel_pstate")
        return {
            "driver": read_str(os.path.join(d, "scaling_driver")),
            "governor": read_str(os.path.join(d, "scaling_governor")),
            "epp": read_str(os.path.join(d, "energy_performance_preference")),
            "pstate_status": read_str(os.path.join(pst, "status")),
            "no_turbo": read_str(os.path.join(pst, "no_turbo")),
        }

    # -- per tick ---------------------------------------------------------------------
    def sample(self, now: float, vals: dict, info: dict) -> None:
        data = read_bytes(join(self.root, "proc/stat"))
        if data is None:
            info["cpu"] = {"error": "/proc/stat unreadable"}
            return
        agg, per, misc = parse_stat(data)
        prev, self._prev = self._prev, (now, agg, per, misc)
        out = {}
        if prev is not None and agg is not None and prev[1] is not None:
            dt = now - prev[0]
            u = usage(agg, prev[1])
            if u is not None:
                vals["cpu.total"], vals["cpu.user"], vals["cpu.system"], vals["cpu.iowait"] = u
            for i, f in per.items():
                pf = prev[2].get(i)
                if pf is not None:
                    pu = usage(f, pf)
                    vals[f"cpu.{i}"] = None if pu is None else pu[0]
            if dt > 0:
                for k in ("ctxt", "intr", "processes"):
                    if k in misc and k in prev[3] and misc[k] >= prev[3][k]:
                        vals[f"cpu.{k}"] = (misc[k] - prev[3][k]) / dt
        total = 0.0
        n = 0
        for i, path in zip(self.static["cpus"], self._freq_paths):
            khz = read_int(path)
            if khz is not None:
                vals[f"cpu.freq.{i}"] = khz / 1000
                total += khz / 1000
                n += 1
        if n:
            vals["cpu.freq.avg"] = total / n
        out["load"] = parse_loadavg(read_str(join(self.root, "proc/loadavg")))
        up = read_str(join(self.root, "proc/uptime"))
        out["uptime"] = float(up.split()[0]) if up else None
        out["procs_running"] = misc.get("procs_running")
        out["procs_blocked"] = misc.get("procs_blocked")
        info["cpu"] = out
