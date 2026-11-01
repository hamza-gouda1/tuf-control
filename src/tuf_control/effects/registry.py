# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Effect registry and the `effects` section of the config."""
from __future__ import annotations

import copy

from ..core import ValidationError
from .plugins import ALL, OVERLAYS, NotificationFlash

EFFECTS = {cls.name: cls for cls in ALL}
OVERLAY_EFFECTS = {cls.name: cls for cls in OVERLAYS}


def coerce_params(name: str, raw: dict) -> dict:
    """Validate and convert parameters (strings from the CLI are accepted)."""
    cls = EFFECTS.get(name) or OVERLAY_EFFECTS.get(name)
    if cls is None:
        raise ValidationError(f"unknown effect {name!r} (one of {', '.join(EFFECTS)})")
    if not isinstance(raw, dict):
        raise ValidationError(f"{name}: parameters must be an object")
    specs = {p.key: p for p in cls.params}
    out = {}
    for k, v in raw.items():
        if k not in specs:
            raise ValidationError(f"{name}: unknown parameter {k!r} "
                                  f"(have {', '.join(specs) or 'none'})")
        out[k] = specs[k].coerce(v)
    return out


def parse_kv(pairs: list[str]) -> dict:
    out = {}
    for item in pairs:
        k, sep, v = item.partition("=")
        if not sep or not k.strip():
            raise ValidationError(f"parameter {item!r}: expected key=value")
        out[k.strip()] = v.strip()
    return out


def validate_effects_section(sec, defaults: dict) -> dict:
    from ..automation.config import ConfigError
    if not isinstance(sec, dict):
        raise ConfigError("effects: expected an object")
    out = copy.deepcopy(defaults)
    bad = set(sec) - set(defaults)
    if bad:
        raise ConfigError(f"effects: unknown keys {sorted(bad)}")
    try:
        active = sec.get("active")
        if active is not None and active not in EFFECTS:
            raise ValidationError(f"effects.active: unknown effect {active!r}")
        out["active"] = active
        for key, lo, hi, typ in (("fps", 1, 60, int), ("max_write_hz", 0.5, 30, float),
                                 ("smoothing_s", 0, 5, float)):
            if key in sec:
                v = sec[key]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                    raise ValidationError(f"effects.{key}: expected {lo}-{hi}")
                out[key] = typ(v)
        params = sec.get("params", {})
        if not isinstance(params, dict):
            raise ValidationError("effects.params: expected an object")
        for n in params:
            if n not in EFFECTS:
                raise ValidationError(f"effects.params: unknown effect {n!r}")
        out["params"] = {n: coerce_params(n, p) for n, p in params.items()}
        notif = sec.get("notifications", {})
        if not isinstance(notif, dict):
            raise ValidationError("effects.notifications: expected an object")
        enabled = notif.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValidationError("effects.notifications.enabled: expected true/false")
        rest = {k: v for k, v in notif.items() if k != "enabled"}
        merged = {**{k: v for k, v in defaults["notifications"].items() if k != "enabled"},
                  **coerce_params(NotificationFlash.name, rest)}
        out["notifications"] = {"enabled": enabled, **merged}
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(str(exc)) from None
    return out


def describe_all() -> list[dict]:
    return [cls.schema() for cls in (*ALL, *OVERLAYS)]
