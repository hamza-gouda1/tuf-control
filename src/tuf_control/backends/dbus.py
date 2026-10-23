"""D-Bus backend: asusd, supergfxd and power-profiles-daemon through Gio.

PyGObject is imported lazily, only by GioTransport. All calls carry a timeout.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import Future
from typing import Any, Protocol

from ..core import (
    BackendError,
    BackendUnavailable,
    Capability,
    CapStatus,
    FanCurve,
    GpuMode,
    PowerLimit,
    Profile,
    ValidationError,
)
from .base import Backend

ASUSD = "xyz.ljones.Asusd"
PLATFORM_PATH, PLATFORM_IFACE = "/xyz/ljones", "xyz.ljones.Platform"
FANCURVES_IFACE = "xyz.ljones.FanCurves"
ARMOURY_PATH, ARMOURY_IFACE = "/xyz/ljones/asus_armoury", "xyz.ljones.AsusArmoury"
AURA_ROOT, AURA_IFACE = "/xyz/ljones/aura", "xyz.ljones.Aura"
SUPERGFX = "org.supergfxctl.Daemon"
SUPERGFX_PATH = "/org/supergfxctl/Gfx"
PPD = "net.hadess.PowerProfiles"
PPD_PATH = "/net/hadess/PowerProfiles"
DEFAULT_TIMEOUT_MS = 2500


class Transport(Protocol):
    def name_has_owner(self, name: str) -> bool: ...
    def get_property(self, dest: str, path: str, iface: str, prop: str) -> Any: ...
    def get_all(self, dest: str, path: str, iface: str) -> dict[str, Any]: ...
    def set_property(self, dest: str, path: str, iface: str, prop: str, sig: str, value: Any): ...
    def call(self, dest: str, path: str, iface: str, method: str,
             sig: str | None = None, args: tuple = ()) -> Any: ...
    def introspect(self, dest: str, path: str) -> str: ...


class GioTransport:
    """Transport over Gio.DBusConnection (system bus). Blocking calls use a timeout."""

    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.timeout_ms = timeout_ms
        self._conn = None
        self._gi: tuple | None = None

    def _mods(self):
        if self._gi is None:
            try:
                import gi
                gi.require_version("Gio", "2.0")
                from gi.repository import Gio, GLib
            except (ImportError, ValueError) as exc:
                raise BackendUnavailable(f"PyGObject/Gio not available: {exc}") from None
            self._gi = (Gio, GLib)
        return self._gi

    def _connection(self):
        if self._conn is None:
            Gio, GLib = self._mods()
            try:
                self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            except GLib.Error as exc:
                raise BackendUnavailable(f"system bus unavailable: {exc.message}") from None
        return self._conn

    def _wrap(self, exc) -> Exception:
        msg = getattr(exc, "message", str(exc))
        if any(k in msg for k in ("ServiceUnknown", "NameHasNoOwner", "UnknownObject",
                                  "UnknownMethod", "UnknownProperty", "UnknownInterface")):
            return BackendUnavailable(msg)
        if "Timeout" in msg or "timed out" in msg.lower():
            return BackendError(f"D-Bus timeout: {msg}")
        return BackendError(msg)

    def call(self, dest, path, iface, method, sig=None, args=()):
        Gio, GLib = self._mods()
        params = GLib.Variant(sig, args) if sig else None
        try:
            res = self._connection().call_sync(
                dest, path, iface, method, params, None, Gio.DBusCallFlags.NONE,
                self.timeout_ms, None)
        except GLib.Error as exc:
            raise self._wrap(exc) from None
        return res.unpack() if res is not None else None

    def call_future(self, dest, path, iface, method, sig=None, args=()) -> Future:
        """True async call; the callback fires on the thread-default GLib main context."""
        Gio, GLib = self._mods()
        fut: Future = Future()
        params = GLib.Variant(sig, args) if sig else None

        def done(conn, res, _data):
            try:
                out = conn.call_finish(res)
                fut.set_result(out.unpack() if out is not None else None)
            except GLib.Error as exc:
                fut.set_exception(self._wrap(exc))

        self._connection().call(dest, path, iface, method, params, None,
                                Gio.DBusCallFlags.NONE, self.timeout_ms, None, done, None)
        return fut

    def get_property(self, dest, path, iface, prop):
        return self.call(dest, path, "org.freedesktop.DBus.Properties", "Get",
                         "(ss)", (iface, prop))[0]

    def get_all(self, dest, path, iface):
        return self.call(dest, path, "org.freedesktop.DBus.Properties", "GetAll",
                         "(s)", (iface,))[0]

    def set_property(self, dest, path, iface, prop, sig, value):
        _, GLib = self._mods()
        self._connection()  # fail early
        try:
            Gio, _ = self._mods()
            self._connection().call_sync(
                dest, path, "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", (iface, prop, GLib.Variant(sig, value))), None,
                Gio.DBusCallFlags.NONE, self.timeout_ms, None)
        except GLib.Error as exc:
            raise self._wrap(exc) from None

    def introspect(self, dest, path):
        return self.call(dest, path, "org.freedesktop.DBus.Introspectable", "Introspect")[0]

    def name_has_owner(self, name):
        try:
            return bool(self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                  "org.freedesktop.DBus", "NameHasOwner", "(s)", (name,))[0])
        except BackendUnavailable:
            return False


def _opt(v: int | None) -> int | None:
    return None if v is None or v < 0 else v


class DBusBackend(Backend):
    name = "dbus"

    def __init__(self, transport: Transport | None = None) -> None:
        self.t: Transport = transport or GioTransport()
        self._owners: dict[str, bool] | None = None

    def _owner(self, name: str) -> bool:
        if self._owners is None:
            self._owners = {}
        if name not in self._owners:
            try:
                self._owners[name] = self.t.name_has_owner(name)
            except BackendError:
                self._owners[name] = False
        return self._owners[name]

    def _need(self, name: str) -> None:
        if not self._owner(name):
            raise BackendUnavailable(f"{name} not running on the system bus")

    def _children(self, dest: str, path: str) -> list[str]:
        xml = self.t.introspect(dest, path)
        return sorted(n.get("name") for n in ET.fromstring(xml).findall("node") if n.get("name"))

    def probe(self):
        res: dict[Capability, CapStatus] = {}
        asusd, gfx, ppd = self._owner(ASUSD), self._owner(SUPERGFX), self._owner(PPD)
        no_asusd = CapStatus(False, "asusd (xyz.ljones.Asusd) not on the system bus")
        for cap in (Capability.POWER_LIMITS,
                    Capability.BATTERY_THRESHOLD, Capability.KBD_BRIGHTNESS,
                    Capability.KBD_RGB, Capability.FAN_CURVES):
            res[cap] = CapStatus(True) if asusd else no_asusd
        res[Capability.PROFILE] = CapStatus(True) if (asusd or ppd) else CapStatus(
            False, "neither asusd nor power-profiles-daemon on the system bus")
        res[Capability.GPU_MODE] = CapStatus(True) if gfx else CapStatus(
            False, "supergfxd (org.supergfxctl.Daemon) not on the system bus")
        res[Capability.FIRMWARE_ATTRIBUTES] = CapStatus(False, "full attribute set read via sysfs")
        res[Capability.BATTERY_HEALTH] = CapStatus(False, "not provided over D-Bus")
        res[Capability.TELEMETRY] = CapStatus(False, "not provided over D-Bus")
        if asusd:
            try:
                limits = self.get_limits()
                if not any(x.bounded for x in limits):
                    res[Capability.POWER_LIMITS] = CapStatus(
                        False, "asusd reports no PPT values (-1); EnablePptGroup off or unsupported")
            except BackendError as exc:
                res[Capability.POWER_LIMITS] = CapStatus(False, f"asusd armoury query failed: {exc}")
        return res

    # -- profile ----------------------------------------------------------
    def get_profile(self) -> Profile:
        if self._owner(ASUSD):
            ident = self.t.get_property(ASUSD, PLATFORM_PATH, PLATFORM_IFACE, "PlatformProfile")
            return Profile.from_asusd_id(int(ident))
        self._need(PPD)
        return Profile.parse(self.t.get_property(PPD, PPD_PATH, PPD, "ActiveProfile"))

    def set_profile(self, profile: Profile) -> None:
        if self._owner(ASUSD):
            self.t.set_property(ASUSD, PLATFORM_PATH, PLATFORM_IFACE, "PlatformProfile",
                                "u", profile.asusd_id)
            return
        self._need(PPD)
        self.t.set_property(PPD, PPD_PATH, PPD, "ActiveProfile", "s", profile.ppd_name)

    # -- armoury ----------------------------------------------------------
    def get_limits(self) -> list[PowerLimit]:
        self._need(ASUSD)
        out = []
        for child in self._children(ASUSD, ARMOURY_PATH):
            props = self.t.get_all(ASUSD, f"{ARMOURY_PATH}/{child}", ARMOURY_IFACE)
            avail = props.get("AvailableAttrs", [])
            if "scalar_increment" not in avail and "min_value" not in avail:
                continue
            out.append(PowerLimit(
                name=child, current=_opt(props.get("CurrentValue")),
                min=_opt(props.get("MinValue")), max=_opt(props.get("MaxValue")),
                default=_opt(props.get("DefaultValue")),
                increment=max(1, props.get("ScalarIncrement", 1))))
        return out

    def set_limit(self, name: str, value: int) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValidationError(f"bad attribute name {name!r}")
        lim = next((x for x in self.get_limits() if x.name == name), None)
        if lim is None:
            raise ValidationError(f"unknown power limit {name!r}")
        lim.validate(value)
        self.t.set_property(ASUSD, f"{ARMOURY_PATH}/{name}", ARMOURY_IFACE,
                            "CurrentValue", "i", value)

    def get_attributes(self):
        self._no("firmware attributes (use sysfs for the full set)")

    # -- battery ------------------------------------------------------------
    def get_battery_threshold(self) -> int:
        self._need(ASUSD)
        return int(self.t.get_property(ASUSD, PLATFORM_PATH, PLATFORM_IFACE,
                                       "ChargeControlEndThreshold"))

    def set_battery_threshold(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 20 <= value <= 100:
            raise ValidationError("battery threshold must be an integer in 20-100")
        self._need(ASUSD)
        self.t.set_property(ASUSD, PLATFORM_PATH, PLATFORM_IFACE,
                            "ChargeControlEndThreshold", "y", value)

    # -- gpu ----------------------------------------------------------------
    def get_gpu_mode(self) -> GpuMode:
        self._need(SUPERGFX)
        res = self.t.call(SUPERGFX, SUPERGFX_PATH, SUPERGFX, "Mode")
        return GpuMode(int(res[0]))

    # -- keyboard -----------------------------------------------------------
    def _aura(self) -> str:
        self._need(ASUSD)
        kids = self._children(ASUSD, AURA_ROOT)
        if not kids:
            raise BackendUnavailable("no aura device exposed by asusd")
        return f"{AURA_ROOT}/{kids[0]}"

    def get_kbd_brightness(self) -> tuple[int, int]:
        path = self._aura()
        cur = int(self.t.get_property(ASUSD, path, AURA_IFACE, "Brightness"))
        sup = self.t.get_property(ASUSD, path, AURA_IFACE, "SupportedBrightness")
        return cur, int(max(sup)) if sup else 3

    def set_kbd_brightness(self, value: int) -> None:
        _, mx = self.get_kbd_brightness()
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= mx:
            raise ValidationError(f"keyboard brightness must be 0-{mx}")
        self.t.set_property(ASUSD, self._aura(), AURA_IFACE, "Brightness", "u", value)

    def get_kbd_rgb(self) -> dict:
        path = self._aura()
        props = self.t.get_all(ASUSD, path, AURA_IFACE)
        mode = props.get("LedModeData")
        data = None
        if mode:
            data = {"mode": mode[0], "zone": mode[1], "colour1": list(mode[2]),
                    "colour2": list(mode[3]), "speed": mode[4], "direction": mode[5]}
        return {"path": path, "led_mode": props.get("LedMode"), "mode_data": data,
                "brightness": props.get("Brightness"),
                "supported_modes": props.get("SupportedBasicModes"),
                "supported_brightness": props.get("SupportedBrightness")}

    # -- fan curves -----------------------------------------------------------
    def get_fan_curves(self) -> dict[str, FanCurve]:
        prof = self.get_profile()
        res = self.t.call(ASUSD, PLATFORM_PATH, FANCURVES_IFACE, "FanCurveData", "(u)",
                          (prof.asusd_id,))[0]
        # asusd tuple order: (fan, pwm[8], temp[8], enabled)
        return {name: FanCurve.from_lists(name, list(temps), list(pwms), bool(en))
                for name, pwms, temps, en in res}
