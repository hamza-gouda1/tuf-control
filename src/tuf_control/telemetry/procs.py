"""Process walk over /proc: CPU %, memory, disk I/O, app grouping by systemd scope.

Runs only while the Processes page is visible (every 2 s). Per process it reads
stat + statm each pass, io for processes we own, and cmdline/cgroup once per process
lifetime (cached by pid and start time). Records are reused between passes.
"""
from __future__ import annotations

import os
import re

from .util import join, read_bytes

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_APP_RE = re.compile(r"^app-(?:(?:gnome|kde|flatpak|snap|xfce|dbus-:[0-9.]+|"
                     r"[a-z0-9]+launcher)-)?(.+?)(?:-[0-9]+\.scope|@[^.]*\.service|\.service"
                     r"|\.scope|\.slice)$")


def parse_pid_stat(data: bytes) -> tuple | None:
    """(comm, state, ppid, utime, stime, threads, starttime, rss_pages) of /proc/pid/stat."""
    r = data.rfind(b")")
    if r < 0:
        return None
    comm = data[data.find(b"(") + 1:r].decode(errors="replace")
    f = data[r + 2:].split()
    if len(f) < 22:
        return None
    return (comm, f[0].decode(), int(f[1]), int(f[11]), int(f[12]), int(f[17]), int(f[19]),
            int(f[21]))


def parse_io(data: bytes) -> tuple[int, int] | None:
    """(read_bytes, write_bytes) at the storage layer from /proc/pid/io."""
    r = w = None
    for line in data.split(b"\n"):
        if line.startswith(b"read_bytes:"):
            r = int(line[11:])
        elif line.startswith(b"write_bytes:"):
            w = int(line[12:])
    return None if r is None or w is None else (r, w)


def parse_vmswap(data: bytes) -> int | None:
    """Swapped-out bytes of a process: the VmSwap line of /proc/pid/status (kB there).
    None when the line is absent (kernel threads, or a kernel without swap accounting)."""
    i = data.find(b"\nVmSwap:")
    if i < 0:
        return None
    line = data[i + 1:data.find(b"\n", i + 1)]
    try:
        return int(line.split()[1]) * 1024
    except (IndexError, ValueError):
        return None


def unescape_unit(s: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def app_from_cgroup(text: str) -> tuple[str | None, str | None]:
    """(application id, unit) from /proc/pid/cgroup, per the systemd XDG naming scheme:
    app[-<launcher>]-<ApplicationID>[-<RANDOM>].scope or ...[@<RANDOM>].service."""
    line = ""
    for ln in text.splitlines():
        if ln.startswith("0::"):
            line = ln[3:]
            break
    else:
        line = text.splitlines()[0].split(":", 2)[-1] if text.strip() else ""
    parts = [p for p in line.split("/") if p]
    unit = parts[-1] if parts else None
    for p in reversed(parts):
        if p.startswith("app-"):
            m = _APP_RE.match(p)
            if m:
                app = unescape_unit(m.group(1))
                return app, unit
        if p.startswith("snap."):
            return "snap." + p.split(".")[1], unit
    return None, unit


class Proc:
    __slots__ = ("pid", "ppid", "start", "name", "cmd", "uid", "state", "threads", "cpu",
                 "mem", "rss", "swap", "io_read", "io_write", "gpu", "vram", "app", "unit", "kthread",
                 "_ticks", "_io", "_t", "seen")

    def __init__(self, pid: int, start: int) -> None:
        self.pid, self.start = pid, start
        self.name = self.cmd = ""
        self.uid = -1
        self.state = ""
        self.ppid = self.threads = 0
        self.cpu = self.io_read = self.io_write = None
        self.mem = self.rss = 0
        self.swap = None
        self.gpu = self.vram = None
        self.app = self.unit = None
        self.kthread = False
        self._ticks = None
        self._io = None
        self._t = 0.0
        self.seen = 0


class ProcessWalker:
    def __init__(self, root: str = "/", ncpu: int | None = None) -> None:
        self.root = root
        self.proc = join(root, "proc")
        self.ncpu = ncpu or os.cpu_count() or 1
        self.table: dict[int, Proc] = {}
        self.gen = 0
        self.my_uid = os.getuid()

    def _meta(self, p: Proc, base: str, comm: str) -> None:
        raw = read_bytes(base + "/cmdline", 1 << 15) or b""
        args = raw.rstrip(b"\0").split(b"\0") if raw else []
        p.cmd = " ".join(a.decode(errors="replace") for a in args)[:512]
        name = comm
        if args:
            first = re.split(r"[/\\]", args[0].decode(errors="replace").split(" ")[0])[-1]
            if first and (first.startswith(comm) or len(comm) >= 15):
                name = first
        p.name = name
        p.kthread = not args and (p.ppid == 2 or p.pid == 2)
        try:
            p.uid = os.stat(base).st_uid
        except OSError:
            p.uid = -1
        cg = read_bytes(base + "/cgroup", 4096)
        if cg:
            p.app, p.unit = app_from_cgroup(cg.decode(errors="replace"))

    def walk(self, now: float, gpu_pct: dict[int, float] | None = None,
             vram: dict[int, dict] | None = None) -> list[Proc]:
        self.gen += 1
        gen = self.gen
        table = self.table
        proc = self.proc
        scale = 100.0 / (CLK_TCK * self.ncpu)  # ticks/s -> % of the whole CPU
        out = []
        try:
            names = os.listdir(proc)
        except OSError:
            return out
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            base = proc + "/" + name
            data = read_bytes(base + "/stat", 4096)
            if not data:
                continue
            st = parse_pid_stat(data)
            if st is None:
                continue
            comm, state, ppid, ut, stt, threads, start, rss_pages = st
            p = table.get(pid)
            if p is None or p.start != start:
                p = Proc(pid, start)
                p.ppid = ppid
                table[pid] = p
                self._meta(p, base, comm)
            p.seen = gen
            p.state, p.ppid, p.threads = state, ppid, threads
            ticks = ut + stt
            if p._ticks is not None and now > p._t:
                p.cpu = max(0.0, (ticks - p._ticks) / (now - p._t) * scale)
            dt = now - p._t
            p._ticks = ticks
            sm = read_bytes(base + "/statm", 256)
            if sm:
                f = sm.split()
                resident, shared = int(f[1]), int(f[2])
                p.rss = resident * PAGE
                p.mem = max(0, resident - shared) * PAGE
            else:
                p.rss = rss_pages * PAGE
                p.mem = p.rss
            if not p.kthread:
                stx = read_bytes(base + "/status", 4096)
                p.swap = parse_vmswap(stx) if stx else None
            if p.uid == self.my_uid:
                io = read_bytes(base + "/io", 1024)
                cur = parse_io(io) if io else None
                if cur and p._io is not None and dt > 0 and cur[0] >= p._io[0] \
                        and cur[1] >= p._io[1]:
                    p.io_read = (cur[0] - p._io[0]) / dt
                    p.io_write = (cur[1] - p._io[1]) / dt
                p._io = cur
            p._t = now
            p.gpu = gpu_pct.get(pid) if gpu_pct else None
            v = vram.get(pid) if vram else None
            if v:
                p.vram = v.get("vram")
                if v.get("sm") is not None:
                    p.gpu = max(p.gpu or 0.0, v["sm"])
            else:
                p.vram = None
            out.append(p)
        for pid in [k for k, v in table.items() if v.seen != gen]:
            del table[pid]
        return out

    def clear(self) -> None:
        self.table.clear()


def kill(pid: int, start: int, sig: int, root: str = "/") -> None:
    """Signal pid only if it is still the same process (start time matches)."""
    data = read_bytes(join(root, f"proc/{pid}/stat"), 4096)
    st = parse_pid_stat(data) if data else None
    if st is None or st[6] != start:
        raise ProcessLookupError(f"process {pid} has already exited")
    if pid <= 1 or pid == os.getpid():
        raise PermissionError("refusing to signal init or this app")
    os.kill(pid, sig)


def alive(pid: int, start: int, root: str = "/") -> bool:
    data = read_bytes(join(root, f"proc/{pid}/stat"), 4096)
    st = parse_pid_stat(data) if data else None
    return st is not None and st[6] == start and st[1] != "Z"
