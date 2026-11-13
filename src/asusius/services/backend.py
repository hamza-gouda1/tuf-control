# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""systemd over D-Bus (org.freedesktop.systemd1), and a fake for tests and screenshots.

Listing: one ListUnits call per refresh, plus ListUnitFiles (enabled state and unit file
path) every few refreshes; PID and memory come from the units' cgroups in /sys/fs/cgroup
(plain file reads). None of it needs root.

Actions: StartUnit / StopUnit / RestartUnit / EnableUnitFiles / DisableUnitFiles (+ Reload)
on the user bus for user units, on the system bus for system units with interactive
authorisation allowed, so systemd asks polkit and the desktop shows its own dialog.
The UI checks `units.refusal()` first and asks for confirmation; this module checks again.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .units import Unit, refusal

SD = "org.freedesktop.systemd1"
SD_PATH = "/org/freedesktop/systemd1"
SD_MGR = "org.freedesktop.systemd1.Manager"
FILES_EVERY = 10  # refreshes between ListUnitFiles calls
CG_ROOT = Path("/sys/fs/cgroup")
CG_SUFFIXES = (".service", ".scope", ".socket", ".mount", ".swap")


class RefusedError(PermissionError):
    pass


def _read_int(p: Path) -> int | None:
    try:
        return int(p.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _first_pid(d: Path) -> int | None:
    try:
        pids = [int(x) for x in (d / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return None
    return min(pids) if pids else None


def cgroup_map(root: Path, max_depth: int = 4) -> dict[str, Path]:
    """unit name -> its cgroup directory, by walking the tree under `root`."""
    out: dict[str, Path] = {}
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if not e.is_dir(follow_symlinks=False):
                    continue
                if e.name.endswith(CG_SUFFIXES):
                    out.setdefault(e.name, Path(e.path))
                if depth < max_depth and (e.name.endswith((".slice", ".service")) or
                                          depth == 0):
                    stack.append((Path(e.path), depth + 1))
    return out


def cgroup_root(scope: str, uid: int | None = None) -> Path:
    if scope == "user":
        uid = os.getuid() if uid is None else uid
        return CG_ROOT / f"user.slice/user-{uid}.slice/user@{uid}.service"
    return CG_ROOT


class SystemdBackend:
    def __init__(self, scope: str = "system", cg_root: Path | None = None) -> None:
        self.scope = scope
        self.kind = "system" if scope == "system" else "session"
        self.cg_root = cg_root or cgroup_root(scope)
        self._files: dict[str, tuple[str, str]] = {}
        self._n = 0
        self._conn = None

    def _call(self, method: str, args=None, reply: str | None = None, interactive=False,
              timeout_ms: int = 10000, path: str = SD_PATH, iface: str = SD_MGR):
        from .. import dbusutil
        if self._conn is None:
            self._conn = dbusutil.bus(self.kind)
        return dbusutil.call(self.kind, SD, path, iface, method, args, reply, timeout_ms,
                             interactive=interactive, conn=self._conn)

    def _unit_files(self) -> dict[str, tuple[str, str]]:
        (rows,) = self._call("ListUnitFiles", None, "(a(ss))", timeout_ms=20000)
        return {os.path.basename(p): (st, p) for p, st in rows}

    def list_units(self, force_files: bool = False) -> list[Unit]:
        (rows,) = self._call("ListUnits", None, "(a(ssssssouso))")
        if force_files or not self._files or self._n % FILES_EVERY == 0:
            try:
                self._files = self._unit_files()
            except Exception:
                pass
        self._n += 1
        cg = cgroup_map(self.cg_root)
        out = []
        for name, desc, load, active, sub, _follow, opath, *_ in rows:
            st, fpath = self._files.get(name) or self._files.get(_template(name)) or ("", "")
            u = Unit(name=name, description=desc, load=load, active=active, sub=sub,
                     file_state=st, path=fpath, object_path=opath, scope=self.scope)
            d = cg.get(name)
            if d is not None and active == "active":
                u.memory = _read_int(d / "memory.current")
                u.pid = _first_pid(d)
            out.append(u)
        return out

    def details(self, unit: Unit) -> dict:
        """Main PID, unit file and timing for the selected unit (a few property reads)."""
        from gi.repository import GLib
        out = {}
        try:
            (props,) = self._call("GetAll", GLib.Variant("(s)", ("org.freedesktop.systemd1.Unit",)),
                                  "(a{sv})", path=unit.object_path,
                                  iface="org.freedesktop.DBus.Properties")
            out["fragment"] = props.get("FragmentPath") or unit.path
            out["since_us"] = props.get("ActiveEnterTimestamp") or 0
            out["triggered_by"] = list(props.get("TriggeredBy") or [])
        except Exception:
            pass
        if unit.name.endswith(".service"):
            try:
                (v,) = self._call("Get", GLib.Variant("(ss)", ("org.freedesktop.systemd1.Service",
                                                              "MainPID")),
                                  "(v)", path=unit.object_path,
                                  iface="org.freedesktop.DBus.Properties")
                out["main_pid"] = int(v) or None
            except Exception:
                pass
        return out

    def act(self, unit: Unit, op: str) -> str:
        why = refusal(unit, op)
        if why:
            raise RefusedError(f"{unit.name}: {why}")
        from gi.repository import GLib
        interactive = self.scope == "system"
        if op in ("start", "stop", "restart"):
            method = {"start": "StartUnit", "stop": "StopUnit", "restart": "RestartUnit"}[op]
            self._call(method, GLib.Variant("(ss)", (unit.name, "replace")), "(o)",
                       interactive=interactive, timeout_ms=120000)
        elif op == "enable":
            self._call("EnableUnitFiles", GLib.Variant("(asbb)", ([unit.name], False, False)),
                       "(ba(sss))", interactive=interactive, timeout_ms=120000)
            self._call("Reload", None, None, interactive=interactive, timeout_ms=120000)
        elif op == "disable":
            self._call("DisableUnitFiles", GLib.Variant("(asb)", ([unit.name], False)),
                       "(a(sss))", interactive=interactive, timeout_ms=120000)
            self._call("Reload", None, None, interactive=interactive, timeout_ms=120000)
        self._files = {}  # re-read enabled states on the next refresh
        return f"{op} {unit.name}"


def _template(name: str) -> str:
    from .units import template_of
    return template_of(name)


# -- fake ---------------------------------------------------------------------------------------
_FAKE = {
    "system": [
        ("NetworkManager.service", "Network Manager", "active", "running", "enabled", 1243,
         10_625_024),
        ("asusd.service", "ASUS Notebook Control", "active", "running", "enabled", 1408,
         6_291_456),
        ("bluetooth.service", "Bluetooth service", "active", "running", "enabled", 1201,
         3_145_728),
        ("cups.service", "CUPS Scheduler", "active", "running", "enabled", 1650, 9_437_184),
        ("docker.service", "Docker Application Container Engine", "inactive", "dead",
         "disabled", None, None),
        ("fwupd.service", "Firmware update daemon", "active", "running", "static", 2210,
         41_943_040),
        ("gdm.service", "GNOME Display Manager", "active", "running", "static", 1525,
         5_242_880),
        ("nvidia-persistenced.service", "NVIDIA Persistence Daemon", "active", "running",
         "enabled", 1302, 1_048_576),
        ("packagekit.service", "PackageKit Daemon", "active", "running", "static", 3001,
         88_080_384),
        ("polkit.service", "Authorization Manager", "active", "running", "static", 1187,
         8_388_608),
        ("power-profiles-daemon.service", "Power Profiles daemon", "active", "running",
         "enabled", 1190, 2_097_152),
        ("snapd.service", "Snap Daemon", "active", "running", "enabled", 1222, 37_748_736),
        ("ssh.service", "OpenBSD Secure Shell server", "inactive", "dead", "disabled", None,
         None),
        ("supergfxd.service", "SUPERGFXD", "active", "running", "enabled", 1410, 2_621_440),
        ("systemd-journald.service", "Journal Service", "active", "running", "static", 412,
         26_214_400),
        ("systemd-logind.service", "User Login Management", "active", "running", "static",
         1199, 4_194_304),
        ("thermald.service", "Thermal Daemon Service", "failed", "failed", "enabled", None,
         None),
        ("ufw.service", "Uncomplicated firewall", "active", "exited", "enabled", None, None),
        ("apt-daily.timer", "Daily apt download activities", "active", "waiting", "enabled",
         None, None),
    ],
    "user": [
        ("asusiusd.service", "Asusius automation and keyboard effects", "active",
         "running", "enabled", 4120, 31_457_280),
        ("pipewire.service", "PipeWire Multimedia Service", "active", "running", "enabled",
         3311, 12_582_912),
        ("wireplumber.service", "Multimedia Service Session Manager", "active", "running",
         "enabled", 3320, 15_728_640),
        ("syncthing.service", "Syncthing - Open Source Continuous File Synchronization",
         "inactive", "dead", "disabled", None, None),
        ("org.gnome.Shell@wayland.service", "GNOME Shell on Wayland", "active", "running",
         "static", 3600, 412_090_368),
        ("xdg-desktop-portal.service", "Portal service", "active", "running", "static", 3700,
         9_437_184),
    ],
}


class FakeServices:
    """In-memory units; actions change the fake state and are logged in `self.log`."""

    def __init__(self, scope: str = "system") -> None:
        self.scope = scope
        self.state = {n: [n, d, "loaded", a, s, f, p, m]
                      for n, d, a, s, f, p, m in _FAKE["system" if scope == "system" else "user"]}
        self.log: list[str] = []

    def list_units(self, force_files: bool = False) -> list[Unit]:
        out = []
        for n, d, load, a, s, f, p, m in self.state.values():
            out.append(Unit(name=n, description=d, load=load, active=a, sub=s, file_state=f,
                            pid=p, memory=m, scope=self.scope,
                            path=f"/usr/lib/systemd/{'system' if self.scope == 'system' else 'user'}/{n}",
                            object_path=f"/org/freedesktop/systemd1/unit/{n.replace('.', '_2e')}"))
        return out

    def details(self, unit: Unit) -> dict:
        return {"fragment": unit.path, "main_pid": unit.pid,
                "since_us": int((time.time() - 3 * 3600) * 1e6) if unit.active == "active"
                else 0}

    def act(self, unit: Unit, op: str) -> str:
        why = refusal(unit, op)
        if why:
            raise RefusedError(f"{unit.name}: {why}")
        row = self.state[unit.name]
        if op in ("start", "restart"):
            row[3], row[4], row[6], row[7] = "active", "running", 9000 + len(self.log), 4 << 20
        elif op == "stop":
            row[3], row[4], row[6], row[7] = "inactive", "dead", None, None
        elif op == "enable":
            row[5] = "enabled"
        elif op == "disable":
            row[5] = "disabled"
        self.log.append(f"{op} {unit.name}")
        return f"{op} {unit.name} (fake)"


def make_backend(scope: str):
    from ..controller import FAKE_ROOT_ENV
    if os.environ.get(FAKE_ROOT_ENV):
        return FakeServices(scope)
    return SystemdBackend(scope)
