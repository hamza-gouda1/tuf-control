"""Inputs for the rule engine, all cheap to sample every couple of seconds.

- Power: /sys/class/power_supply (Mains `online`, Battery `capacity`/`status`). The daemon
  additionally listens to UPower's PropertiesChanged on the system bus, only to wake up
  early; the values still come from sysfs.
- Processes: one listdir of /proc per scan; new PIDs are read once (comm, exe, argv[0],
  and, for our own processes, GIO_LAUNCHED_DESKTOP_FILE which GNOME sets for apps it
  launches). Known PIDs cost nothing.
- Clock and platform profile.

Focused-window tracking is not available to normal clients on GNOME Wayland
(org.gnome.Shell.Introspect is restricted to allow-listed callers), so app rules match
running processes, not focus. See docs/AUTOMATION.md.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..core import TufControlError
from .engine import State


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def read_power(root: str | Path = "/") -> tuple[bool | None, int | None]:
    """(on_ac, battery_percent); None where unknown."""
    base = Path(root) / "sys/class/power_supply"
    on_ac: bool | None = None
    pct: int | None = None
    status: str | None = None
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return None, None
    for d in entries:
        kind = _read(d / "type")
        if kind == "Mains" or (kind is None and d.name.startswith(("AC", "ADP", "ACAD"))):
            online = _read(d / "online")
            if online in ("0", "1"):
                on_ac = bool(on_ac) or online == "1"
        elif kind in ("Battery", None) and d.name.startswith("BAT"):
            if pct is None:
                cap = _read(d / "capacity")
                pct = int(cap) if cap and cap.isdigit() else None
                status = _read(d / "status")
    if on_ac is None and status:
        on_ac = status != "Discharging"
    return on_ac, pct


class ProcessScanner:
    """Names of running programs (lower-case): comm, exe basename, argv[0] basename
    (Windows paths under Wine/Proton included) and desktop ids."""

    def __init__(self, proc: str | Path = "/proc", uid: int | None = None) -> None:
        self.proc = Path(proc)
        self.uid = os.getuid() if uid is None else uid
        self._cache: dict[str, frozenset[str]] = {}

    def _names(self, d: Path) -> frozenset[str]:
        names: set[str] = set()
        comm = _read(d / "comm")
        if comm:
            names.add(comm.lower())
        try:
            names.add(os.path.basename(os.readlink(d / "exe")).lower())
        except OSError:
            pass
        try:
            raw = (d / "cmdline").read_bytes()[:4096]
        except OSError:
            raw = b""
        if raw:
            argv0 = raw.split(b"\0", 1)[0].decode(errors="replace")
            base = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base:
                names.add(base)
        try:
            own = d.stat().st_uid == self.uid
        except OSError:
            own = False
        if own:
            try:
                env = (d / "environ").read_bytes()
            except OSError:
                env = b""
            key = b"GIO_LAUNCHED_DESKTOP_FILE="
            for item in env.split(b"\0"):
                if item.startswith(key):
                    desk = os.path.basename(item[len(key):].decode(errors="replace")).lower()
                    if desk.endswith(".desktop"):
                        names.add(desk[:-8])
                    break
        names.discard("")
        return frozenset(names)

    def scan(self) -> frozenset[str]:
        try:
            pids = [p for p in os.listdir(self.proc) if p.isdigit()]
        except OSError:
            return frozenset()
        alive = set(pids)
        for gone in [p for p in self._cache if p not in alive]:
            del self._cache[gone]
        out: set[str] = set()
        for pid in pids:
            names = self._cache.get(pid)
            if names is None:
                names = self._names(self.proc / pid)
                if names:  # an unreadable, already-exited pid is retried next time
                    self._cache[pid] = names
            out |= names
        return frozenset(out)


class StateCollector:
    def __init__(self, ctl=None, *, root: str | Path | None = None,
                 scanner: ProcessScanner | None = None,
                 wall: Callable[[], datetime] = datetime.now,
                 power: Callable[[], tuple[bool | None, int | None]] | None = None) -> None:
        from ..controller import FAKE_ROOT_ENV
        self.ctl = ctl
        self.root = root if root is not None else (os.environ.get(FAKE_ROOT_ENV) or "/")
        self.scanner = scanner or ProcessScanner()
        self.wall = wall
        self.power = power or (lambda: read_power(self.root))
        self.needs_apps = True
        self.needs_profile = True

    def collect(self) -> State:
        on_ac, pct = self.power()
        prof = None
        if self.ctl is not None and self.needs_profile:
            try:
                prof = self.ctl.get_profile()[0].value
            except TufControlError:
                prof = None
        apps = self.scanner.scan() if self.needs_apps else frozenset()
        return State(self.wall(), on_ac, pct, apps, prof)
