# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Telemetry and process snapshots for the agent read tools.

Uses the same GTK-free readers as the GUI (`telemetry.Sampler`, `ProcessWalker`). Rates
(CPU %, disk and network throughput) need two samples: the first call takes two, half
a second apart; later calls reuse the previous sample as the baseline. The discrete GPU
is never woken: while it is runtime-suspended only its power state is reported. Under
ASUSIUS_FAKE_ROOT the fake world is advanced on a virtual clock instead of sleeping.
"""
from __future__ import annotations

import time
from pathlib import Path

from .sanitize import untrusted

SECTIONS = ("cpu", "memory", "gpus", "fans", "storage", "network", "battery")
MIB = 1 << 20
GIB = 1 << 30
BASELINE_MIN_S = 0.5
BASELINE_MAX_S = 60.0


def _r(v, digits: int = 1):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, float) and v != v:
        return None
    return round(float(v), digits) if digits else int(round(v))


def _mib(v):
    return None if v is None else _r(v / MIB, 0)


class Observer:
    def __init__(self, root: str = "/", fake: bool = False, sleep=time.sleep,
                 clock=time.monotonic) -> None:
        self.root, self.fake = root, fake
        self.sleep, self.clock = sleep, clock
        self._sampler = None
        self._world = None
        self._walker = None
        self._last: tuple | None = None   # (now, vals, info)
        self._walk_t: float | None = None
        self._vclock = 0.0

    # -- clocks ---------------------------------------------------------------------
    def _now(self) -> float:
        if self.fake:  # virtual time: fake counters advance without waiting
            self._vclock = max(self._vclock + 1.0, self.clock())
            return self._vclock
        return self.clock()

    def _setup(self) -> None:
        if self._sampler is not None:
            return
        from ..telemetry import Sampler
        if self.fake:
            from ..telemetry.fake import FakeWorld
            self._world = FakeWorld(Path(self.root))
        self._sampler = Sampler(self.root, fake=self.fake, capacity=4)
        self._sampler.disk.want_mounts = True

    def _collect(self):
        now = self._now()
        if self._world is not None:
            self._world.advance(now)
        return self._sampler.collect(now)

    def sample(self) -> tuple[dict, dict]:
        self._setup()
        last = self._last
        age = None if last is None else self.clock() - last[0] if not self.fake else 1.0
        if last is None or age is None or age > BASELINE_MAX_S:
            self._collect()
            if not self.fake:
                self.sleep(BASELINE_MIN_S)
        elif not self.fake and age < BASELINE_MIN_S:
            self.sleep(BASELINE_MIN_S - age)
        now, vals, info = self._collect()
        self._last = (self.clock() if not self.fake else now, vals, info)
        return vals, info

    def close(self) -> None:
        if self._sampler is not None:
            self._sampler.close()

    # -- sections -----------------------------------------------------------------------
    def snapshot(self, sections=SECTIONS) -> dict:
        vals, info = self.sample()
        out: dict = {}
        withheld = []
        for name in sections:
            fn = getattr(self, f"_s_{name}")
            out[name] = fn(vals, info, withheld)
        errs = {k: v for k, v in (info.get("errors") or {}).items()}
        if errs:
            out["errors"] = errs
        if withheld:
            out["withheld_fields"] = withheld
        return out

    def _s_cpu(self, v, info, withheld) -> dict:
        c = info.get("cpu") or {}
        st = self._sampler.cpu.static
        model, w = untrusted(st.get("model"), 80)
        if w:
            withheld.append("cpu.model")
        load_l = list(c.get("load") or ())[:3]
        return {"usage_pct": _r(v.get("cpu.total")), "user_pct": _r(v.get("cpu.user")),
                "system_pct": _r(v.get("cpu.system")), "iowait_pct": _r(v.get("cpu.iowait")),
                "freq_avg_mhz": _r(v.get("cpu.freq.avg"), 0), "temp_c": _r(v.get("temp.cpu")),
                "package_power_w": _r(v.get("power.cpu")),
                "load_avg": [_r(x, 2) for x in (load_l or [])][:3],
                "logical_cpus": st.get("count"), "cores": st.get("cores"),
                "model": model, "uptime_s": _r(c.get("uptime"), 0),
                "procs_running": c.get("procs_running")}

    def _s_memory(self, v, info, _w) -> dict:
        m = info.get("mem") or {}
        psi = (m.get("psi") or {}).get("some") or {}
        return {"total_mib": _mib(m.get("total")), "in_use_mib": _mib(m.get("in_use")),
                "available_mib": _mib(m.get("available")),
                "reclaimable_mib": _mib(m.get("reclaimable")), "free_mib": _mib(m.get("free")),
                "swap_total_mib": _mib(m.get("swap_total")),
                "swap_used_mib": _mib(m.get("swap_used")),
                "pressure_some_avg10_pct": _r(psi.get("avg10"), 2)}

    def _s_gpus(self, v, info, withheld) -> dict:
        ig = info.get("igpu") or {}
        dg = info.get("dgpu") or {}
        iname, w1 = untrusted(ig.get("name"), 80)
        dname, w2 = untrusted(dg.get("name"), 80)
        if w1:
            withheld.append("gpus.integrated.name")
        if w2:
            withheld.append("gpus.discrete.name")
        integrated = {"present": bool(ig.get("present")), "name": iname or None,
                      "util_pct": _r(v.get("igpu.util")), "freq_mhz": _r(v.get("igpu.freq"), 0),
                      "awake_pct": _r(v.get("igpu.awake"))}
        awake = dg.get("state") == "awake"
        q = dg.get("q") or {}
        discrete = {"present": bool(dg.get("present")), "name": dname or None,
                    "state": dg.get("state"), "runtime_pm": dg.get("runtime"),
                    "note": ("The NVIDIA driver lost access to this GPU. Restart the "
                             "laptop to recover it; monitoring is paused."
                             if dg.get("state") == "lost" else
                             None if awake else
                             "not queried while suspended (never woken)"
                             if dg.get("state") == "sleeping" else dg.get("reason"))}
        if awake:
            discrete.update({
                "util_pct": _r(v.get("dgpu.util")), "temp_c": _r(v.get("dgpu.temp")),
                "power_w": _r(v.get("dgpu.power")), "core_mhz": _r(v.get("dgpu.gr_mhz"), 0),
                "vram_used_mib": _mib(v.get("dgpu.vram")),
                "vram_total_mib": _mib(q.get("vram_total"))})
        return {"integrated": integrated, "discrete": discrete}

    def _s_fans(self, v, info, _w) -> dict:
        s = info.get("sensors") or {}
        fans = {key: _r(v.get(f"fan.{key}"), 0) for key, _lab in s.get("fans", [])}
        temps = {"cpu_package": _r(v.get("temp.cpu"))}
        for disk in (s.get("nvme") or {}):
            temps[f"nvme_{disk}"] = _r(v.get(f"temp.disk.{disk}.0"))
        for key, _lab in s.get("extra", []):
            temps[key] = _r(v.get(f"temp.{key}"))
        dg = info.get("dgpu") or {}
        if dg.get("state") == "awake":
            temps["dgpu"] = _r(v.get("dgpu.temp"))
        return {"fans_rpm": fans, "temps_c": temps}

    def _s_storage(self, v, info, withheld) -> dict:
        d = info.get("disk") or {}
        disks = {}
        for name in d.get("disks") or {}:
            safe, _ = untrusted(name, 32)
            disks[safe] = {"read_bytes_per_s": _r(v.get(f"disk.{name}.read"), 0),
                           "write_bytes_per_s": _r(v.get(f"disk.{name}.write"), 0),
                           "busy_pct": _r(v.get(f"disk.{name}.busy"))}
        mounts = []
        for i, m in enumerate((d.get("mounts") or [])[:16]):
            mnt, w = untrusted(m.get("mount"), 80)
            if w:
                withheld.append(f"storage.mounts[{i}].mount")
            size = m.get("size") or 0
            mounts.append({"mount": mnt, "fstype": untrusted(m.get("fstype"), 16)[0],
                           "size_gib": _r(size / GIB), "avail_gib": _r((m.get("avail") or 0) / GIB),
                           "used_pct": _r(100 * (m.get("used") or 0) / size) if size else None})
        return {"read_bytes_per_s": _r(v.get("disk.read"), 0),
                "write_bytes_per_s": _r(v.get("disk.write"), 0), "disks": disks,
                "mounts": mounts}

    def _s_network(self, v, info, _w) -> dict:
        n = (info.get("net") or {}).get("ifaces") or {}
        ifaces = {}
        for name, meta in list(n.items())[:16]:
            safe, _ = untrusted(name, 32)
            wifi = meta.get("wifi")
            ifaces[safe] = {"kind": meta.get("kind"), "state": meta.get("state"),
                            "virtual": meta.get("virtual"),
                            "rx_bytes_per_s": _r(v.get(f"net.{name}.rx"), 0),
                            "tx_bytes_per_s": _r(v.get(f"net.{name}.tx"), 0),
                            "speed_mbps": meta.get("speed_mbps"),
                            "wifi_signal_dbm": _r(wifi[1], 0) if wifi else None}
        return {"rx_bytes_per_s": _r(v.get("net.rx"), 0), "tx_bytes_per_s": _r(v.get("net.tx"), 0),
                "interfaces": ifaces}

    def _s_battery(self, v, info, withheld) -> dict:
        b = info.get("battery") or {}
        model, w = untrusted(b.get("model"), 40)
        if w:
            withheld.append("battery.model")
        full, design = b.get("energy_full"), b.get("energy_design")
        health = min(100.0, max(0.0, 100 * full / design)) if full and design else None
        return {"present": bool(b.get("present")), "percent": b.get("percent"),
                "status": b.get("status"), "ac_online": b.get("ac_online"),
                "power_w": _r(b.get("watts")), "voltage_v": _r(b.get("voltage"), 2),
                "energy_now_wh": _r((b.get("energy_now") or 0) / 1e6) if b.get(
                    "energy_now") is not None else None,
                "energy_full_wh": _r(full / 1e6) if full else None,
                "energy_design_wh": _r(design / 1e6) if design else None,
                "health_pct": _r(health),
                "cycles": b.get("cycles"), "charge_limit_pct": b.get("limit"),
                "hours_left_at_current_draw": _r(b.get("hours"), 2), "model": model or None}

    def _gpu_maps(self, now: float) -> tuple[dict | None, dict | None]:
        vals: dict = {}
        info: dict = {}
        for src in (self._sampler.igpu, self._sampler.dgpu):
            try:
                src.sample(now, vals, info)
            except Exception:
                pass
        return info.get("igpu", {}).get("per_pid"), info.get("dgpu", {}).get("procs")

    # -- processes ---------------------------------------------------------------------------
    def processes(self, *, sort: str = "cpu", limit: int = 10, name_filter: str | None = None,
                  include_kernel: bool = False) -> dict:
        from ..telemetry.procs import ProcessWalker
        self._setup()
        if self._walker is None:
            self._walker = ProcessWalker(self.root, self._sampler.cpu.static.get("count"))
            now = self._now()
            if self._world is not None:
                self._world.advance(now)
            ig_map, dg_map = self._gpu_maps(now)
            self._walker.walk(now, ig_map, dg_map)
            self._walk_t = self.clock()
            if not self.fake:
                self.sleep(BASELINE_MIN_S)
        elif not self.fake:
            age = self.clock() - (self._walk_t or 0)
            if age < BASELINE_MIN_S:
                self.sleep(BASELINE_MIN_S - age)
        now = self._now()
        if self._world is not None:
            self._world.advance(now)
        ig_map, dg_map = self._gpu_maps(now)
        procs = self._walker.walk(now, ig_map, dg_map)
        self._walk_t = self.clock()
        total = len(procs)
        if not include_kernel:
            procs = [p for p in procs if not p.kthread]
        needle = name_filter.lower() if name_filter else None
        rows = []
        for p in procs:
            if needle and needle not in (p.name or "").lower() and needle not in (
                    p.app or "").lower():
                continue
            rows.append(p)
        key = (lambda p: p.cpu or 0.0) if sort == "cpu" else (lambda p: p.mem or 0)
        rows.sort(key=key, reverse=True)
        out = []
        withheld = []
        for i, p in enumerate(rows[:limit]):
            name, w1 = untrusted(p.name, 48)
            app, w2 = untrusted(p.app, 64) if p.app else ("", False)
            if w1:
                withheld.append(f"processes[{i}].name")
            if w2:
                withheld.append(f"processes[{i}].app")
            out.append({"pid": p.pid, "name": name, "app": app or None,
                        "cpu_pct": _r(p.cpu), "mem_mib": _mib(p.mem), "rss_mib": _mib(p.rss),
                        "threads": p.threads, "state": p.state[:1] if p.state else None,
                        "own": p.uid == self._walker.my_uid, "gpu_pct": _r(p.gpu)})
        res = {"sort": sort, "total_processes": total, "matched": len(rows),
               "processes": out}
        if withheld:
            res["withheld_fields"] = withheld
        return res
