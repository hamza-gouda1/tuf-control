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
    GpuAction,
    GpuMode,
    GpuStatus,
    KbdEffect,
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

    def call(self, dest, path, iface, method, sig=None, args=(), timeout_ms=None):
        Gio, GLib = self._mods()
        params = GLib.Variant(sig, args) if sig else None
        try:
            res = self._connection().call_sync(
                dest, path, iface, method, params, None, Gio.DBusCallFlags.NONE,
                timeout_ms or self.timeout_ms, None)
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


# asusd (6.x) LedModeData is (uu(yyy)(yyy)ss): speed and direction are enum *names*.
_SPEED_NAMES = ("Low", "Med", "High")
_DIRECTION_NAMES = ("Right", "Left", "Up", "Down")


def speed_to_name(v: int) -> str:
    """Model speed (0-2, or a raw HID byte such as 0xE1/0xEB/0xF5) -> asusd name."""
    if 0 <= v <= 2:
        return _SPEED_NAMES[v]
    return _SPEED_NAMES[min(range(3), key=lambda i: abs((0xE1, 0xEB, 0xF5)[i] - v))]


def direction_to_name(v: int) -> str:
    return _DIRECTION_NAMES[v] if 0 <= v < len(_DIRECTION_NAMES) else _DIRECTION_NAMES[0]


def _name_to_index(names: tuple[str, ...], v, default: int) -> int:
    if isinstance(v, str):
        low = [n.lower() for n in names]
        return low.index(v.lower()) if v.lower() in low else default
    return int(v) if v is not None else default


def aura_mode_from_dbus(v) -> int:
    """asusd 6.3 serialises AuraModeNum by variant *index* in some replies (AllModeData
    reports Pulse as 9) but only accepts the discriminant (10) on writes; 9 is not a mode
    (asusd answers 'incorrect type'), so read it as Pulse."""
    v = int(v)
    return 10 if v == 9 else v


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
                        False, "asusd reports no PPT values (-1): the kernel asus-armoury driver has no power-limit table for this board")
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

    def get_gpu_status(self) -> GpuStatus:
        self._need(SUPERGFX)
        mode = self.get_gpu_mode()

        def ask(method):
            try:
                return self.t.call(SUPERGFX, SUPERGFX_PATH, SUPERGFX, method)[0]
            except (BackendError, BackendUnavailable):
                return None
        supported = [GpuMode(int(m)) for m in (ask("Supported") or [])
                     if int(m) in GpuMode._value2member_map_]
        pend_mode, pend_action, power = ask("PendingMode"), ask("PendingUserAction"), ask("Power")
        powers = {0: "active", 1: "suspended", 2: "off", 3: "disabled", 4: "mux-discreet"}
        pm = GpuMode(int(pend_mode)) if pend_mode is not None else None
        if pm in (GpuMode.NONE, mode):
            pm = None
        pa = GpuAction(int(pend_action)) if pend_action is not None and \
            int(pend_action) in GpuAction._value2member_map_ else None
        if pa is GpuAction.NOTHING:
            pa = None
        return GpuStatus(mode, supported, pm, pa,
                         powers.get(int(power)) if power is not None else None)

    def set_gpu_mode(self, mode: GpuMode) -> GpuAction:
        self._need(SUPERGFX)
        # supergfxd may unload drivers before answering: allow a long timeout
        res = self.t.call(SUPERGFX, SUPERGFX_PATH, SUPERGFX, "SetMode", "(u)", (int(mode),),
                          timeout_ms=30000)
        try:
            return GpuAction(int(res[0]))
        except (TypeError, ValueError, IndexError):
            return GpuAction.NOTHING

    # -- keyboard -----------------------------------------------------------
    def _aura(self) -> str:
        # cached: software effects write several times a second
        if getattr(self, "_aura_path", None) is None:
            self._need(ASUSD)
            kids = self._children(ASUSD, AURA_ROOT)
            if not kids:
                raise BackendUnavailable("no aura device exposed by asusd")
            self._aura_path = f"{AURA_ROOT}/{kids[0]}"
        return self._aura_path

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

        def unpack(mode):
            return {"mode": aura_mode_from_dbus(mode[0]), "zone": mode[1],
                    "colour1": list(mode[2]), "colour2": list(mode[3]),
                    "speed": _name_to_index(_SPEED_NAMES, mode[4], 1),
                    "direction": _name_to_index(_DIRECTION_NAMES, mode[5], 0)}
        mode = props.get("LedModeData")
        data = unpack(mode) if mode else None
        try:  # per-mode settings asusd keeps (software effects put Static's back after use)
            res = self.t.call(ASUSD, path, AURA_IFACE, "AllModeData")
            builtins = {aura_mode_from_dbus(k): unpack(v) for k, v in (res[0] or {}).items()}
        except (BackendError, BackendUnavailable, TypeError, IndexError, AttributeError):
            builtins = {}
        modes = props.get("SupportedBasicModes")
        dev = props.get("DeviceType")
        # DeviceType 2 (LaptopKeyboardTuf): asusd writes kbd_rgb_mode with colour1 only,
        # colour2 is kept for D-Bus but the keys never show it
        return {"path": path, "led_mode": props.get("LedMode"), "mode_data": data,
                "device_type": int(dev) if dev is not None else None,
                "builtins": builtins, "brightness": props.get("Brightness"),
                "supported_modes": [aura_mode_from_dbus(m) for m in modes]
                if modes is not None else None,
                "supported_brightness": props.get("SupportedBrightness")}

    def set_kbd_effect(self, effect: KbdEffect) -> None:
        """Exactly what `asusctl aura effect static -c ff0000` and `asusctl aura effect
        breathe --colour ff0000 --colour2 0000ff` send (captured with strace against asusd
        6.3.5): Properties.Set(xyz.ljones.Aura, LedModeData,
        <(uu(yyy)(yyy)ss) mode zone c1 c2 "Med" "Right">) on /xyz/ljones/aura/<dev>.
        (asusctl's breathe drops --speed and always sends "Med"; this sends the speed.)"""
        effect.validate()
        value = (effect.mode, effect.zone, tuple(effect.colour1), tuple(effect.colour2),
                 speed_to_name(effect.speed), direction_to_name(effect.direction))
        try:
            self.t.set_property(ASUSD, self._aura(), AURA_IFACE, "LedModeData",
                                "(uu(yyy)(yyy)ss)", value)
        except BackendUnavailable:
            self._aura_path = None  # asusd restarted or the device went away
            raise

    # -- attributes / battery extras --------------------------------------------
    def set_attribute(self, name: str, value: int | str) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValidationError(f"bad attribute name {name!r}")
        self._need(ASUSD)
        if name not in self._children(ASUSD, ARMOURY_PATH):
            raise BackendUnavailable(f"asusd does not expose {name}")
        self.t.set_property(ASUSD, f"{ARMOURY_PATH}/{name}", ARMOURY_IFACE,
                            "CurrentValue", "i", int(value))

    def one_shot_full_charge(self) -> None:
        self._need(ASUSD)
        self.t.call(ASUSD, PLATFORM_PATH, PLATFORM_IFACE, "OneShotFullCharge")

    # -- fan curves -----------------------------------------------------------
    def set_fan_curve(self, profile: Profile, curve: FanCurve) -> None:
        curve.validate()
        self._need(ASUSD)
        # verified against asusd 6.3: SetFanCurve(u, (s, (yyyyyyyy) pwm, (yyyyyyyy) temp, b))
        if len(curve.points) != 8:
            raise ValidationError("asusd fan curves need exactly 8 points")
        data = (curve.fan, tuple(p.pwm for p in curve.points),
                tuple(p.temp for p in curve.points), curve.enabled)
        self.t.call(ASUSD, PLATFORM_PATH, FANCURVES_IFACE, "SetFanCurve",
                    "(u(s(yyyyyyyy)(yyyyyyyy)b))",
                    (profile.asusd_id, data))

    def reset_fan_curves(self, profile: Profile) -> None:
        self._need(ASUSD)
        self.t.call(ASUSD, PLATFORM_PATH, FANCURVES_IFACE, "SetCurvesToDefaults", "(u)",
                    (profile.asusd_id,))

    def get_fan_curves(self, profile: Profile | None = None) -> dict[str, FanCurve]:
        prof = profile or self.get_profile()
        res = self.t.call(ASUSD, PLATFORM_PATH, FANCURVES_IFACE, "FanCurveData", "(u)",
                          (prof.asusd_id,))[0]
        # asusd tuple order: (fan, pwm[8], temp[8], enabled)
        return {name: FanCurve.from_lists(name, list(temps), list(pwms), bool(en))
                for name, pwms, temps, en in res}
