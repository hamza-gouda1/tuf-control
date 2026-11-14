# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum, StrEnum

from .errors import ValidationError


class Capability(StrEnum):
    PROFILE = "profile"
    POWER_LIMITS = "power_limits"
    FIRMWARE_ATTRIBUTES = "firmware_attributes"
    BATTERY_THRESHOLD = "battery_threshold"
    BATTERY_HEALTH = "battery_health"
    GPU_MODE = "gpu_mode"
    KBD_BRIGHTNESS = "kbd_brightness"
    KBD_RGB = "kbd_rgb"
    FAN_CURVES = "fan_curves"
    TELEMETRY = "telemetry"


@dataclass(frozen=True)
class CapStatus:
    available: bool
    reason: str = ""


class Profile(StrEnum):
    QUIET = "quiet"
    BALANCED = "balanced"
    PERFORMANCE = "performance"

    @classmethod
    def parse(cls, text: str) -> Profile:
        key = str(text).strip().lower().replace("_", "-")
        aliases = {
            "low-power": cls.QUIET, "power-saver": cls.QUIET, "cool": cls.QUIET,
            "silent": cls.QUIET, "quiet": cls.QUIET,
            "balanced": cls.BALANCED, "balanced-performance": cls.BALANCED,
            "performance": cls.PERFORMANCE,
        }
        try:
            return aliases[key]
        except KeyError:
            raise ValidationError(f"unknown profile {text!r}") from None

    @property
    def asusd_id(self) -> int:
        return {Profile.BALANCED: 0, Profile.PERFORMANCE: 1, Profile.QUIET: 2}[self]

    @classmethod
    def from_asusd_id(cls, ident: int) -> Profile:
        try:
            return {0: cls.BALANCED, 1: cls.PERFORMANCE, 2: cls.QUIET}[ident]
        except KeyError:
            raise ValidationError(f"unknown asusd profile id {ident}") from None

    @property
    def ppd_name(self) -> str:
        return {Profile.QUIET: "power-saver"}.get(self, self.value)

    @property
    def cli_name(self) -> str:
        return self.value.capitalize()


class GpuMode(IntEnum):
    """supergfxd GfxMode numbering."""

    HYBRID = 0
    INTEGRATED = 1
    NVIDIA_NO_MODESET = 2
    VFIO = 3
    ASUS_EGPU = 4
    ASUS_MUX_DGPU = 5
    NONE = 6


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass
class PowerLimit:
    """An integer firmware attribute. The attribute's own min/max are HARD bounds."""

    name: str
    current: int | None
    min: int | None
    max: int | None
    default: int | None = None
    increment: int = 1
    display_name: str = ""

    @property
    def bounded(self) -> bool:
        return self.min is not None and self.max is not None and self.min <= self.max

    def clamp(self, value: int) -> int:
        if not _is_int(value):
            raise ValidationError(f"{self.name}: value must be an integer")
        if not self.bounded:
            raise ValidationError(f"{self.name}: firmware bounds unknown, refusing to guess")
        return max(self.min, min(self.max, value))  # type: ignore[type-var]

    def validate(self, value: int) -> int:
        if not _is_int(value):
            raise ValidationError(f"{self.name}: value must be an integer")
        if not self.bounded:
            raise ValidationError(f"{self.name}: firmware bounds unknown, refusing to write")
        assert self.min is not None and self.max is not None
        if not self.min <= value <= self.max:
            raise ValidationError(f"{self.name}: {value} outside [{self.min}, {self.max}]")
        if self.increment > 1 and (value - self.min) % self.increment:
            raise ValidationError(f"{self.name}: {value} not a multiple of step {self.increment}")
        return value


@dataclass
class FirmwareAttribute:
    """Any asus-armoury attribute (toggles, read-only info, integers)."""

    name: str
    type: str
    current: int | str | None
    possible_values: list[str] = field(default_factory=list)
    display_name: str = ""


@dataclass(frozen=True)
class FanPoint:
    temp: int
    pwm: int


@dataclass
class FanCurve:
    N_POINTS = 8
    TEMP_RANGE = (20, 110)
    PWM_RANGE = (0, 255)

    fan: str
    points: list[FanPoint]
    enabled: bool = True

    @classmethod
    def from_lists(cls, fan: str, temps: list[int], pwms: list[int], enabled: bool = True):
        if len(temps) != len(pwms):
            raise ValidationError("temps and pwms differ in length")
        return cls(fan, [FanPoint(int(t), int(p)) for t, p in zip(temps, pwms)], enabled)

    def validate(self) -> FanCurve:
        if len(self.points) != self.N_POINTS:
            raise ValidationError(f"{self.fan}: need exactly {self.N_POINTS} points")
        lo_t, hi_t = self.TEMP_RANGE
        lo_p, hi_p = self.PWM_RANGE
        prev: FanPoint | None = None
        for i, p in enumerate(self.points, 1):
            if not (_is_int(p.temp) and _is_int(p.pwm)):
                raise ValidationError(f"{self.fan}: point {i} must be integers")
            if not lo_t <= p.temp <= hi_t:
                raise ValidationError(f"{self.fan}: point {i} temp {p.temp} outside {lo_t}-{hi_t}")
            if not lo_p <= p.pwm <= hi_p:
                raise ValidationError(f"{self.fan}: point {i} pwm {p.pwm} outside {lo_p}-{hi_p}")
            if prev and p.temp <= prev.temp:
                raise ValidationError(f"{self.fan}: temps must strictly increase (point {i})")
            if prev and p.pwm < prev.pwm:
                raise ValidationError(f"{self.fan}: pwm must not decrease (point {i})")
            prev = p
        return self


@dataclass
class BatteryHealth:
    energy_full_uwh: int
    energy_full_design_uwh: int
    cycle_count: int | None = None

    @property
    def wear_percent(self) -> float:
        if self.energy_full_design_uwh <= 0:
            raise ValidationError("design energy must be positive")
        wear = (1 - self.energy_full_uwh / self.energy_full_design_uwh) * 100
        return round(max(0.0, wear), 1)

    @property
    def health_percent(self) -> float:
        return round(100 - self.wear_percent, 1)


@dataclass
class Telemetry:
    timestamp: float = field(default_factory=time.time)
    cpu_temp_c: float | None = None
    fan_rpm: dict[str, int] = field(default_factory=dict)
    gpu_temp_c: float | None = None
    gpu_power_w: float | None = None
    gpu_util_pct: float | None = None
    battery_power_w: float | None = None
    battery_status: str | None = None
    battery_percent: int | None = None
    profile: Profile | None = None
    cpu_power_w: float | None = None
    gpu_runtime: str | None = None  # dGPU runtime PM state: "active", "suspended", ...


class GpuAction(IntEnum):
    """supergfxd GfxUserAction numbering (what the user must do after a switch)."""

    LOGOUT = 0
    REBOOT = 1
    SWITCH_TO_INTEGRATED = 2
    ASUS_EGPU_DISABLE = 3
    NOTHING = 4


@dataclass
class GpuStatus:
    mode: GpuMode
    supported: list[GpuMode] = field(default_factory=list)
    pending_mode: GpuMode | None = None
    pending_action: GpuAction | None = None
    dgpu_power: str | None = None  # "active", "suspended", "off", ...


@dataclass
class KbdEffect:
    """asusd LedModeData: (mode, zone, colour1, colour2, speed, direction)."""

    mode: int
    zone: int = 0
    colour1: tuple[int, int, int] = (142, 42, 68)
    colour2: tuple[int, int, int] = (0, 0, 0)
    speed: int = 1
    direction: int = 0

    def validate(self) -> KbdEffect:
        for name in ("mode", "zone", "speed", "direction"):
            v = getattr(self, name)
            if not _is_int(v) or v < 0:
                raise ValidationError(f"keyboard effect {name} must be a non-negative integer")
        for name in ("colour1", "colour2"):
            c = tuple(getattr(self, name))
            if len(c) != 3 or not all(_is_int(x) and 0 <= x <= 255 for x in c):
                raise ValidationError(f"keyboard effect {name} must be three values 0-255")
        return self


AURA_MODES: dict[int, str] = {
    0: "Static", 1: "Breathe", 2: "Colour cycle", 3: "Rainbow wave", 4: "Star", 5: "Rain",
    6: "Highlight", 7: "Laser", 8: "Ripple", 10: "Pulse", 11: "Comet", 12: "Flash",
}


def jsonable(obj):
    """Convert models (dataclasses, enums, properties of interest) to JSON-safe data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        for extra in ("bounded", "wear_percent", "health_percent"):
            if hasattr(type(obj), extra):
                data[extra] = getattr(obj, extra)
        return data
    if isinstance(obj, Enum):
        return obj.name.lower() if isinstance(obj, IntEnum) else obj.value
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj
