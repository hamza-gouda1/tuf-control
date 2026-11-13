# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Action sink: the only place automation touches settings. Every write goes through the
Controller (which validates bounds and honours dry-run); power-profiles-daemon has no
Controller capability of its own, so `PpdClient` talks to it directly with a fixed,
validated set of names."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from ..controller import FAKE_ROOT_ENV, Controller
from ..core import BackendUnavailable, KbdEffect, Profile, ValidationError
from .engine import RESTORE_CONFIG
from .rules import PPD_NAMES


class PpdClient:
    """net.hadess.PowerProfiles ActiveProfile. Under ASUSIUS_FAKE_ROOT it reads and
    writes <root>/fake/ppd.json instead, so tests never reach the system bus."""

    def __init__(self, transport=None, fake_root: str | None = None) -> None:
        self.fake_root = fake_root if fake_root is not None else os.environ.get(FAKE_ROOT_ENV)
        self._t = transport

    def _fake(self) -> Path:
        return Path(self.fake_root) / "fake" / "ppd.json"

    def _transport(self):
        if self._t is None:
            from ..backends.dbus import GioTransport
            self._t = GioTransport()
        return self._t

    def get(self) -> str:
        if self.fake_root:
            try:
                return json.loads(self._fake().read_text())["active"]
            except (OSError, ValueError, KeyError):
                return "balanced"
        from ..backends.dbus import PPD, PPD_PATH
        return str(self._transport().get_property(PPD, PPD_PATH, PPD, "ActiveProfile"))

    def set(self, name: str, dry_run: bool = False) -> str:
        if name not in PPD_NAMES:
            raise ValidationError(f"unknown power-profiles-daemon profile {name!r}")
        if dry_run:
            return f"dry run: would set power-profiles-daemon to {name}"
        if self.fake_root:
            self._fake().parent.mkdir(parents=True, exist_ok=True)
            self._fake().write_text(json.dumps({"active": name}))
            return f"set power-profiles-daemon to {name} via fake"
        from ..backends.dbus import PPD, PPD_PATH
        t = self._transport()
        if not t.name_has_owner(PPD):
            raise BackendUnavailable("power-profiles-daemon not running")
        t.set_property(PPD, PPD_PATH, PPD, "ActiveProfile", "s", name)
        return f"set power-profiles-daemon to {name}"


def _rgb(v) -> tuple[int, int, int]:
    return tuple(int(x) for x in v)  # type: ignore[return-value]


class ControllerSink:
    """read(key) -> current value; apply(key, value, dry_run) -> human detail."""

    def __init__(self, ctl: Controller, *, ppd: PpdClient | None = None,
                 effect_get: Callable[[], object] | None = None,
                 effect_set: Callable[[object, bool], str] | None = None,
                 kbd_changed: Callable[[KbdEffect], None] | None = None,
                 kbd_saved: Callable[[], KbdEffect | None] | None = None) -> None:
        self.ctl = ctl
        self.ppd = ppd or PpdClient()
        self.effect_get = effect_get
        self.effect_set = effect_set
        self.kbd_changed = kbd_changed
        self.kbd_saved = kbd_saved  # hardware mode a running software effect will restore

    @staticmethod
    def _res(res) -> str:
        return f"{res.detail} via {res.backend}"

    def read(self, key: str):
        c = self.ctl
        if key == "profile":
            return c.get_profile()[0].value
        if key == "kbd":
            saved = self.kbd_saved() if self.kbd_saved else None
            if saved is not None:  # the keyboard shows an effect: remember what is under it
                return {"mode": saved.mode, "colour1": list(saved.colour1),
                        "colour2": list(saved.colour2), "speed": saved.speed,
                        "direction": saved.direction}
            data = c.get_kbd_rgb()[0].get("mode_data") or {}
            return {"mode": int(data.get("mode", 0)),
                    "colour1": list(data.get("colour1", (0, 0, 0))),
                    "colour2": list(data.get("colour2", (0, 0, 0))),
                    "speed": int(data.get("speed", 1)), "direction": int(data.get("direction", 0))}
        if key == "kbd_brightness":
            return c.get_kbd_brightness()[0][0]
        if key == "charge_limit":
            return c.get_battery_threshold()[0]
        if key == "panel_overdrive":
            return bool(int(self._overdrive_attr().current))
        if key == "ppd":
            return self.ppd.get()
        if key == "effect":
            return self.effect_get() if self.effect_get else RESTORE_CONFIG
        raise ValidationError(f"unknown setting {key!r}")

    def _overdrive_attr(self):
        attrs, _ = self.ctl.get_attributes()
        attr = next((a for a in attrs if a.name == "panel_overdrive"), None)
        if attr is None:
            raise BackendUnavailable("panel_overdrive attribute not present")
        return attr

    def apply(self, key: str, value, dry_run: bool = False) -> str:
        c = self.ctl
        if key == "profile":
            return self._res(c.set_profile(Profile.parse(value), dry_run=dry_run))
        if key == "kbd":
            eff = KbdEffect(int(value["mode"]), 0, _rgb(value.get("colour1", (0, 0, 0))),
                            _rgb(value.get("colour2", (0, 0, 0))), int(value.get("speed", 1)),
                            int(value.get("direction", 0)))
            detail = self._res(c.set_kbd_effect(eff, dry_run=dry_run))
            if not dry_run and self.kbd_changed:
                self.kbd_changed(eff)  # a running software effect restores to this later
            return detail
        if key == "kbd_brightness":
            return self._res(c.set_kbd_brightness(int(value), dry_run=dry_run))
        if key == "charge_limit":
            return self._res(c.set_battery_threshold(int(value), dry_run=dry_run))
        if key == "panel_overdrive":
            attr = self._overdrive_attr()
            want = "1" if value else "0"
            if attr.possible_values and want not in attr.possible_values:
                raise ValidationError(f"panel_overdrive: {want} not in {attr.possible_values}")
            return self._res(c.set_attribute("panel_overdrive", int(want), dry_run=dry_run))
        if key == "ppd":
            return self.ppd.set(str(value), dry_run=dry_run)
        if key == "effect":
            if self.effect_set is None:
                if dry_run:
                    name = value.get("name") if isinstance(value, dict) else None
                    return f"dry run: would {'start effect ' + name if name else 'stop effects'}"
                raise BackendUnavailable("software effects run in the daemon only")
            return self.effect_set(value, dry_run)
        raise ValidationError(f"unknown setting {key!r}")
