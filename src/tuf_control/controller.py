"""Controller facade: picks the best backend per capability (D-Bus -> sysfs -> CLI)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .backends.base import OPERATIONS, Backend
from .core import (
    BackendError,
    BackendUnavailable,
    Capability,
    CapStatus,
    FanCurve,
    FirmwareAttribute,
    GpuAction,
    GpuMode,
    GpuStatus,
    KbdEffect,
    PowerLimit,
    Profile,
    Unsupported,
    ValidationError,
)

FAKE_ROOT_ENV = "TUF_CONTROL_FAKE_ROOT"


@dataclass
class WriteResult:
    applied: bool
    backend: str
    detail: str
    value: object = None  # what the backend returned, e.g. the GpuAction after a switch


def default_backends() -> list[Backend]:
    """Real backends, or only the fake-tree backend when TUF_CONTROL_FAKE_ROOT is set."""
    fake = os.environ.get(FAKE_ROOT_ENV)
    if fake:
        from .backends.fake import FakeBackend
        return [FakeBackend(Path(fake))]
    from .backends.cli import CliBackend
    from .backends.dbus import DBusBackend
    from .backends.sysfs import SysfsBackend
    return [DBusBackend(), SysfsBackend("/"), CliBackend()]


class Controller:
    def __init__(self, backends: list[Backend] | None = None) -> None:
        self.backends = backends if backends is not None else default_backends()
        self._probes: dict[str, dict[Capability, CapStatus]] | None = None

    # -- capabilities -------------------------------------------------------
    def _probe_all(self) -> dict[str, dict[Capability, CapStatus]]:
        if self._probes is None:
            self._probes = {}
            for b in self.backends:
                try:
                    self._probes[b.name] = b.probe()
                except Exception as exc:  # a broken backend must not break discovery
                    self._probes[b.name] = {c: CapStatus(False, f"probe failed: {exc}")
                                            for c in Capability}
        return self._probes

    def invalidate(self) -> None:
        self._probes = None

    def providers(self, cap: Capability) -> list[Backend]:
        probes = self._probe_all()
        return [b for b in self.backends if probes[b.name][cap].available]

    def capabilities(self) -> dict[str, dict]:
        probes = self._probe_all()
        out = {}
        for cap in Capability:
            avail = [b.name for b in self.backends if probes[b.name][cap].available]
            out[cap.value] = {
                "available": bool(avail),
                "backend": avail[0] if avail else None,
                "fallbacks": avail[1:],
                "unavailable": {b.name: probes[b.name][cap].reason for b in self.backends
                                if not probes[b.name][cap].available},
                "notes": {b.name: probes[b.name][cap].reason for b in self.backends
                          if probes[b.name][cap].available and probes[b.name][cap].reason},
            }
        return out

    def why_unavailable(self, cap: Capability) -> str:
        reasons = self.capabilities()[cap.value]["unavailable"]
        return "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no backends configured"

    # -- dispatch -------------------------------------------------------------
    def read(self, cap: Capability, *args):
        """Return (value, backend_name); falls through on unavailable/failed backends."""
        method = OPERATIONS[cap][0]
        errors = []
        for b in self.providers(cap):
            try:
                return getattr(b, method)(*args), b.name
            except (BackendUnavailable, BackendError, Unsupported) as exc:
                errors.append(f"{b.name}: {exc}")
        raise BackendUnavailable(
            f"{cap.value} unavailable: " + ("; ".join(errors) or self.why_unavailable(cap)))

    def _write(self, cap: Capability, *args, dry_run: bool = False, detail: str = "",
               method: str | None = None, backends: list[Backend] | None = None) -> WriteResult:
        method = method or OPERATIONS[cap][1]
        assert method
        errors = []
        for b in (self.providers(cap) if backends is None else backends):
            if dry_run:
                return WriteResult(False, b.name, f"dry run: would {detail}")
            try:
                out = getattr(b, method)(*args)
                res = WriteResult(True, b.name, detail)
                if out is not None:
                    res.value = out
                return res
            except (BackendUnavailable, Unsupported) as exc:  # never after validation errors
                errors.append(f"{b.name}: {exc}")
        raise BackendUnavailable(
            f"cannot {detail}: " + ("; ".join(errors) or self.why_unavailable(cap)))

    # -- high level -----------------------------------------------------------
    def get_profile(self) -> tuple[Profile, str]:
        return self.read(Capability.PROFILE)

    def set_profile(self, profile: Profile, dry_run: bool = False) -> WriteResult:
        return self._write(Capability.PROFILE, profile, dry_run=dry_run,
                           detail=f"set profile {profile.value}")

    def get_limits(self) -> tuple[list[PowerLimit], str]:
        return self.read(Capability.POWER_LIMITS)

    def set_limit(self, name: str, value: int, dry_run: bool = False,
                  clamp: bool = False) -> WriteResult:
        limits, _ = self.get_limits()
        lim = next((x for x in limits if x.name == name), None)
        if lim is None:
            raise ValidationError(f"unknown power limit {name!r}")
        value = lim.clamp(value) if clamp else lim.validate(value)
        return self._write(Capability.POWER_LIMITS, name, value, dry_run=dry_run,
                           detail=f"set {name}={value}")

    def get_battery_threshold(self) -> tuple[int, str]:
        return self.read(Capability.BATTERY_THRESHOLD)

    def set_battery_threshold(self, value: int, dry_run: bool = False) -> WriteResult:
        if isinstance(value, bool) or not isinstance(value, int) or not 20 <= value <= 100:
            raise ValidationError("battery threshold must be an integer in 20-100")
        return self._write(Capability.BATTERY_THRESHOLD, value, dry_run=dry_run,
                           detail=f"set battery charge limit {value}")

    # -- keyboard -------------------------------------------------------------
    def get_kbd_brightness(self) -> tuple[tuple[int, int], str]:
        return self.read(Capability.KBD_BRIGHTNESS)

    def set_kbd_brightness(self, value: int, dry_run: bool = False) -> WriteResult:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("keyboard brightness must be a non-negative integer")
        return self._write(Capability.KBD_BRIGHTNESS, value, dry_run=dry_run,
                           detail=f"set keyboard brightness {value}")

    def get_kbd_rgb(self) -> tuple[dict, str]:
        return self.read(Capability.KBD_RGB)

    def set_kbd_effect(self, effect: KbdEffect, dry_run: bool = False,
                       verify: bool = False) -> WriteResult:
        """verify: read the mode back and fail loudly when the daemon did not take it
        (the UI's Apply uses it; the software-effect writer does not, at 10 writes/s)."""
        effect.validate()
        res = self._write(Capability.KBD_RGB, effect, dry_run=dry_run, method="set_kbd_effect",
                          detail=f"set keyboard effect mode {effect.mode}")
        if verify and res.applied:
            got = (self.get_kbd_rgb()[0] or {}).get("mode_data") or {}
            want = {"mode": effect.mode, "colour1": list(effect.colour1)}
            have = {"mode": got.get("mode"), "colour1": list(got.get("colour1") or [])}
            if have != want:
                raise BackendError(f"{res.backend} accepted the keyboard effect but reports "
                                   f"mode {have['mode']} colour {have['colour1']} instead of "
                                   f"mode {want['mode']} colour {want['colour1']}")
        return res

    # -- fans -------------------------------------------------------------------
    def get_fan_curves(self, profile: Profile | None = None) -> tuple[dict[str, FanCurve], str]:
        return self.read(Capability.FAN_CURVES, *(() if profile is None else (profile,)))

    def set_fan_curve(self, profile: Profile, curve: FanCurve,
                      dry_run: bool = False) -> WriteResult:
        curve.validate()
        return self._write(Capability.FAN_CURVES, profile, curve, dry_run=dry_run,
                           method="set_fan_curve",
                           detail=f"set {curve.fan} fan curve for {profile.value}")

    def reset_fan_curves(self, profile: Profile, dry_run: bool = False) -> WriteResult:
        return self._write(Capability.FAN_CURVES, profile, dry_run=dry_run,
                           method="reset_fan_curves",
                           detail=f"reset fan curves for {profile.value}")

    # -- gpu --------------------------------------------------------------------
    def get_gpu_status(self) -> tuple[GpuStatus, str]:
        errors = []
        for b in self.providers(Capability.GPU_MODE):
            try:
                return b.get_gpu_status(), b.name
            except (BackendUnavailable, BackendError, Unsupported) as exc:
                errors.append(f"{b.name}: {exc}")
        raise BackendUnavailable("gpu status unavailable: " + (
            "; ".join(errors) or self.why_unavailable(Capability.GPU_MODE)))

    def set_gpu_mode(self, mode: GpuMode, dry_run: bool = False) -> WriteResult:
        if not isinstance(mode, GpuMode):
            raise ValidationError("unknown GPU mode")
        res = self._write(Capability.GPU_MODE, mode, dry_run=dry_run, method="set_gpu_mode",
                          detail=f"switch GPU mode to {mode.name.lower()}")
        if res.applied and res.value is None:
            res.value = GpuAction.NOTHING
        return res

    # -- firmware attributes ------------------------------------------------------
    def get_attributes(self) -> tuple[list[FirmwareAttribute], str]:
        return self.read(Capability.FIRMWARE_ATTRIBUTES)

    def set_attribute(self, name: str, value: int | str, dry_run: bool = False) -> WriteResult:
        # every backend is tried in order: asusd can write what sysfs needs root for
        return self._write(Capability.FIRMWARE_ATTRIBUTES, name, value, dry_run=dry_run,
                           method="set_attribute", backends=list(self.backends),
                           detail=f"set {name}={value}")

    def one_shot_full_charge(self, dry_run: bool = False) -> WriteResult:
        return self._write(Capability.BATTERY_THRESHOLD, dry_run=dry_run,
                           method="one_shot_full_charge", detail="charge once to 100%")

    def cross_check(self) -> list[dict]:
        """Read every capability from every available backend and report disagreements."""
        from .core import jsonable
        rows = []
        for cap in Capability:
            if cap in (Capability.TELEMETRY, Capability.KBD_RGB, Capability.FIRMWARE_ATTRIBUTES):
                continue
            vals = {}
            for b in self.providers(cap):
                try:
                    v = getattr(b, OPERATIONS[cap][0])()
                except Exception as exc:
                    vals[b.name] = f"error: {exc}"
                    continue
                if cap is Capability.POWER_LIMITS:
                    v = {x.name: [x.current, x.min, x.max] for x in v}
                vals[b.name] = jsonable(v)
            if len(vals) > 1:
                same = len({repr(v) for v in vals.values()}) == 1
                rows.append({"capability": cap.value, "values": vals, "agree": same})
        return rows
