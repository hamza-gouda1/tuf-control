"""The built-in software effects."""
from __future__ import annotations

import math

from .base import Effect, Param
from .colour import RGB, gradient, lerp_rgb, scale

BORDEAUX = "#8e2a44"


class TemperatureEffect(Effect):
    name, title = "temperature", "Temperature"
    description = "Cool blue at idle, gold when warm, red when hot."
    needs = ("telemetry",)
    max_fps = 8
    params = (
        Param("source", "choice", "max", "Sensor", choices=("cpu", "gpu", "max")),
        Param("low", "int", 45, "Cool at (°C)", 20, 90),
        Param("high", "int", 90, "Hot at (°C)", 40, 110),
        Param("cool", "colour", "#2f6fdf", "Cool colour"),
        Param("warm", "colour", "#c9ae86", "Warm colour"),
        Param("hot", "colour", "#e0182d", "Hot colour"),
    )

    def __init__(self, params=None, inputs=None) -> None:
        super().__init__(params, inputs)
        self.temp: float | None = None

    def reading(self) -> float | None:
        t = self.inputs.telemetry()
        if t is None:
            return None
        cpu, gpu = getattr(t, "cpu_temp_c", None), getattr(t, "gpu_temp_c", None)
        src = self.p["source"]
        if src == "cpu":
            return cpu
        if src == "gpu":
            return gpu if gpu is not None else cpu  # dGPU asleep: follow the CPU
        vals = [v for v in (cpu, gpu) if v is not None]
        return max(vals) if vals else None

    def tick(self, dt: float) -> RGB | None:
        r = self.reading()
        if r is not None:  # a little lag so a single spike does not flash red
            self.temp = r if self.temp is None else self.temp + (r - self.temp) * min(1, dt / 1.5)
        if self.temp is None:
            return None
        lo, hi = self.p["low"], max(self.p["high"], self.p["low"] + 1)
        return gradient([self.colour("cool"), self.colour("warm"), self.colour("hot")],
                        (self.temp - lo) / (hi - lo))


class AudioEffect(Effect):
    name, title = "audio", "Audio"
    description = "Pulses with the bass of whatever is playing."
    needs = ("audio",)
    note = "Reads the default output's monitor through parec or pw-record."
    params = (
        Param("band", "choice", "bass", "Follow", choices=("bass", "mid", "treble", "full")),
        Param("base", "colour", "#1a0a10", "Resting colour"),
        Param("peak", "colour", BORDEAUX, "Beat colour"),
        Param("gain", "float", 1.2, "Sensitivity", 0.2, 4.0, step=0.1),
    )

    def tick(self, dt: float) -> RGB | None:
        src = self.inputs.audio
        lvl = src.level(self.p["band"]) if src is not None else 0.0
        lvl = min(1.0, (lvl * self.p["gain"]) ** 1.5)
        return lerp_rgb(self.colour("base"), self.colour("peak"), lvl)


class AmbientEffect(Effect):
    name, title = "ambient", "Screen colour"
    description = "Average colour of the screen."
    needs = ("frames",)
    max_fps = 15
    note = ("GNOME asks once through the screen-sharing portal, then shows its sharing "
            "indicator in the top bar while this runs.")
    params = (Param("boost", "float", 1.3, "Saturation", 1.0, 2.5, step=0.1),)

    def tick(self, dt: float) -> RGB | None:
        src = self.inputs.frames
        return src.latest() if src is not None else None


class ProfileEffect(Effect):
    name, title = "profile", "Profile colours"
    description = "Colour follows the power profile."
    needs = ("profile",)
    max_fps = 8
    params = (
        Param("quiet", "colour", "#2f6fdf", "Quiet"),
        Param("balanced", "colour", "#c9ae86", "Balanced"),
        Param("performance", "colour", BORDEAUX, "Performance"),
    )

    def tick(self, dt: float) -> RGB | None:
        prof = self.inputs.profile()
        return self.colour(prof) if prof in ("quiet", "balanced", "performance") else None


class AccentEffect(Effect):
    name, title = "accent", "Accent"
    description = "A static or breathing accent colour."
    needs = ("theme",)
    max_fps = 20
    params = (
        Param("colour", "colour", BORDEAUX, "Colour"),
        Param("breathe", "bool", False, "Breathe"),
        Param("period", "float", 4.0, "Breath length (s)", 1.0, 20.0, step=0.5),
        Param("follow_theme", "bool", False, "Follow Bordeaux theme"),
    )

    def base(self) -> RGB:
        if self.p["follow_theme"]:
            c = self.inputs.theme_accent()
            if c is not None:
                return c
        return self.colour("colour")

    def tick(self, dt: float) -> RGB | None:
        self.t += dt
        c = self.base()
        if not self.p["breathe"]:
            return c
        k = 0.12 + 0.88 * (0.5 - 0.5 * math.cos(2 * math.pi * self.t / self.p["period"]))
        return scale(c, k)


class NotificationFlash(Effect):
    name, title = "notifications", "Notification flash"
    description = "Flashes on new desktop notifications, above any effect."
    needs = ("notifications",)
    overlay = True
    params = (
        Param("colour", "colour", "#ffffff", "Flash colour"),
        Param("flashes", "int", 2, "Flashes", 1, 6),
        Param("duration_ms", "int", 900, "Length (ms)", 200, 5000),
        Param("apps", "list", (), "Only these apps"),
    )

    def __init__(self, params=None, inputs=None) -> None:
        self.remaining = 0.0
        self.colour_now = None
        super().__init__(params, inputs)

    @property
    def max_fps(self) -> int:  # type: ignore[override]
        return 30 if self.remaining > 0 else 8

    def _wanted(self, ev: dict) -> bool:
        apps = [a.lower() for a in self.p["apps"]]
        return not apps or ev.get("app", "").lower() in apps

    def tick(self, dt: float) -> RGB | None:
        src = self.inputs.notifications
        if src is not None and any(self._wanted(e) for e in src.drain()):
            self.remaining = self.p["duration_ms"] / 1000
        if self.remaining <= 0:
            return None
        total = self.p["duration_ms"] / 1000
        self.remaining = max(0.0, self.remaining - dt)
        phase = (total - self.remaining) / total * self.p["flashes"]
        k = math.sin(math.pi * (phase % 1.0))
        if self.remaining <= 0:
            return None
        return scale(self.colour("colour"), max(0.0, k))


ALL = (TemperatureEffect, AudioEffect, AmbientEffect, ProfileEffect, AccentEffect)
OVERLAYS = (NotificationFlash,)
