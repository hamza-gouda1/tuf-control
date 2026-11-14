# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Intel integrated GPU (i915 or xe): frequency, RC6 residency and engine busy time.

Utilisation comes from the DRM client statistics every driver publishes in
/proc/<pid>/fdinfo (drm-engine-<class>: <busy ns>, or drm-cycles-*/drm-total-cycles-* on
xe). Only processes we may inspect (our own) are visible, which on a desktop covers the
compositor and apps; system-wide PMU counters need perf_event_paranoid <= 0 or root.
"""
from __future__ import annotations

import os

from .util import join, listdir, read_bytes, read_int, read_str

RESCAN_S = 10.0


def _parse_int_tok(raw: bytes) -> int | None:
    parts = raw.split()
    if not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def parse_fdinfo(data: bytes) -> dict | None:
    """DRM client stats from one fdinfo file, None when the fd is not a DRM client."""
    if b"drm-driver" not in data:
        return None
    out: dict = {"engines": {}, "cycles": {}, "total_cycles": {}, "capacity": {}, "mem": {}}
    for line in data.split(b"\n"):
        if not line.startswith(b"drm-"):
            continue
        raw_key, _, val = line.partition(b":")
        key = raw_key.decode("utf-8", errors="replace")
        val = val.strip()
        if key.startswith("drm-engine-capacity-"):
            v = _parse_int_tok(val)
            if v is not None:
                out["capacity"][key[20:]] = v
        elif key.startswith("drm-engine-"):
            v = _parse_int_tok(val)
            if v is not None:
                out["engines"][key[11:]] = v
        elif key.startswith("drm-total-cycles-"):
            v = _parse_int_tok(val)
            if v is not None:
                out["total_cycles"][key[17:]] = v
        elif key.startswith("drm-cycles-"):
            v = _parse_int_tok(val)
            if v is not None:
                out["cycles"][key[11:]] = v
        elif key.startswith("drm-resident-") or key.startswith("drm-total-"):
            parts = val.split()
            n = _parse_int_tok(val)
            if n is not None:
                unit = parts[1] if len(parts) > 1 else b""
                n *= {b"KiB": 1024, b"MiB": 1 << 20, b"GiB": 1 << 30}.get(unit, 1)
                out["mem"][key[4:]] = n
        elif key == "drm-driver":
            out["driver"] = val.decode("utf-8", errors="replace")
        elif key == "drm-pdev":
            out["pdev"] = val.decode("utf-8", errors="replace")
        elif key == "drm-client-id":
            v = _parse_int_tok(val)
            if v is not None:
                out["client"] = v
    return out


class DrmClients:
    """Tracks DRM client fds of readable processes for one PCI device.

    A full /proc/*/fd scan (about 17 ms for 560 processes) runs every RESCAN_S; each tick
    then reads one fdinfo file per client.
    """

    def __init__(self, root: str, pdev: str) -> None:
        self.root, self.pdev = root, pdev
        self.paths: dict[tuple[int, int], str] = {}  # (pid, client id) -> fdinfo path
        self._scan_t = -1e9
        self._prev: dict[tuple[int, int], tuple[float, dict]] = {}

    def rescan(self, now: float) -> None:
        self._scan_t = now
        proc = join(self.root, "proc")
        paths: dict[tuple[int, int], str] = {}
        try:
            pids = os.listdir(proc)
        except OSError:
            self.paths = {}
            return
        for pid in pids:
            if not pid.isdigit():
                continue
            fd_dir = os.path.join(proc, pid, "fd")
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if not target.startswith("/dev/dri/"):
                    continue
                path = os.path.join(proc, pid, "fdinfo", fd)
                data = read_bytes(path, 1 << 14)
                info = parse_fdinfo(data) if data else None
                if info and info.get("pdev") == self.pdev and "client" in info:
                    paths.setdefault((int(pid), info["client"]), path)
        self.paths = paths

    def sample(self, now: float) -> tuple[dict[str, float], dict[int, float], dict]:
        """({engine: busy %}, {pid: busy % of its busiest engine}, {engine: capacity})."""
        if now - self._scan_t >= RESCAN_S:
            self.rescan(now)
        engines: dict[str, float] = {}
        per_pid: dict[int, float] = {}
        capacity: dict[str, int] = {}
        cur: dict[tuple[int, int], tuple[float, dict]] = {}
        gone = []
        for key, path in self.paths.items():
            data = read_bytes(path, 1 << 14)
            info = parse_fdinfo(data) if data else None
            if info is None or info.get("client") != key[1]:
                gone.append(key)
                continue
            cur[key] = (now, info)
            for e, c in info["capacity"].items():
                capacity[e] = c
            prev = self._prev.get(key)
            if prev is None:
                continue
            dt = now - prev[0]
            if dt <= 0:
                continue
            p = prev[1]
            best = 0.0
            for e, ns in info["engines"].items():
                d = ns - p["engines"].get(e, ns)
                if d < 0:
                    continue
                pct = d / (dt * 1e9) * 100 / max(1, info["capacity"].get(e, 1))
                engines[e] = engines.get(e, 0.0) + pct
                best = max(best, pct)
            for e, cyc in info["cycles"].items():  # xe: busy cycles / elapsed GPU cycles
                tc, ptc = info["total_cycles"].get(e), p["total_cycles"].get(e)
                pc = p["cycles"].get(e)
                if tc is None or ptc is None or pc is None or tc <= ptc or cyc < pc:
                    continue
                pct = (cyc - pc) / (tc - ptc) * 100 / max(1, info["capacity"].get(e, 1))
                engines[e] = engines.get(e, 0.0) + pct
                best = max(best, pct)
            per_pid[key[0]] = per_pid.get(key[0], 0.0) + best
        for key in gone:
            self.paths.pop(key, None)
        self._prev = cur
        return ({e: min(100.0, v) for e, v in engines.items()},
                {p: min(100.0, v) for p, v in per_pid.items()}, capacity)


def find_intel_card(root: str) -> tuple[str, str, str] | None:
    """(card dir, PCI address, driver) of the first i915/xe DRM card."""
    base = join(root, "sys/class/drm")
    for card in listdir(base):
        if not card.startswith("card") or "-" in card:
            continue
        d = os.path.join(base, card)
        drv = os.path.basename(os.path.realpath(os.path.join(d, "device/driver"))) \
            if root == "/" else read_str(os.path.join(d, "device/driver_name"))
        if drv in ("i915", "xe"):
            addr = os.path.basename(os.path.realpath(os.path.join(d, "device"))) \
                if root == "/" else read_str(os.path.join(d, "device/address"))
            return d, addr or "", drv
    return None


ENGINE_NAMES = {
    "render": "Render/3D", "rcs": "Render/3D", "gfx": "3D / GFX",
    "copy": "Blitter", "bcs": "Blitter", "sdma": "DMA",
    "video": "Video", "vcs": "Video", "dec": "Video decode", "enc": "Video encode",
    "video-enhance": "Video enhance", "vecs": "Video enhance", "jpeg": "JPEG",
    "compute": "Compute", "ccs": "Compute",
}


class IntelGpu:
    def __init__(self, root: str = "/") -> None:
        self.root = root
        found = find_intel_card(root)
        self.card, self.pdev, self.driver = found if found else (None, None, None)
        self.clients = DrmClients(root, self.pdev) if self.pdev else None
        self._rc6_prev: tuple[float, int] | None = None
        self.name = None
        if self.card:
            from .pci import pci_name
            dev = os.path.join(self.card, "device")
            did = read_str(os.path.join(dev, "device"))
            label = pci_name("8086", did) if did else None
            self.name = f"Intel {label}" if label else "Intel integrated graphics"
        self.per_pid: dict[int, float] = {}

    def _freq(self, name: str) -> int | None:
        """i915: cardN/gt_<name>_freq_mhz; xe: device/tile0/gt0/freq0/<name>_freq."""
        v = read_int(os.path.join(self.card, f"gt_{name}_freq_mhz"))
        if v is None:
            xe = {"act": "act", "cur": "cur", "max": "max", "min": "min", "RP0": "rp0",
                  "boost": "max"}.get(name, name)
            v = read_int(os.path.join(self.card, f"device/tile0/gt0/freq0/{xe}_freq"))
        return v

    def _rc6_ms(self) -> int | None:
        v = read_int(os.path.join(self.card, "gt/gt0/rc6_residency_ms"))
        if v is None:
            v = read_int(os.path.join(self.card, "power/rc6_residency_ms"))
        if v is None:
            v = read_int(os.path.join(self.card, "device/tile0/gt0/gtidle/idle_residency_ms"))
        return v

    def sample(self, now: float, vals: dict, info: dict) -> None:
        if not self.card:
            info["igpu"] = {"present": False, "reason": "no i915/xe DRM device"}
            return
        out = {"present": True, "name": self.name, "driver": self.driver, "pdev": self.pdev}
        act, cur = self._freq("act"), self._freq("cur")
        vals["igpu.freq"] = act
        vals["igpu.freq_req"] = cur
        out["max_mhz"] = self._freq("max")
        out["min_mhz"] = self._freq("min")
        out["rp0_mhz"] = self._freq("RP0")
        out["boost_mhz"] = self._freq("boost")
        rc6 = self._rc6_ms()
        if rc6 is not None:
            prev, self._rc6_prev = self._rc6_prev, (now, rc6)
            if prev is not None and now > prev[0] and rc6 >= prev[1]:
                pct = min(100.0, (rc6 - prev[1]) / ((now - prev[0]) * 10.0))
                vals["igpu.rc6"] = pct
                vals["igpu.awake"] = 100.0 - pct
        if self.clients is not None:
            engines, per_pid, cap = self.clients.sample(now)
            self.per_pid = per_pid
            for e, v in engines.items():
                vals[f"igpu.engine.{e}"] = v
            if engines:
                vals["igpu.util"] = max(engines.values())
            elif self.clients.paths:
                vals["igpu.util"] = None  # first tick: no delta yet
            out["engines"] = sorted(set(engines) | set(cap))
            out["clients"] = len(self.clients.paths)
            out["per_pid"] = per_pid
        thr = read_str(os.path.join(self.card, "gt/gt0/throttle_reason_status"))
        out["throttled"] = thr == "1" if thr is not None else None
        info["igpu"] = out
