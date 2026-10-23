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

    def _write(self, cap: Capability, *args, dry_run: bool = False, detail: str = "") -> WriteResult:
        method = OPERATIONS[cap][1]
        assert method
        errors = []
        for b in self.providers(cap):
            if dry_run:
                return WriteResult(False, b.name, f"dry run: would {detail}")
            try:
                getattr(b, method)(*args)
                return WriteResult(True, b.name, detail)
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
