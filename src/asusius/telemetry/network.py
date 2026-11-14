# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Network interfaces: /proc/net/dev byte counters, link type and state."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from .util import join, read_bytes, read_int, read_str

ARPHRD = {1: "Ethernet", 772: "Loopback", 65534: "Tunnel", 776: "Tunnel", 768: "Tunnel",
          778: "Tunnel", 823: "Tunnel"}
IW_EVERY_S = 5.0
_IW_SIGNAL = re.compile(r"^\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", re.MULTILINE)


def parse_net_dev(data: bytes) -> dict[str, tuple[int, int, int, int]]:
    """{iface: (rx_bytes, rx_packets, tx_bytes, tx_packets)}."""
    out = {}
    for line in data.split(b"\n")[2:]:
        name, sep, rest = line.partition(b":")
        if not sep:
            continue
        p = rest.split()
        if len(p) < 10:
            continue
        out[name.strip().decode()] = (int(p[0]), int(p[1]), int(p[8]), int(p[9]))
    return out


def parse_wireless(text: str | None) -> dict[str, tuple[float, float]]:
    """{iface: (link quality 0-70, signal dBm)} from /proc/net/wireless."""
    out = {}
    for line in (text or "").splitlines()[2:]:
        name, sep, rest = line.partition(":")
        p = rest.split()
        if sep and len(p) >= 3:
            try:
                out[name.strip()] = (float(p[1].rstrip(".")), float(p[2].rstrip(".")))
            except ValueError:
                pass
    return out


class Network:
    def __init__(self, root: str = "/") -> None:
        self.root = root
        self._prev: tuple[float, dict] | None = None
        self._meta: dict[str, dict] = {}
        self._iw_cache: dict[str, tuple[float, tuple[float, float] | None]] = {}

    def _iw_link(self, name: str, now: float) -> tuple[float, float] | None:
        cached = self._iw_cache.get(name)
        if cached and now - cached[0] < IW_EVERY_S:
            return cached[1]
        iw = shutil.which("iw")
        if self.root != "/" or iw is None:
            return None
        val: tuple[float, float] | None = None
        try:
            proc = subprocess.run([iw, "dev", name, "link"], capture_output=True, text=True,
                                  timeout=0.5, check=False, stdin=subprocess.DEVNULL)
            if proc.returncode == 0:
                m = _IW_SIGNAL.search(proc.stdout)
                if m:
                    dbm = float(m.group(1))
                    qual = round(max(0.0, min(70.0, (dbm + 100.0) * 70.0 / 60.0)), 1)
                    val = (qual, dbm)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        self._iw_cache[name] = (now, val)
        return val

    def meta(self, name: str) -> dict:
        m = self._meta.get(name)
        if m is None:
            d = join(self.root, f"sys/class/net/{name}")
            real = os.path.realpath(d) if self.root == "/" else d
            virtual = "/virtual/" in real or not os.path.exists(os.path.join(d, "device"))
            if os.path.exists(os.path.join(d, "wireless")) or \
                    os.path.exists(os.path.join(d, "phy80211")):
                kind = "Wi-Fi"
            else:
                kind = ARPHRD.get(read_int(os.path.join(d, "type")) or 0, "Network")
                if virtual and kind == "Ethernet":
                    kind = "Virtual"
            m = self._meta[name] = {"virtual": virtual or name == "lo", "kind": kind}
        return m

    def link(self, name: str) -> dict:
        d = join(self.root, f"sys/class/net/{name}")
        speed = read_int(os.path.join(d, "speed"))
        return {"state": read_str(os.path.join(d, "operstate")),
                "speed_mbps": speed if speed and speed > 0 else None}

    def sample(self, now: float, vals: dict, info: dict) -> None:
        data = read_bytes(join(self.root, "proc/net/dev"))
        if data is None:
            info["net"] = {"error": "/proc/net/dev unreadable"}
            return
        stats = parse_net_dev(data)
        self._meta = {k: v for k, v in self._meta.items() if k in stats}
        self._iw_cache = {k: v for k, v in self._iw_cache.items() if k in stats}
        prev, self._prev = self._prev, (now, stats)
        ifaces = []
        rx_t = tx_t = 0.0
        have = False
        for name, cur in stats.items():
            m = self.meta(name)
            if name == "lo":
                continue
            ifaces.append(name)
            if prev is None or name not in prev[1]:
                continue
            p = prev[1][name]
            dt = now - prev[0]
            if dt <= 0 or cur[0] < p[0] or cur[2] < p[2]:
                continue
            rx, tx = (cur[0] - p[0]) / dt, (cur[2] - p[2]) / dt
            vals[f"net.{name}.rx"], vals[f"net.{name}.tx"] = rx, tx
            if not m["virtual"]:
                rx_t += rx
                tx_t += tx
                have = True
        if have:
            vals["net.rx"], vals["net.tx"] = rx_t, tx_t
        wl = parse_wireless(read_str(join(self.root, "proc/net/wireless")))
        ifaces_info = {}
        for n in ifaces:
            m = self.meta(n)
            lk = self.link(n)
            wifi = wl.get(n)
            if wifi is None and m["kind"] == "Wi-Fi" and lk.get("state") == "up":
                wifi = self._iw_link(n, now)
            ifaces_info[n] = {**m, **lk, "rx_total": stats[n][0],
                              "tx_total": stats[n][2], "wifi": wifi}
        info["net"] = {"ifaces": ifaces_info}
