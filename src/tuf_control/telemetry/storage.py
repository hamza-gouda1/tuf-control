"""Block devices: /proc/diskstats rates and busy time; mounted filesystems via statvfs."""
from __future__ import annotations

import os

from .util import join, listdir, read_bytes, read_int, read_str

SECTOR = 512  # /proc/diskstats always counts 512-byte sectors, whatever the device uses
MOUNTS_EVERY_S = 10.0
SKIP_FS = {"squashfs", "overlay", "tmpfs", "devtmpfs", "ramfs", "iso9660"}


def parse_diskstats(data: bytes) -> dict[str, tuple[int, ...]]:
    """{name: (reads, sectors_read, writes, sectors_written, io_ms)}."""
    out = {}
    for line in data.split(b"\n"):
        p = line.split()
        if len(p) < 14:
            continue
        out[p[2].decode()] = (int(p[3]), int(p[5]), int(p[7]), int(p[9]), int(p[12]))
    return out


def disk_rates(cur: tuple[int, ...], prev: tuple[int, ...], dt: float) -> dict | None:
    if dt <= 0 or any(c < p for c, p in zip(cur, prev)):
        return None
    return {
        "read": (cur[1] - prev[1]) * SECTOR / dt,
        "write": (cur[3] - prev[3]) * SECTOR / dt,
        "busy": min(100.0, (cur[4] - prev[4]) / (dt * 10.0)),  # ms busy per s -> %
        "riops": (cur[0] - prev[0]) / dt,
        "wiops": (cur[2] - prev[2]) / dt,
    }


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    """[(source, mountpoint, fstype)] for real block-device filesystems, first mount wins."""
    seen = set()
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) < 3 or not p[0].startswith("/dev/") or p[2] in SKIP_FS:
            continue
        if p[0].startswith("/dev/loop") or p[0] in seen:
            continue
        seen.add(p[0])
        mnt = p[1].replace("\\040", " ").replace("\\011", "\t")
        out.append((p[0], mnt, p[2]))
    return out


class Storage:
    def __init__(self, root: str = "/") -> None:
        self.root = root
        self._prev: tuple[float, dict] | None = None
        self.want_mounts = False
        self._mounts: list[dict] = []
        self._mounts_t = -1e9
        self.disks = self._disks()

    def _disks(self) -> dict[str, dict]:
        """Physical disks only: /sys/block entries backed by a device (no loop, dm, zram)."""
        base = join(self.root, "sys/block")
        out = {}
        for name in listdir(base):
            d = os.path.join(base, name)
            if not os.path.exists(os.path.join(d, "device")):
                continue
            model = read_str(os.path.join(d, "device/model")) or \
                read_str(os.path.join(d, "device/name"))
            size = read_int(os.path.join(d, "size"))
            rot = read_str(os.path.join(d, "queue/rotational"))
            kind = "NVMe SSD" if name.startswith("nvme") else (
                "HDD" if rot == "1" else ("SSD" if rot == "0" else "Disk"))
            if read_str(os.path.join(d, "removable")) == "1":
                kind = "Removable"
            out[name] = {"model": model, "size": None if size is None else size * SECTOR,
                         "kind": kind}
        return out

    def _parent_disk(self, dev: str) -> str | None:
        name = os.path.basename(os.path.realpath(dev)) if self.root == "/" else \
            os.path.basename(dev)
        if name in self.disks:
            return name
        cb = join(self.root, f"sys/class/block/{name}")
        if os.path.exists(os.path.join(cb, "partition")):
            parent = os.path.basename(os.path.dirname(os.path.realpath(cb)))
            if parent in self.disks:
                return parent
        for s in listdir(os.path.join(cb, "slaves")):  # dm-crypt, LVM
            p = self._parent_disk("/dev/" + s)
            if p:
                return p
        for d in self.disks:
            if name.startswith(d):
                return d
        return None

    def mounts(self, now: float) -> list[dict]:
        if now - self._mounts_t < MOUNTS_EVERY_S:
            return self._mounts
        self._mounts_t = now
        fake = join(self.root, "fake/mounts.json")
        if self.root != "/" and os.path.exists(fake):
            import json
            with open(fake) as fh:
                self._mounts = json.load(fh)
            return self._mounts
        text = read_str(join(self.root, "proc/self/mounts")) or ""
        out = []
        for src, mnt, fs in parse_mounts(text):
            path = mnt if self.root == "/" else join(self.root, mnt)
            try:
                st = os.statvfs(path)
            except OSError:
                continue
            size = st.f_blocks * st.f_frsize
            if size <= 0:
                continue
            avail = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            out.append({"source": src, "mount": mnt, "fstype": fs, "size": size,
                        "used": used, "avail": avail, "disk": self._parent_disk(src)})
        self._mounts = out
        return out

    def sample(self, now: float, vals: dict, info: dict) -> None:
        data = read_bytes(join(self.root, "proc/diskstats"))
        if data is None:
            info["disk"] = {"error": "/proc/diskstats unreadable"}
            return
        stats = parse_diskstats(data)
        prev, self._prev = self._prev, (now, stats)
        tot_r = tot_w = 0.0
        have = False
        totals = {}
        for name in self.disks:
            cur = stats.get(name)
            if cur is None:
                continue
            totals[name] = (cur[1] * SECTOR, cur[3] * SECTOR)
            if prev is None or name not in prev[1]:
                continue
            r = disk_rates(cur, prev[1][name], now - prev[0])
            if r is None:
                continue
            have = True
            vals[f"disk.{name}.read"] = r["read"]
            vals[f"disk.{name}.write"] = r["write"]
            vals[f"disk.{name}.busy"] = r["busy"]
            tot_r += r["read"]
            tot_w += r["write"]
        if have:
            vals["disk.read"], vals["disk.write"] = tot_r, tot_w
        out = {"disks": self.disks, "totals": totals}
        if self.want_mounts:
            out["mounts"] = self.mounts(now)
        info["disk"] = out
