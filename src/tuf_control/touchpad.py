# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""GSettings for the touchpad and for the quick-settings extension's gestures.

Thin read/write wrappers over Gio.Settings. Every call is synchronous and fast
(a read is an in-memory lookup, a write a dconf/keyfile commit), so the page
applies changes directly on the main thread; nothing here goes through run_bg.
No widgets, so the logic is unit-testable without a display: the tests use
GSETTINGS_BACKEND=keyfile and a temporary XDG_CONFIG_HOME, never the real dconf.
"""
from __future__ import annotations

import os

from gi.repository import Gio

TOUCHPAD_SCHEMA = "org.gnome.desktop.peripherals.touchpad"
EXTENSION_SCHEMA = "org.gnome.shell.extensions.tuf-control-quick"
EXTENSION_UUID = "tuf-control-quick@hamza-gouda"

SEND_EVENTS = ("enabled", "disabled", "disabled-on-external-mouse")
CLICK_METHODS = ("default", "none", "areas", "fingers")

GESTURE_KEYS = (
    "gesture-swipe3-down",
    "gesture-swipe3-left",
    "gesture-swipe3-right",
    "gesture-hold4",
    "gesture-swipe4-up",
    "gesture-swipe4-down",
    "gesture-swipe4-left",
    "gesture-swipe4-right",
    "gesture-pinch-in",
    "gesture-pinch-out",
)
GESTURE_ACTIONS = (
    "none",
    "overview",
    "app-grid",
    "show-desktop",
    "switch-windows",
    "prev-workspace",
    "next-workspace",
    "launch-app",
    "prev-window",
    "next-window",
    "volume-up",
    "volume-down",
    "play-pause",
)


class SchemaMissing(Exception):
    """A GSettings schema is not reachable. For the touchpad that would be a broken
    desktop install; for the extension it means it is not installed."""

    def __init__(self, schema_id: str) -> None:
        super().__init__(f"GSettings schema not installed: {schema_id}")
        self.schema_id = schema_id


def schema_lookup(schema_id: str, source: Gio.SettingsSchemaSource | None = None
                  ) -> Gio.SettingsSchema | None:
    # recursive=True: the default source chains per-directory schema sources, and a
    # non-recursive lookup misses schemas that live in those includes (the desktop's
    # touchpad schema among them)
    return (source or Gio.SettingsSchemaSource.get_default()).lookup(schema_id, True)


def extension_source() -> Gio.SettingsSchemaSource | None:
    """The quick-settings extension's own schema directory, chained onto the default
    source. install.sh compiles the schema in place under the extension, where the
    default source never looks; None when the extension is not installed."""
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(data, "gnome-shell", "extensions", EXTENSION_UUID, "schemas")
    if not os.path.isfile(os.path.join(d, "gschemas.compiled")):
        return None
    return Gio.SettingsSchemaSource.new_from_directory(d, Gio.SettingsSchemaSource.get_default(),
                                                       False)


def _open(schema_id: str, source: Gio.SettingsSchemaSource | None) -> Gio.SettingsSchema:
    schema = schema_lookup(schema_id, source)
    if schema is None:
        raise SchemaMissing(schema_id)
    return schema


class TouchpadSettings:
    """org.gnome.desktop.peripherals.touchpad. The schema ships with
    gsettings-desktop-schemas, so only a broken desktop misses it: __init__ raises
    SchemaMissing then, and the page shows the reason instead of the rows."""

    def __init__(self, schema_id: str = TOUCHPAD_SCHEMA,
                 source: Gio.SettingsSchemaSource | None = None) -> None:
        self.schema_id = schema_id
        self._schema = _open(schema_id, source)
        self.settings = Gio.Settings.new_full(self._schema, None, None)

    def _nicks(self, key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        """The values the schema allows for one of its enum keys."""
        try:
            kind, values = self._schema.get_key(key).get_range()
        except Exception:
            return fallback
        return tuple(values) if kind == "enum" and values else fallback

    def click_method_choices(self) -> tuple[str, ...]:
        return self._nicks("click-method", CLICK_METHODS)

    def send_events(self) -> str:
        return self.settings.get_string("send-events")

    def set_send_events(self, value: str) -> None:
        if value not in self._nicks("send-events", SEND_EVENTS):
            raise ValueError(f"send-events must be one of {', '.join(SEND_EVENTS)}, "
                             f"not {value!r}")
        self.settings.set_string("send-events", value)

    def tap_to_click(self) -> bool:
        return self.settings.get_boolean("tap-to-click")

    def set_tap_to_click(self, on: bool) -> None:
        self.settings.set_boolean("tap-to-click", bool(on))

    def natural_scroll(self) -> bool:
        return self.settings.get_boolean("natural-scroll")

    def set_natural_scroll(self, on: bool) -> None:
        self.settings.set_boolean("natural-scroll", bool(on))

    def speed(self) -> float:
        return self.settings.get_double("speed")

    def set_speed(self, value: float) -> None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"speed must be a number, not {value!r}") from None
        if not -1.0 <= value <= 1.0:
            raise ValueError("speed must be between -1 and 1")
        self.settings.set_double("speed", value)

    def click_method(self) -> str:
        return self.settings.get_string("click-method")

    def set_click_method(self, value: str) -> None:
        if value not in self.click_method_choices():
            raise ValueError(f"click-method must be one of "
                             f"{', '.join(self.click_method_choices())}, not {value!r}")
        self.settings.set_string("click-method", value)


class GestureSettings:
    """The quick-settings extension's gesture bindings (org.gnome.shell.extensions.
    tuf-control-quick). The schema travels with the extension, so `available` tells
    the page whether it is installed and its schema reachable; the read and write
    methods raise SchemaMissing when it is not."""

    def __init__(self, schema_id: str = EXTENSION_SCHEMA,
                 source: Gio.SettingsSchemaSource | None = None) -> None:
        self.schema_id = schema_id
        if source is not None:
            schema = schema_lookup(schema_id, source)
        else:
            chained = extension_source()
            schema = (schema_lookup(schema_id) or
                      (schema_lookup(schema_id, chained) if chained else None))
        self._schema = schema
        self.available = schema is not None
        self.settings = Gio.Settings.new_full(schema, None, None) if schema else None

    def _require(self) -> Gio.Settings:
        if self.settings is None:
            raise SchemaMissing(self.schema_id)
        return self.settings

    @staticmethod
    def _check_key(key: str) -> None:
        if key not in GESTURE_KEYS:
            raise ValueError(f"unknown gesture key: {key!r}")

    def enabled(self) -> bool:
        return self._require().get_boolean("gesture-enabled")

    def set_enabled(self, on: bool) -> None:
        self._require().set_boolean("gesture-enabled", bool(on))

    def binding_choices(self, key: str) -> tuple[str, ...]:
        self._check_key(key)
        self._require()
        if not self._schema.has_key(key):
            return ()
        kind, values = self._schema.get_key(key).get_range()
        return tuple(action for action in values if action in GESTURE_ACTIONS) if kind == "enum" else ()

    def binding(self, key: str) -> str:
        if not self.binding_choices(key):
            return "none"
        return self._require().get_string(key)

    def set_binding(self, key: str, action: str) -> None:
        if action not in self.binding_choices(key):
            raise ValueError(f"unsupported action {action!r} for gesture {key!r}")
        self._require().set_string(key, action)

    def launch_app(self) -> str:
        return self._require().get_string("gesture-launch-app")

    def set_launch_app(self, desktop_id: str) -> None:
        self._require().set_string("gesture-launch-app", desktop_id or "")

    def hides_power_mode(self) -> bool | None:
        """GNOME's built-in Power Mode tile is hidden while our tile runs (they switch
        the same platform profile). Cosmetic: the profile itself is unaffected.
        None when the installed extension's schema predates this key — the app can be
        newer than the extension, and touching a missing key is a hard abort."""
        settings = self._require()
        if not settings.get_property("settings-schema").has_key("hide-power-mode"):
            return None
        return settings.get_boolean("hide-power-mode")

    def set_hides_power_mode(self, on: bool) -> bool:
        settings = self._require()
        if not settings.get_property("settings-schema").has_key("hide-power-mode"):
            return False
        settings.set_boolean("hide-power-mode", bool(on))
        return True
