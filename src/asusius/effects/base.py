# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Effect plugin interface.

An effect is a class with a `name`, a `params` schema and `tick(dt) -> (r, g, b) | None`.
None means "nothing to show": the next layer down in the stack shows through (an overlay
such as the notification flash returns None between flashes). Effects read the world only
through `Inputs`, so tests feed them synthetic sensors, audio, frames and notifications.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ..core import ValidationError
from .colour import RGB, hex_to_rgb, rgb_to_hex


@dataclass(frozen=True)
class Param:
    key: str
    kind: str                  # colour | float | int | bool | choice | list
    default: object
    label: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    step: float | None = None

    def coerce(self, value):
        k = self.kind
        try:
            if k == "colour":
                return rgb_to_hex(hex_to_rgb(value))
            if k == "bool":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str) and value.lower() in ("1", "true", "on", "yes"):
                    return True
                if isinstance(value, str) and value.lower() in ("0", "false", "off", "no"):
                    return False
                raise ValueError
            if k == "list":
                if isinstance(value, str):
                    value = [x.strip() for x in value.split(",") if x.strip()]
                if not isinstance(value, (list, tuple)) or not all(
                        isinstance(x, str) and 0 < len(x) <= 128 for x in value):
                    raise ValueError
                return list(value)
            if k == "choice":
                if value not in self.choices:
                    raise ValueError
                return value
            if k == "int":
                if isinstance(value, bool):
                    raise ValueError
                v = int(value) if isinstance(value, str) else value
                if not isinstance(v, int):
                    raise ValueError
            elif k == "float":
                if isinstance(value, bool):
                    raise ValueError
                v = float(value)
            else:
                raise ValueError
        except (TypeError, ValueError, ValidationError):
            extra = f" (one of {', '.join(self.choices)})" if self.choices else ""
            raise ValidationError(f"parameter {self.key}: bad {k} value {value!r}{extra}") from None
        if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
            raise ValidationError(f"parameter {self.key}: {v} outside {self.min}-{self.max}")
        return v

    def schema(self) -> dict:
        d = {"kind": self.kind, "default": self.default, "label": self.label or self.key}
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.choices:
            d["choices"] = list(self.choices)
        return d


@dataclass
class Inputs:
    """What effects may look at. Every field is optional; missing ones read as None."""

    telemetry: Callable[[], object] = lambda: None
    profile: Callable[[], str | None] = lambda: None
    audio: object = None          # .level(band) -> 0..1   (effects.audio)
    frames: object = None         # .latest() -> RGB | None (effects.ambient)
    notifications: object = None  # .drain() -> list[dict] (effects.notify)
    theme_accent: Callable[[], RGB | None] = lambda: None
    cpu_load: Callable[[], float | None] = lambda: None   # 0..1 over all cores
    now: Callable[[], datetime] = datetime.now           # wall clock (night effect)
    extra: dict = field(default_factory=dict)


class Effect:
    name = ""
    title = ""
    description = ""
    params: tuple[Param, ...] = ()
    needs: tuple[str, ...] = ()   # inputs it needs: telemetry, profile, audio, frames, ...
    overlay = False               # True: sits above the chosen effect (notification flash)
    note = ""                     # capability caveat shown in the UI
    max_fps = 30                  # frames per second this effect can use

    def __init__(self, params: dict | None = None, inputs: Inputs | None = None) -> None:
        self.p = {pp.key: pp.default for pp in self.params}
        for k, v in (params or {}).items():
            spec = next((pp for pp in self.params if pp.key == k), None)
            if spec is None:
                raise ValidationError(f"{self.name}: unknown parameter {k!r}")
            self.p[k] = spec.coerce(v)
        self.inputs = inputs or Inputs()
        self.t = 0.0

    def colour(self, key: str) -> RGB:
        return hex_to_rgb(self.p[key])

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def tick(self, dt: float) -> RGB | None:  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def schema(cls) -> dict:
        return {"name": cls.name, "title": cls.title, "description": cls.description,
                "overlay": cls.overlay, "note": cls.note,
                "params": {p.key: p.schema() for p in cls.params}}


class EffectStack:
    """Layers in priority order (highest first); the first non-None colour wins. Every
    layer ticks every frame so time-based effects stay in step while covered."""

    def __init__(self, layers: list[tuple[int, Effect]] | None = None) -> None:
        self.layers: list[tuple[int, Effect]] = sorted(layers or [], key=lambda x: -x[0])

    def add(self, priority: int, effect: Effect) -> None:
        self.layers.append((priority, effect))
        self.layers.sort(key=lambda x: -x[0])

    def tick(self, dt: float) -> tuple[RGB | None, str | None]:
        out: RGB | None = None
        who: str | None = None
        for _prio, eff in self.layers:
            c = eff.tick(dt)
            if out is None and c is not None:
                out, who = c, eff.name
        return out, who

    def start(self) -> None:
        for _p, e in self.layers:
            e.start()

    def stop(self) -> None:
        for _p, e in self.layers:
            e.stop()
