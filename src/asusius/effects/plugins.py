# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The built-in software effects."""
from __future__ import annotations

import colorsys
import math

from ..brand_palette import ACCENTS, HEX_PALETTES
from .base import Effect, Param
from .colour import RGB, gradient, lerp_rgb, scale

BORDEAUX = ACCENTS["bordeaux"].lower()
BRAND_GOLD = HEX_PALETTES["dark"]["gold"].lower()


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
        Param("warm", "colour", BRAND_GOLD, "Warm colour"),
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
    name, title = "audio", "Music beat"
    description = "Pulses with the beat of whatever is playing."
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
    name, title = "ambient", "Screen ambient"
    description = "Follows the average colour of the screen."
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
    description = "Colour follows the power profile: Performance (game mode) stands out."
    needs = ("profile",)
    max_fps = 8
    params = (
        Param("quiet", "colour", "#2f6fdf", "Quiet"),
        Param("balanced", "colour", BRAND_GOLD, "Balanced"),
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


class SpectrumEffect(Effect):
    name, title = "spectrum", "Music spectrum"
    description = ("Hue follows the music's balance (bass warm, treble cool), "
                   "brightness its loudness.")
    needs = ("audio",)
    note = "Reads the default output's monitor through parec or pw-record."
    params = (
        Param("style", "choice", "bands", "Style", choices=("bands", "meter")),
        Param("bass", "colour", "#e0182d", "Bass colour"),
        Param("mid", "colour", BRAND_GOLD, "Mid colour"),
        Param("treble", "colour", "#2f6fdf", "Treble colour"),
        Param("gain", "float", 1.2, "Sensitivity", 0.2, 4.0, step=0.1),
        Param("floor", "float", 0.08, "Resting level", 0.0, 0.6, step=0.02),
    )

    def tick(self, dt: float) -> RGB | None:
        src = self.inputs.audio
        lv = {b: (src.level(b) if src is not None else 0.0) for b in ("bass", "mid", "treble")}
        stops = [self.colour("bass"), self.colour("mid"), self.colour("treble")]
        g, floor = self.p["gain"], self.p["floor"]
        if self.p["style"] == "meter":  # a VU meter: louder walks bass -> mid -> treble colour
            loud = min(1.0, (max(lv.values()) * g) ** 1.3)
            return scale(gradient(stops, loud), floor + (1 - floor) * loud)
        total = sum(lv.values())
        if total <= 1e-6:
            return scale(stops[0], floor)
        mix = [sum(c[i] * lv[b] for c, b in zip(stops, lv)) / total for i in range(3)]
        loud = min(1.0, (max(lv.values()) * g) ** 1.5)
        return scale(mix, floor + (1 - floor) * loud)


def two_colour_breathe(a, b, t: float, period: float, dip: float = 0.0) -> RGB:
    """Colour A at t=0, colour B at half a period, back to A: a cosine ease, so each colour
    lingers a moment. `dip` darkens the crossing (0 = a pure gradient, 1 = through black
    like a classic breathe). Shared with the in-app preview of the hardware Breathe."""
    k = 0.5 - 0.5 * math.cos(2 * math.pi * ((t / max(period, 1e-6)) % 1.0))
    return scale(lerp_rgb(a, b, k), 1.0 - max(0.0, min(1.0, dip)) * math.sin(math.pi * k))


class GradientBreatheEffect(Effect):
    name, title = "gradient", "Gradient breathing"
    description = "Fades smoothly Colour 1 ↔ Colour 2 and back, on any keyboard."
    note = ("Runs through asusd like the other software effects, so it also gives a "
            "single-colour keyboard (TUF) a real two-colour breathe.")
    max_fps = 20
    params = (
        Param("colour1", "colour", BORDEAUX, "Colour 1"),
        Param("colour2", "colour", BRAND_GOLD, "Colour 2"),
        Param("period", "float", 4.0, "Cycle length (s)", 1.5, 30.0, step=0.5),
        Param("dip", "float", 0.3, "Dim between colours", 0.0, 1.0, step=0.05),
    )

    def tick(self, dt: float) -> RGB | None:
        self.t += dt
        return two_colour_breathe(self.colour("colour1"), self.colour("colour2"), self.t,
                                  self.p["period"], self.p["dip"])


class RainbowEffect(Effect):
    name, title = "rainbow", "Rainbow"
    description = "A slow walk round the colour wheel."
    max_fps = 15
    params = (
        Param("period", "float", 20.0, "Cycle length (s)", 4.0, 300.0, step=1.0),
        Param("saturation", "float", 1.0, "Saturation", 0.2, 1.0, step=0.05),
    )

    def tick(self, dt: float) -> RGB | None:
        self.t += dt
        h = (self.t / self.p["period"]) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, self.p["saturation"], 1.0)
        return (round(r * 255), round(g * 255), round(b * 255))


class CpuLoadEffect(Effect):
    name, title = "cpu", "CPU load"
    description = "Calm when idle, warmer as the processor gets busy."
    needs = ("cpu_load",)
    max_fps = 8
    params = (
        Param("low", "int", 5, "Idle below (%)", 0, 90),
        Param("high", "int", 85, "Busy at (%)", 10, 100),
        Param("idle", "colour", "#2f6fdf", "Idle colour"),
        Param("busy", "colour", BRAND_GOLD, "Busy colour"),
        Param("max", "colour", "#e0182d", "Flat-out colour"),
    )

    def __init__(self, params=None, inputs=None) -> None:
        super().__init__(params, inputs)
        self.load: float | None = None

    def tick(self, dt: float) -> RGB | None:
        v = self.inputs.cpu_load()
        if v is not None:  # ~1 s lag: compiles and page loads read as load, not as flicker
            self.load = v if self.load is None else self.load + (v - self.load) * min(1, dt)
        if self.load is None:
            return None
        lo = self.p["low"] / 100
        hi = max(self.p["high"] / 100, lo + 0.01)
        return gradient([self.colour("idle"), self.colour("busy"), self.colour("max")],
                        (self.load - lo) / (hi - lo))


class NightEffect(Effect):
    name, title = "night", "Night warmth"
    description = "Day colour by day; fades to a dim warm amber in the evening."
    max_fps = 2
    params = (
        Param("day", "colour", BORDEAUX, "Day colour"),
        Param("night", "colour", "#ff8a3d", "Night colour"),
        Param("start", "int", 21, "Evening starts (hour)", 0, 23),
        Param("end", "int", 7, "Morning (hour)", 0, 23),
        Param("fade", "int", 45, "Fade (min)", 0, 180),
        Param("level", "float", 0.35, "Night brightness", 0.05, 1.0, step=0.05),
    )

    def night_amount(self) -> float:
        """0 by day, 1 at night, a linear fade of `fade` minutes after start/before end."""
        now = self.inputs.now()
        m = now.hour * 60 + now.minute + now.second / 60
        start, end, fade = self.p["start"] * 60, self.p["end"] * 60, max(self.p["fade"], 0)
        span = (end - start) % 1440 or 1440
        since_start = (m - start) % 1440
        if since_start >= span:  # daytime
            return 0.0
        to_end = span - since_start
        k = 1.0 if not fade else min(1.0, since_start / fade, to_end / fade)
        return max(0.0, k)

    def tick(self, dt: float) -> RGB | None:
        k = self.night_amount()
        return scale(lerp_rgb(self.colour("day"), self.colour("night"), k),
                     1 - (1 - self.p["level"]) * k)


ALL = (AudioEffect, SpectrumEffect, AmbientEffect, GradientBreatheEffect, RainbowEffect,
       TemperatureEffect, CpuLoadEffect, ProfileEffect, NightEffect, AccentEffect)
OVERLAYS = (NotificationFlash,)
