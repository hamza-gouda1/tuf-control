# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Network interfaces: /proc/net/dev byte counters, link type and state."""
from __future__ import annotations

import os

from .util import join, read_bytes, read_int, read_str

ARPHRD = {1: "Ethernet", 772: "Loopback", 65534: "Tunnel", 776: "Tunnel", 768: "Tunnel",
          778: "Tunnel", 823: "Tunnel"}


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
        info["net"] = {
            "ifaces": {n: {**self.meta(n), **self.link(n), "rx_total": stats[n][0],
                           "tx_total": stats[n][2], "wifi": wl.get(n)} for n in ifaces},
        }
