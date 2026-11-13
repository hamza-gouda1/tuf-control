# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Small colour helpers shared by effects, rules and the UI (no GTK)."""
from __future__ import annotations

import colorsys
import math

from ..core import ValidationError

RGB = tuple[int, int, int]


def clamp8(v: float) -> int:
    return 0 if v <= 0 else 255 if v >= 255 else int(round(v))


def hex_to_rgb(text) -> RGB:
    if isinstance(text, (list, tuple)) and len(text) == 3:
        if all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in text):
            return tuple(text)  # type: ignore[return-value]
        raise ValidationError(f"colour {text!r}: need three integers 0-255")
    if not isinstance(text, str):
        raise ValidationError(f"colour {text!r}: expected '#rrggbb'")
    s = text.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValidationError(f"colour {text!r}: expected '#rrggbb'")
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        raise ValidationError(f"colour {text!r}: expected '#rrggbb'") from None


def rgb_to_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(clamp8(c) for c in rgb))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_rgb(a, b, t: float) -> RGB:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (clamp8(lerp(a[0], b[0], t)), clamp8(lerp(a[1], b[1], t)), clamp8(lerp(a[2], b[2], t)))


def gradient(stops: list[RGB], t: float) -> RGB:
    """Evenly spaced stops, t in 0..1."""
    if len(stops) == 1:
        return tuple(stops[0])  # type: ignore[return-value]
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    return lerp_rgb(stops[i], stops[i + 1], pos - i)


def scale(rgb, k: float) -> RGB:
    return (clamp8(rgb[0] * k), clamp8(rgb[1] * k), clamp8(rgb[2] * k))


def boost_saturation(rgb, k: float) -> RGB:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * k), v)
    return (clamp8(r * 255), clamp8(g * 255), clamp8(b * 255))


def distance(a, b) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class Smoother:
    """Exponential approach towards a target colour; frame-rate independent."""

    def __init__(self, tau_s: float = 0.25) -> None:
        self.tau = max(0.0, tau_s)
        self.value: list[float] | None = None

    def step(self, target, dt: float) -> RGB:
        if self.value is None or self.tau == 0:
            self.value = [float(c) for c in target]
        else:
            k = 1 - math.exp(-max(dt, 0.0) / self.tau)
            self.value = [v + (t - v) * k for v, t in zip(self.value, target)]
        return (clamp8(self.value[0]), clamp8(self.value[1]), clamp8(self.value[2]))
