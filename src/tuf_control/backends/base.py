from __future__ import annotations

from abc import ABC, abstractmethod

from ..core import (
    BatteryHealth,
    Capability,
    CapStatus,
    FanCurve,
    FirmwareAttribute,
    GpuMode,
    PowerLimit,
    Profile,
    Telemetry,
    Unsupported,
)

# Capability -> (reader method, writer method or None)
OPERATIONS: dict[Capability, tuple[str, str | None]] = {
    Capability.PROFILE: ("get_profile", "set_profile"),
    Capability.POWER_LIMITS: ("get_limits", "set_limit"),
    Capability.FIRMWARE_ATTRIBUTES: ("get_attributes", None),
    Capability.BATTERY_THRESHOLD: ("get_battery_threshold", "set_battery_threshold"),
    Capability.BATTERY_HEALTH: ("get_battery_health", None),
    Capability.GPU_MODE: ("get_gpu_mode", None),
    Capability.KBD_BRIGHTNESS: ("get_kbd_brightness", "set_kbd_brightness"),
    Capability.KBD_RGB: ("get_kbd_rgb", None),
    Capability.FAN_CURVES: ("get_fan_curves", None),
    Capability.TELEMETRY: ("get_telemetry", None),
}


class Backend(ABC):
    """Common interface. Operations a backend lacks raise Unsupported."""

    name = "base"

    @abstractmethod
    def probe(self) -> dict[Capability, CapStatus]:
        """Report, for every capability, whether this backend can serve it and why not."""

    def _no(self, what: str):
        raise Unsupported(f"{self.name}: {what} not supported")

    def get_profile(self) -> Profile:
        self._no("profile")

    def set_profile(self, profile: Profile) -> None:
        self._no("set profile")

    def get_limits(self) -> list[PowerLimit]:
        self._no("power limits")

    def set_limit(self, name: str, value: int) -> None:
        self._no("set power limit")

    def get_attributes(self) -> list[FirmwareAttribute]:
        self._no("firmware attributes")

    def get_battery_threshold(self) -> int:
        self._no("battery threshold")

    def set_battery_threshold(self, value: int) -> None:
        self._no("set battery threshold")

    def get_battery_health(self) -> BatteryHealth:
        self._no("battery health")

    def get_gpu_mode(self) -> GpuMode:
        self._no("gpu mode")

    def get_kbd_brightness(self) -> tuple[int, int]:
        """Return (current, maximum)."""
        self._no("keyboard brightness")

    def set_kbd_brightness(self, value: int) -> None:
        self._no("set keyboard brightness")

    def get_kbd_rgb(self) -> dict:
        self._no("keyboard rgb")

    def get_fan_curves(self) -> dict[str, FanCurve]:
        self._no("fan curves")

    def get_telemetry(self) -> Telemetry:
        self._no("telemetry")


def all_unavailable(reason: str) -> dict[Capability, CapStatus]:
    return {cap: CapStatus(False, reason) for cap in Capability}
