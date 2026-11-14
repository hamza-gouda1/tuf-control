# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""AMD GPU telemetry (amdgpu): integrated APUs and discrete Radeon GPUs via sysfs + drm fdinfo."""
from __future__ import annotations

import os
import re

from .gpu_intel import DrmClients
from .pci import pci_name, runtime_state
from .sensors import channels
from .util import join, listdir, read_int, read_str

_DPM_MHZ = re.compile(r"^\d+:\s*(\d+)\s*MHz", re.IGNORECASE)


def _parse_dpm_active(text: str | None) -> tuple[int | None, int | None, int | None]:
    """Return (active_mhz, min_mhz, max_mhz) from pp_dpm_sclk / pp_dpm_mclk."""
    if not text:
        return None, None, None
    freqs: list[int] = []
    active: int | None = None
    for line in text.splitlines():
        m = _DPM_MHZ.match(line.strip())
        if not m:
            continue
        val = int(m.group(1))
        freqs.append(val)
        if "*" in line:
            active = val
    return active, (min(freqs) if freqs else None), (max(freqs) if freqs else None)


def find_amd_cards(root: str) -> list[dict]:
    """Discover amdgpu DRM cards sorted deterministically by card name."""
    base = join(root, "sys/class/drm")
    out: list[dict] = []
    for card in sorted(listdir(base)):
        if not card.startswith("card") or "-" in card:
            continue
        cdir = os.path.join(base, card)
        dev = os.path.join(cdir, "device")
        drv = (os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
               if root == "/" else read_str(os.path.join(dev, "driver_name")))
        if drv != "amdgpu":
            continue
        addr = (os.path.basename(os.path.realpath(dev))
                if root == "/" else read_str(os.path.join(dev, "address"))) or ""
        boot_vga = read_str(os.path.join(dev, "boot_vga")) == "1"
        vram_total = read_int(os.path.join(dev, "mem_info_vram_total"))
        out.append({"card": cdir, "dev": dev, "pdev": addr, "boot_vga": boot_vga,
                    "vram_total": vram_total, "card_name": card})
    return out


class AmdGpu:
    """AMD integrated GPU reader compatible with IntelGpu."""

    def __init__(self, root: str = "/", card_info: dict | None = None, *,
                 exclude_pdev: str | None = None) -> None:
        self.root = root
        cards = [c for c in find_amd_cards(root) if not exclude_pdev or c["pdev"] != exclude_pdev]
        if card_info is None and cards:
            card_info = next((c for c in cards if c["boot_vga"]), cards[0])
        self.info = card_info
        self.card = card_info["card"] if card_info else None
        self.dev_dir = card_info["dev"] if card_info else None
        self.pdev = card_info["pdev"] if card_info else None
        self.driver = "amdgpu" if card_info else None
        self.clients = DrmClients(root, self.pdev) if self.pdev else None
        self.per_pid: dict[int, float] = {}
        self.procs: dict[int, dict] = {}
        self.backend = "sysfs" if self.card else None
        self.reason = None if self.card else "no amdgpu DRM device"
        self.static: dict = {}
        self.name: str | None = self._resolve_label()

    def _resolve_label(self) -> str | None:
        if not self.dev_dir:
            return None
        did = read_str(os.path.join(self.dev_dir, "device"))
        label = pci_name("1002", did) if did else None
        return f"AMD {label}" if label else "AMD Radeon graphics"

    @property
    def dev(self) -> dict | None:
        if not self.dev_dir:
            return None
        return {"dir": self.dev_dir, "addr": self.pdev or "", "vendor": "0x1002"}

    def release(self) -> None:
        return None

    def _hwmon_dir(self) -> str | None:
        if not self.dev_dir:
            return None
        base = os.path.join(self.dev_dir, "hwmon")
        for d in sorted(listdir(base)):
            if d.startswith("hwmon"):
                return os.path.join(base, d)
        return None

    def _read_sclk(self, hw: str | None) -> tuple[int | None, int | None, int | None]:
        act, lo, hi = _parse_dpm_active(read_str(os.path.join(self.dev_dir, "pp_dpm_sclk")))
        if act is None and hw:
            hz = read_int(os.path.join(hw, "freq1_input"))
            act = hz // 1_000_000 if hz is not None else None
        return act, lo, hi

    def _read_mclk(self, hw: str | None) -> int | None:
        act, _lo, _hi = _parse_dpm_active(read_str(os.path.join(self.dev_dir, "pp_dpm_mclk")))
        if act is None and hw:
            hz = read_int(os.path.join(hw, "freq2_input"))
            act = hz // 1_000_000 if hz is not None else None
        return act

    def _read_temp(self, hw: str | None) -> float | None:
        if not hw:
            return None
        ch = channels(hw, "temp")
        for pref in ("edge", "junction"):
            for _n, path, lab in ch:
                if (lab or "").lower() == pref:
                    raw = read_int(path)
                    if raw is not None:
                        return raw / 1000
        if ch:
            raw = read_int(ch[0][1])
            return None if raw is None else raw / 1000
        return None

    def _read_power(self, hw: str | None) -> float | None:
        if not hw:
            return None
        uw = read_int(os.path.join(hw, "power1_average"))
        if uw is None:
            uw = read_int(os.path.join(hw, "power1_input"))
        return None if uw is None else round(uw / 1e6, 2)

    def sample(self, now: float, vals: dict, info: dict) -> None:
        self._sample_igpu(now, vals, info)

    def _sample_igpu(self, now: float, vals: dict, info: dict) -> None:
        if not self.card or not self.dev_dir:
            info["igpu"] = {"present": False, "reason": "no amdgpu DRM device"}
            return
        hw = self._hwmon_dir()
        act, lo, hi = self._read_sclk(hw)
        mclk = self._read_mclk(hw)
        busy = read_int(os.path.join(self.dev_dir, "gpu_busy_percent"))
        temp = self._read_temp(hw)
        power = self._read_power(hw)
        vals["igpu.freq"] = act
        vals["igpu.freq_req"] = act
        if temp is not None:
            vals["igpu.temp"] = temp
        if power is not None:
            vals["igpu.power"] = power
        if busy is not None:
            vals["igpu.util"] = float(busy)
        out: dict = {
            "present": True, "name": self.name, "driver": "amdgpu", "pdev": self.pdev,
            "min_mhz": lo, "max_mhz": hi, "mem_mhz": mclk, "temp": temp, "power": power,
            "vram_used": read_int(os.path.join(self.dev_dir, "mem_info_vram_used")),
            "vram_total": read_int(os.path.join(self.dev_dir, "mem_info_vram_total")),
            "throttled": None,
        }
        if self.clients is not None:
            engines, per_pid, cap = self.clients.sample(now)
            self.per_pid = per_pid
            for e, v in engines.items():
                vals[f"igpu.engine.{e}"] = v
            if busy is None and engines:
                vals["igpu.util"] = max(engines.values())
            out["engines"] = sorted(set(engines) | set(cap))
            out["clients"] = len(self.clients.paths)
            out["per_pid"] = per_pid
        info["igpu"] = out

    def _sample_dgpu(self, now: float, vals: dict, info: dict, label: str) -> None:
        out: dict = {"present": self.card is not None, "backend": "sysfs"}
        if not self.card or not self.dev_dir:
            out["reason"] = self.reason
            info["dgpu"] = out
            return
        rs, ps = runtime_state(self.dev_dir)
        out.update({"runtime": rs, "power_state": ps, "addr": self.pdev, "name": label})
        if rs in ("suspended", "suspending", "resuming"):
            self.procs = {}
            out["state"] = "sleeping"
            info["dgpu"] = out
            return
        hw = self._hwmon_dir()
        gr_mhz, lo, hi = self._read_sclk(hw)
        mem_mhz = self._read_mclk(hw)
        busy = read_int(os.path.join(self.dev_dir, "gpu_busy_percent"))
        temp = self._read_temp(hw)
        power = self._read_power(hw)
        vram_used = read_int(os.path.join(self.dev_dir, "mem_info_vram_used"))
        vram_total = read_int(os.path.join(self.dev_dir, "mem_info_vram_total"))
        if not self.static:
            self.static = {"name": label, "driver_version": "amdgpu",
                           "max_gr_mhz": hi, "min_gr_mhz": lo}
        if self.clients is not None:
            engines, per_pid, _ = self.clients.sample(now)
            self.per_pid = per_pid
            self.procs = {pid: {"type": "G", "vram": None, "sm": pct}
                          for pid, pct in per_pid.items()}
            if busy is None and engines:
                busy = max(engines.values())
        util = float(busy) if busy is not None else None
        q = {"util": util, "mem_util": None, "vram_used": vram_used, "vram_total": vram_total,
             "gr_mhz": gr_mhz, "mem_mhz": mem_mhz, "temp": temp, "power": power}
        if util is not None:
            vals["dgpu.util"] = util
        if vram_used is not None:
            vals["dgpu.vram"] = vram_used
        if temp is not None:
            vals["dgpu.temp"] = temp
        if power is not None:
            vals["dgpu.power"] = power
        if gr_mhz is not None:
            vals["dgpu.gr_mhz"] = gr_mhz
        if mem_mhz is not None:
            vals["dgpu.mem_mhz"] = mem_mhz
        out["q"] = q
        out["state"] = "awake"
        out["static"] = self.static
        out["procs"] = self.procs
        info["dgpu"] = out


class AmdDiscreteGpu:
    """AMD discrete GPU reader exposing the same contract as NvidiaGpu."""

    def __init__(self, root: str = "/", card_info: dict | None = None, *,
                 exclude_pdev: str | None = None) -> None:
        if card_info is None:
            cards = [c for c in find_amd_cards(root)
                     if not exclude_pdev or c["pdev"] != exclude_pdev]
            card_info = next((c for c in cards if not c["boot_vga"]), cards[0] if cards else None)
        self._inner = AmdGpu(root, card_info=card_info, exclude_pdev=exclude_pdev)

    @property
    def dev(self) -> dict | None:
        return self._inner.dev

    @property
    def backend(self) -> str | None:
        return self._inner.backend

    @property
    def procs(self) -> dict[int, dict]:
        return self._inner.procs

    @property
    def static(self) -> dict:
        return self._inner.static

    def name(self) -> str:
        return self._inner.name or "AMD Radeon discrete GPU"

    def release(self) -> None:
        self._inner.release()

    def sample(self, now: float, vals: dict, info: dict) -> None:
        self._inner._sample_dgpu(now, vals, info, self.name())
