# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""systemd units: listing (no root), filtering, and the safety rules for actions."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

SCOPES = ("system", "user")
OPERATIONS = ("start", "stop", "restart", "enable", "disable")
ACTION_TYPES = (".service", ".socket", ".timer", ".path")
TOGGLE_STATES = ("enabled", "disabled")  # unit file states enable/disable may change

# never stopped, restarted or disabled from here: the session, the bus, login, networking,
# the display manager, polkit itself, and this app's own units
CRITICAL = (
    "dbus.service", "dbus.socket", "dbus-broker.service", "dbus-*.service",
    "systemd-*", "polkit.service", "polkitd.service",
    "NetworkManager.service", "NetworkManager-*.service", "network-manager.service",
    "wpa_supplicant.service", "iwd.service", "ModemManager.service",
    "thermald.service",
    "display-manager.service", "gdm.service", "gdm3.service", "gdm-*.service",
    "lightdm.service", "sddm.service", "lxdm.service",
    "user@*.service", "user-runtime-dir@*.service", "getty@*.service", "autovt@*.service",
    "accounts-daemon.service", "udisks2.service", "upower.service", "init.scope",
    "org.gnome.Shell*", "gnome-session*", "org.gnome.SettingsDaemon.*",
    "xdg-desktop-portal*.service", "pipewire*.service", "pipewire*.socket",
    "wireplumber.service", "at-spi-dbus-bus.service", "gnome-keyring-daemon.*",
    "asusd.service", "supergfxd.service", "power-profiles-daemon.service",
    "asusius*", "asusiusd*", "app-io.github.hamzagouda.Asusius*",
    "apparmor.service", "ufw.service",
)
NAME_RE = re.compile(r"^[A-Za-z0-9:_.\\@-]{1,255}$")


@dataclass
class Unit:
    name: str
    description: str = ""
    load: str = ""          # loaded, not-found, masked, error
    active: str = ""        # active, inactive, failed, activating, deactivating
    sub: str = ""           # running, exited, dead, waiting, listening...
    file_state: str = ""    # enabled, disabled, static, masked, generated, indirect, alias
    pid: int | None = None
    memory: int | None = None
    path: str = ""          # unit file
    object_path: str = ""
    scope: str = "system"
    extra: dict = field(default_factory=dict)

    @property
    def type(self) -> str:
        return self.name.rsplit(".", 1)[-1] if "." in self.name else ""

    @property
    def running(self) -> bool:
        return self.active == "active" and self.sub in ("running", "exited", "listening",
                                                        "waiting", "mounted", "plugged", "active")

    def state_text(self) -> str:
        if self.active == "failed":
            return "Failed"
        if self.active == "active":
            return "Running" if self.sub == "running" else (self.sub or "active").title()
        if self.active in ("activating", "deactivating", "reloading"):
            return self.active.title()
        return "Stopped"

    def state_tone(self) -> str:
        """chip style: ok, warn, err, dim"""
        if self.active == "failed":
            return "err"
        if self.active == "active":
            return "ok"
        if self.active in ("activating", "deactivating", "reloading"):
            return "warn"
        return "dim"

    def enabled_text(self) -> str:
        return {"enabled": "Enabled", "enabled-runtime": "Enabled (runtime)",
                "disabled": "Disabled", "static": "Static", "masked": "Masked",
                "generated": "Generated", "indirect": "Indirect", "alias": "Alias",
                "linked": "Linked", "transient": "Transient"}.get(self.file_state,
                                                                  self.file_state or "–")


def template_of(name: str) -> str:
    """'getty@tty1.service' -> 'getty@.service'."""
    m = re.match(r"^([^@]+)@.*(\.[^.]+)$", name)
    return f"{m[1]}@{m[2]}" if m else name


def is_critical(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(template_of(name), pat)
               for pat in CRITICAL)


def refusal(unit: Unit, op: str) -> str | None:
    """Why `op` is not allowed on `unit` from this app, or None when it is."""
    if op not in OPERATIONS:
        return f"unknown operation {op!r}"
    if not NAME_RE.match(unit.name):
        return "unusual unit name"
    if not unit.name.endswith(ACTION_TYPES):
        return "only services, sockets, timers and paths can be changed here"
    if unit.load in ("masked", "not-found", "error", "bad-setting"):
        return f"the unit is {unit.load}"
    if op in ("stop", "restart", "disable") and is_critical(unit.name):
        return "a core part of the system or the desktop session; stopping it can log you " \
               "out or cut the network"
    if op in ("enable", "disable"):
        if unit.file_state not in TOGGLE_STATES:
            return f"the unit file is {unit.file_state or 'unknown'}: it cannot be " \
                   "enabled or disabled"
        if (op == "enable") == (unit.file_state == "enabled"):
            return f"already {unit.file_state}"
    if op == "start" and unit.active == "active":
        return "already running"
    if op == "stop" and unit.active in ("inactive", "failed"):
        return "not running"
    return None


def allowed_ops(unit: Unit) -> dict[str, str | None]:
    return {op: refusal(unit, op) for op in OPERATIONS}


FILTERS = {
    "all": lambda u: True,
    "running": lambda u: u.active == "active",
    "failed": lambda u: u.active == "failed",
    "stopped": lambda u: u.active in ("inactive",),
    "enabled": lambda u: u.file_state.startswith("enabled"),
}
TYPES = {"service": (".service",), "timer": (".timer",), "socket": (".socket",),
         "all": None}


def filter_units(units: list[Unit], text: str = "", state: str = "all",
                 kind: str = "service") -> list[Unit]:
    t = text.strip().lower()
    pred = FILTERS.get(state, FILTERS["all"])
    types = TYPES.get(kind)
    out = []
    for u in units:
        if types and not u.name.endswith(types):
            continue
        if not pred(u):
            continue
        if t and t not in u.name.lower() and t not in u.description.lower() and t != str(
                u.pid or ""):
            continue
        out.append(u)
    return out


SORTS = {
    "name": lambda u: u.name.lower(),
    "state": lambda u: ({"failed": 0, "active": 1}.get(u.active, 2), u.name.lower()),
    "enabled": lambda u: (u.file_state, u.name.lower()),
    "pid": lambda u: u.pid or 0,
    "memory": lambda u: u.memory or 0,
}


def sort_units(units: list[Unit], key: str = "name", desc: bool = False) -> list[Unit]:
    return sorted(units, key=SORTS.get(key, SORTS["name"]), reverse=desc)


def counts(units: list[Unit]) -> dict[str, int]:
    return {"total": len(units), "running": sum(1 for u in units if u.active == "active"),
            "failed": sum(1 for u in units if u.active == "failed")}
