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

from dataclasses import dataclass
import os
import shutil
import subprocess

import gi
try:
    gi.require_version("Gio", "2.0")
except ValueError:
    pass
from gi.repository import Gio

TOUCHPAD_SCHEMA = "org.gnome.desktop.peripherals.touchpad"
EXTENSION_SCHEMA = "org.gnome.shell.extensions.asusius-quick"
EXTENSION_UUID = "asusius-quick@hamza-gouda"

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
    "media-next",
    "media-prev",
)


class SchemaMissing(Exception):
    """A GSettings schema is not reachable. For the touchpad that would be a broken
    desktop install; for the extension it means it is not installed."""

    def __init__(self, schema_id: str) -> None:
        super().__init__(f"GSettings schema not installed: {schema_id}")
        self.schema_id = schema_id


_NON_GNOME_DESKTOPS = frozenset({
    "kde", "plasma", "xfce", "hyprland", "sway", "river", "niri",
    "lxqt", "lxde", "mate", "cinnamon", "x-cinnamon", "cosmic", "i3",
    "openbox", "enlightenment", "pantheon", "deepin",
})


@dataclass(frozen=True)
class SessionInfo:
    """Snapshot of the active desktop session and GNOME Shell extension state."""

    desktop: str
    is_gnome: bool
    is_other_desktop: bool
    session_type: str
    installed: bool
    schema_compiled: bool
    extension_path: str | None
    shell_state: str
    enabled: bool | None = None
    detail: str = ""


def schema_lookup(schema_id: str, source: Gio.SettingsSchemaSource | None = None
                  ) -> Gio.SettingsSchema | None:
    # recursive=True: the default source chains per-directory schema sources, and a
    # non-recursive lookup misses schemas that live in those includes (the desktop's
    # touchpad schema among them)
    resolved = source or Gio.SettingsSchemaSource.get_default()
    if resolved is None:
        return None
    return resolved.lookup(schema_id, True)


def _extension_data_dirs() -> list[str]:
    """Return XDG data directories in lookup order without duplicates."""
    home_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    sys_raw = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    candidates = [home_data, *(p for p in sys_raw.split(os.pathsep) if p)]
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in candidates:
        norm = os.path.normpath(os.path.expanduser(raw))
        if norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    return ordered


def extension_dir(uuid: str = EXTENSION_UUID) -> str | None:
    """Return the highest-precedence installed extension directory, or None."""
    for base in _extension_data_dirs():
        path = os.path.join(base, "gnome-shell", "extensions", uuid)
        if os.path.isdir(path):
            return path
    return None


def extension_source(uuid: str = EXTENSION_UUID) -> Gio.SettingsSchemaSource | None:
    """The quick-settings extension's own schema directory, chained onto the default
    source. Searches both XDG_DATA_HOME (install.sh) and XDG_DATA_DIRS (/usr/share for
    system packages like Arch PKGBUILD); None when no compiled schema is found."""
    for base in _extension_data_dirs():
        d = os.path.join(base, "gnome-shell", "extensions", uuid, "schemas")
        if os.path.isfile(os.path.join(d, "gschemas.compiled")):
            return Gio.SettingsSchemaSource.new_from_directory(
                d, Gio.SettingsSchemaSource.get_default(), False)
    return None


def desktop_session() -> tuple[str, bool, bool, str]:
    """Return ``(desktop, is_gnome, is_other_desktop, session_type)`` from env vars."""
    raw_desktop = (
        os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or ""
    ).strip()
    parts = [p.strip() for p in raw_desktop.split(":") if p.strip()]
    lower = {p.lower() for p in parts}
    # Budgie:GNOME sets GNOME for session services, not GNOME Shell.
    is_gnome = bool(
        ("gnome" in lower or any(t.startswith("gnome-") for t in lower))
        and "budgie" not in lower
    )
    if is_gnome:
        desktop = "GNOME"
    elif parts:
        desktop = parts[0]
    else:
        desktop = "unknown"
    is_other = bool(parts) and (not is_gnome or bool(lower & _NON_GNOME_DESKTOPS))

    sess = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if not sess and os.environ.get("WAYLAND_DISPLAY"):
        sess = "wayland"
    if not sess:
        sess = "unknown"
    return desktop, is_gnome, is_other, sess


def parse_gnome_extensions_info(stdout: str) -> tuple[str, bool | None]:
    """Parse ``gnome-extensions info`` stdout into ``(normalized_state, enabled)``."""
    state = "UNKNOWN"
    enabled: bool | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("state:"):
            raw = stripped.split(":", 1)[1].strip().upper().replace(" ", "_")
            if raw == "ENABLED":
                raw = "ACTIVE"
            elif raw == "DISABLED":
                raw = "INACTIVE"
            state = raw or "UNKNOWN"
        elif lower.startswith("enabled:"):
            val = stripped.split(":", 1)[1].strip().lower()
            if val in {"yes", "true"}:
                enabled = True
            elif val in {"no", "false"}:
                enabled = False
    return state, enabled


def _run_gnome_extensions(args: list[str], *, runner=None,
                          timeout: float = 2.5) -> subprocess.CompletedProcess[str] | None:
    """Execute ``gnome-extensions <args>`` safely, returning None if unavailable."""
    if runner is None:
        cli = shutil.which("gnome-extensions")
        if not cli:
            return None
        cmd = [cli, *args]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
    try:
        return runner(["gnome-extensions", *args])
    except (OSError, subprocess.TimeoutExpired):
        return None


def session_info(uuid: str = EXTENSION_UUID, *, query_shell: bool = True,
                 runner=None) -> SessionInfo:
    """Inspect session environment, extension files on disk, and GNOME Shell status."""
    desktop, is_gnome, is_other, session_type = desktop_session()
    ext_path = extension_dir(uuid)
    installed = ext_path is not None
    compiled = bool(
        ext_path and os.path.isfile(os.path.join(ext_path, "schemas", "gschemas.compiled"))
    ) or (extension_source(uuid) is not None)

    if not installed:
        return SessionInfo(
            desktop, is_gnome, is_other, session_type,
            installed=False, schema_compiled=False, extension_path=None,
            shell_state="not_installed",
        )

    if not query_shell or is_other:
        return SessionInfo(
            desktop, is_gnome, is_other, session_type,
            installed=True, schema_compiled=compiled, extension_path=ext_path,
            shell_state="unknown",
        )

    proc = _run_gnome_extensions(["info", uuid], runner=runner)
    if proc is None:
        return SessionInfo(
            desktop, is_gnome, is_other, session_type,
            installed=True, schema_compiled=compiled, extension_path=ext_path,
            shell_state="unavailable",
        )

    if proc.returncode == 0:
        state, enabled = parse_gnome_extensions_info(proc.stdout)
        if state == "ACTIVE":
            shell_state = "active"
        elif state in {"ERROR", "OUT_OF_DATE"}:
            shell_state = state.lower()
        elif state in {"INACTIVE", "INITIALIZED"} or enabled is False:
            shell_state = "inactive"
        else:
            shell_state = "unknown"
        return SessionInfo(
            desktop, is_gnome, is_other, session_type,
            installed=True, schema_compiled=compiled, extension_path=ext_path,
            shell_state=shell_state, enabled=enabled,
        )

    detail = (proc.stderr or proc.stdout or "").strip()
    lower_detail = detail.lower()
    if "does not exist" in lower_detail or "not found" in lower_detail:
        shell_state = "needs_relogin"
    else:
        shell_state = "unavailable"
    return SessionInfo(
        desktop, is_gnome, is_other, session_type,
        installed=True, schema_compiled=compiled, extension_path=ext_path,
        shell_state=shell_state, detail=detail,
    )


def enable_extension(uuid: str = EXTENSION_UUID, *, runner=None) -> tuple[bool, str]:
    """Enable the GNOME Shell extension via ``gnome-extensions enable``."""
    proc = _run_gnome_extensions(["enable", uuid], runner=runner, timeout=3.0)
    if proc is None:
        return False, "gnome-extensions CLI is not available."
    if proc.returncode == 0:
        return True, "Shell extension enabled."
    detail = (proc.stderr or proc.stdout or "").strip()
    if "does not exist" in detail.lower() or "not found" in detail.lower():
        return (
            False,
            "GNOME Shell has not discovered the extension yet. Log out and back in first.",
        )
    return False, detail or f"gnome-extensions exited with status {proc.returncode}."


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
    asusius-quick). The schema travels with the extension, so `available` tells
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
        if kind != "enum":
            return ()
        return tuple(action for action in values if action in GESTURE_ACTIONS)

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

    def hides_night_light(self) -> bool | None:
        """GNOME's built-in Night Light tile can be hidden from the Quick Settings
        panel while the extension runs. Cosmetic: Night Light itself is unaffected.
        None when the installed extension's schema predates this key — the app can
        be newer than the extension, and touching a missing key is a hard abort."""
        settings = self._require()
        if not settings.get_property("settings-schema").has_key("hide-night-light"):
            return None
        return settings.get_boolean("hide-night-light")

    def set_hides_night_light(self, on: bool) -> bool:
        settings = self._require()
        if not settings.get_property("settings-schema").has_key("hide-night-light"):
            return False
        settings.set_boolean("hide-night-light", bool(on))
        return True
