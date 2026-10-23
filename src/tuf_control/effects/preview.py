"""In-app preview of a software effect: the real effect class fed with simulated inputs
(live telemetry and profile where the app has them, a synthetic beat for audio, a slow
colour drift for the screen). No capture, no portal, no keyboard writes."""
from __future__ import annotations

import math

from .base import Inputs
from .colour import RGB, Smoother, gradient
from .notify import NotificationQueue
from .registry import EFFECTS, OVERLAY_EFFECTS


class _Beat:
    def __init__(self) -> None:
        self.t = 0.0

    def level(self, band: str = "bass") -> float:
        beat = (self.t * 2.0) % 1.0  # 120 bpm
        return math.exp(-beat * 6.0) * (0.55 if band == "treble" else 0.95)


class _Drift:
    COLOURS = [(40, 90, 200), (190, 60, 120), (230, 160, 60), (60, 170, 120)]

    def __init__(self) -> None:
        self.t = 0.0

    def latest(self) -> RGB:
        return gradient(self.COLOURS + self.COLOURS[:1], (self.t / 16.0) % 1.0)


class EffectPreview:
    def __init__(self, telemetry=lambda: None, profile=lambda: None,
                 theme=lambda: None) -> None:
        self.beat, self.drift = _Beat(), _Drift()
        self.queue = NotificationQueue()
        self.inputs = Inputs(telemetry=telemetry, profile=profile, audio=self.beat,
                             frames=self.drift, notifications=self.queue, theme_accent=theme)
        self.effect = None
        self.overlay = None
        self.smoother = Smoother(0.15)
        self.colour: RGB | None = None

    def configure(self, name: str | None, params: dict, notif: dict | None = None) -> None:
        self.effect = EFFECTS[name](params, self.inputs) if name in EFFECTS else None
        n = dict(notif or {})
        self.overlay = OVERLAY_EFFECTS["notifications"](
            {k: v for k, v in n.items() if k != "enabled"}, self.inputs) \
            if n.get("enabled") else None

    def flash(self) -> None:
        self.queue.push({"app": "", "summary": "preview"})

    def tick(self, dt: float) -> RGB | None:
        self.beat.t += dt
        self.drift.t += dt
        rgb = None
        if self.overlay is not None:
            rgb = self.overlay.tick(dt)
        base = self.effect.tick(dt) if self.effect is not None else None
        rgb = rgb or base
        if rgb is None:
            return self.colour
        self.colour = self.smoother.step(rgb, dt)
        return self.colour
