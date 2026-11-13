# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""PCI helpers: device names from pci.ids and runtime power state (never wakes a device)."""
from __future__ import annotations

import os

from .util import join, listdir, read_str

PCI_IDS = ("/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids")
_names: dict[tuple[str, str], str | None] = {}


def pci_name(vendor: str, device: str) -> str | None:
    """'8086', 'a78b' -> 'Raptor Lake-S UHD Graphics' (streamed once per id, cached)."""
    vendor, device = vendor.lower().removeprefix("0x"), device.lower().removeprefix("0x")
    key = (vendor, device)
    if key in _names:
        return _names[key]
    name = None
    for path in PCI_IDS:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            in_vendor = False
            for line in fh:
                if not line or line[0] == "#":
                    continue
                if line[0] != "\t":
                    if in_vendor:
                        break
                    in_vendor = line.startswith(vendor + " ")
                elif in_vendor and line[1] != "\t" and line[1:5] == device:
                    name = line[5:].strip()
                    break
        break
    _names[key] = name
    return name


def display_devices(root: str) -> list[dict]:
    """PCI display controllers with vendor/device ids and runtime PM state."""
    base = join(root, "sys/bus/pci/devices")
    out = []
    for addr in listdir(base):
        d = os.path.join(base, addr)
        cls = read_str(os.path.join(d, "class")) or ""
        if not cls.startswith("0x03"):
            continue
        out.append({"addr": addr, "dir": d, "vendor": read_str(os.path.join(d, "vendor")),
                    "device": read_str(os.path.join(d, "device")),
                    "boot_vga": read_str(os.path.join(d, "boot_vga"))})
    return out


def runtime_state(dev_dir: str) -> tuple[str | None, str | None]:
    """(runtime_status, power_state). Reading these sysfs files does not resume the device."""
    return (read_str(os.path.join(dev_dir, "power/runtime_status")),
            read_str(os.path.join(dev_dir, "power_state")))
