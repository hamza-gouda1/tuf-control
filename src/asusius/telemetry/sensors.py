# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""hwmon temperatures and fans, and CPU package power from RAPL when readable."""
from __future__ import annotations

import os
import re

from .util import join, listdir, read_int, read_str

RAPL_ROOT_ONLY = ("needs root: the kernel makes the RAPL energy counter root-only "
                  "(since 5.10, against the PLATYPUS power side channel)")
REDISCOVER_S = 60.0
SLOW_EVERY_S = 5.0  # extra NVMe sensors (an admin command each), DIMM (I2C), ACPI


class HwmonMap:
    """name -> list of hwmon dirs, rebuilt rarely (numbers are stable while booted)."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.by_name: dict[str, list[str]] = {}
        self.t = -1e9

    def refresh(self, now: float) -> None:
        if now - self.t < REDISCOVER_S:
            return
        self.t = now
        base = join(self.root, "sys/class/hwmon")
        out: dict[str, list[str]] = {}
        for d in listdir(base):
            p = os.path.join(base, d)
            name = read_str(os.path.join(p, "name"))
            if name:
                out.setdefault(name, []).append(p)
        self.by_name = out

    def first(self, name: str) -> str | None:
        lst = self.by_name.get(name)
        return lst[0] if lst else None


def channels(hw: str, kind: str) -> list[tuple[str, str, str | None]]:
    """[(channel, input path, label)] for kind 'temp' or 'fan', in numeric order."""
    out = []
    pat = re.compile(rf"{kind}(\d+)_input$")
    for f in listdir(hw):
        m = pat.match(f)
        if m:
            n = m.group(1)
            out.append((n, os.path.join(hw, f),
                        read_str(os.path.join(hw, f"{kind}{n}_label"))))
    out.sort(key=lambda x: int(x[0]))
    return out


class Sensors:
    def __init__(self, root: str = "/") -> None:
        self.root = root
        self.hw = HwmonMap(root)
        self._layout_t = -1e9
        self.core_paths: dict[int, str] = {}  # core_id -> temp input
        self.pkg_path: str | None = None
        self.fan_paths: list[tuple[str, str]] = []  # (label, path)
        self.nvme: dict[str, list[tuple[str, str]]] = {}  # disk -> [(label, path)]
        self.extra: list[tuple[str, str, str]] = []  # (key, label, path): acpitz, dimm
        self._rapl_prev: tuple[float, int] | None = None
        self._slow_t = -1e9
        self._slow: dict[str, float | None] = {}
        rapl = join(root, "sys/class/powercap/intel-rapl:0")
        self.rapl_path = os.path.join(rapl, "energy_uj")
        self.rapl_range = read_int(os.path.join(rapl, "max_energy_range_uj"))
        self.rapl_reason: str | None = None
        if not os.path.exists(self.rapl_path):
            self.rapl_reason = "not available: no intel-rapl powercap zone"
        elif not os.access(self.rapl_path, os.R_OK):
            self.rapl_reason = RAPL_ROOT_ONLY

    def _layout(self, now: float) -> None:
        if now - self._layout_t < REDISCOVER_S:
            return
        self._layout_t = now
        self.hw.refresh(now)
        self.core_paths, self.pkg_path = {}, None
        for name in ("coretemp", "k10temp", "zenpower"):
            hw = self.hw.first(name)
            if not hw:
                continue
            tctl_fallback: str | None = None
            for _n, path, lab in channels(hw, "temp"):
                lab = lab or ""
                if lab.startswith("Package id") or lab == "Tdie":
                    self.pkg_path = self.pkg_path or path
                elif lab == "Tctl":
                    tctl_fallback = tctl_fallback or path
                elif lab.startswith("Core "):
                    try:
                        self.core_paths[int(lab.split()[1])] = path
                    except ValueError:
                        pass
            if self.pkg_path is None:
                if tctl_fallback is not None:
                    self.pkg_path = tctl_fallback
                else:
                    ch = channels(hw, "temp")
                    self.pkg_path = ch[0][1] if ch else None
            break
        self.fan_paths = []
        for name in ("asus", "asus_nb_wmi", "asus-ec-sensors", "thinkpad", "dell_smm", "acpi_fan"):
            hw = self.hw.first(name)
            if not hw:
                continue
            for n, path, lab in channels(hw, "fan"):
                self.fan_paths.append((lab or f"fan{n}", path))
            if self.fan_paths:
                break
        self.nvme = {}
        for hw in self.hw.by_name.get("nvme", []):
            dev = os.path.basename(os.path.realpath(os.path.join(hw, "device")))
            disk = None
            ndir = os.path.join(os.path.realpath(os.path.join(hw, "device")))
            for d in listdir(ndir):  # nvme0 -> nvme0n1
                if re.fullmatch(r"nvme\d+n\d+", d):
                    disk = d
                    break
            disk = disk or (dev + "n1" if dev.startswith("nvme") else dev)
            self.nvme[disk] = [(lab or f"temp{n}", path) for n, path, lab in channels(hw, "temp")]
        self.extra = []
        hw = self.hw.first("acpitz")
        if hw:
            ch = channels(hw, "temp")
            if ch:
                self.extra.append(("acpitz", "ACPI thermal zone", ch[0][1]))
        for i, hw in enumerate(self.hw.by_name.get("spd5118", []) +
                               self.hw.by_name.get("jc42", [])):
            ch = channels(hw, "temp")
            if ch:
                self.extra.append((f"dimm{i}", f"Memory module {i + 1}", ch[0][1]))

    def fan_key(self, label: str) -> str:
        lab = label.lower()
        if "cpu" in lab:
            return "cpu"
        if "gpu" in lab:
            return "gpu"
        return re.sub(r"[^a-z0-9]+", "_", lab).strip("_") or "fan"

    def sample(self, now: float, vals: dict, info: dict) -> None:
        self._layout(now)
        out: dict = {"fans": [], "nvme": {}, "extra": []}
        if self.pkg_path:
            v = read_int(self.pkg_path)
            vals["temp.cpu"] = None if v is None else v / 1000
        for core, path in self.core_paths.items():
            v = read_int(path)
            vals[f"temp.core.{core}"] = None if v is None else v / 1000
        for lab, path in self.fan_paths:
            key = self.fan_key(lab)
            v = read_int(path)
            vals[f"fan.{key}"] = v
            out["fans"].append((key, lab))
        # slow sensors: read every SLOW_EVERY_S, the last reading is held in between
        fresh = now - self._slow_t >= SLOW_EVERY_S
        if fresh:
            self._slow_t = now
        for disk, chans in self.nvme.items():
            labels = []
            for i, (lab, path) in enumerate(chans):
                key = f"temp.disk.{disk}.{i}"
                if fresh or i == 0:  # the composite reading every tick, the rest held
                    v = read_int(path)
                    self._slow[key] = None if v is None else v / 1000
                vals[key] = self._slow.get(key)
                labels.append(lab)
            out["nvme"][disk] = labels
        for key, lab, path in self.extra:
            k = f"temp.{key}"
            if fresh:
                v = read_int(path)
                self._slow[k] = None if v is None else v / 1000
            vals[k] = self._slow.get(k)
            out["extra"].append((key, lab))
        out["has_core_temps"] = bool(self.core_paths)
        # RAPL package power
        if self.rapl_reason is None:
            e = read_int(self.rapl_path)
            if e is None:
                self.rapl_reason = RAPL_ROOT_ONLY
            else:
                prev, self._rapl_prev = self._rapl_prev, (now, e)
                if prev is not None and now > prev[0]:
                    de = e - prev[1]
                    if de < 0 and self.rapl_range:
                        de += self.rapl_range  # counter wrapped
                    if de >= 0:
                        vals["power.cpu"] = de / 1e6 / (now - prev[0])
        out["rapl_reason"] = self.rapl_reason
        info["sensors"] = out
