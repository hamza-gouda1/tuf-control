# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Refresh-rate switching through org.gnome.Mutter.DisplayConfig (session bus, async).

`FakeDisplayConfig` is used whenever TUF_CONTROL_FAKE_ROOT is set (and in tests), so
development never reconfigures a real monitor.
"""
from __future__ import annotations

import copy
import os
from collections.abc import Callable

from gi.repository import Gio, GLib

from ..controller import FAKE_ROOT_ENV
from .viewmodels import build_apply_config

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
METHOD_TEMPORARY, METHOD_PERSISTENT = 1, 2
TIMEOUT_MS = 3000

Done = Callable[[object], None]
Fail = Callable[[BaseException], None]


class MutterDisplayConfig:
    def __init__(self) -> None:
        self._bus = None

    def _call(self, method: str, params, done: Done, fail: Fail) -> None:
        def ready(conn, res):
            try:
                out = conn.call_finish(res)
                done(out.unpack() if out is not None else None)
            except GLib.Error as exc:
                fail(RuntimeError(exc.message))

        def go(bus):
            bus.call(BUS_NAME, PATH, BUS_NAME, method, params, None, Gio.DBusCallFlags.NONE,
                     TIMEOUT_MS, None, ready)

        if self._bus is not None:
            go(self._bus)
            return

        def got_bus(_src, res):
            try:
                self._bus = Gio.bus_get_finish(res)
            except GLib.Error as exc:
                fail(RuntimeError(f"session bus unavailable: {exc.message}"))
                return
            go(self._bus)

        Gio.bus_get(Gio.BusType.SESSION, None, got_bus)

    def get_state(self, done: Done, fail: Fail) -> None:
        self._call("GetCurrentState", None, done, fail)

    def set_mode(self, state, connector: str, mode_id: str, done: Done, fail: Fail) -> None:
        logical = build_apply_config(state, connector, mode_id)
        params = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                              (state[0], METHOD_PERSISTENT, logical, {}))
        self._call("ApplyMonitorsConfig", params, done, fail)


def _fake_state() -> tuple:
    def mode(rate, current=False):
        return (f"2560x1600@{rate:.3f}", 2560, 1600, rate, 1.6, [1.0, 1.25, 1.6, 2.0],
                {"is-current": current, "is-preferred": rate > 160})
    monitors = [(("eDP-1", "BOE", "0x0b5a", "0x00000000"),
                 [mode(165.0, True), mode(120.0), mode(60.0)],
                 {"is-builtin": True, "display-name": "Built-in display"})]
    logical = [(0, 0, 1.6, 0, True, [("eDP-1", "BOE", "0x0b5a", "0x00000000")], {})]
    return (1, monitors, logical, {})


class FakeDisplayConfig:
    """In-memory stand-in with a 2560x1600 panel at 60/120/165 Hz."""

    def __init__(self) -> None:
        self.state = _fake_state()

    def get_state(self, done: Done, fail: Fail) -> None:
        GLib.idle_add(lambda: (done(copy.deepcopy(self.state)), False)[1])

    def set_mode(self, state, connector, mode_id, done: Done, fail: Fail) -> None:
        build_apply_config(state, connector, mode_id)  # validate the same way as for real
        serial, monitors, logical, props = self.state
        for spec, modes, _p in monitors:
            if spec[0] == connector:
                for m in modes:
                    m[6]["is-current"] = m[0] == mode_id
        self.state = (serial + 1, monitors, logical, props)
        GLib.idle_add(lambda: (done(None), False)[1])


def make_display_config():
    if os.environ.get(FAKE_ROOT_ENV) or os.environ.get("TUF_CONTROL_FAKE_DISPLAY"):
        return FakeDisplayConfig()
    return MutterDisplayConfig()
