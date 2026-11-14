# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Rule model: trigger -> actions, with validation, (de)serialisation and descriptions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core import Profile, ValidationError
from ..effects.colour import hex_to_rgb, rgb_to_hex

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TRIGGER_TYPES = ("power", "battery", "app", "schedule", "profile")
ACTION_TYPES = ("profile", "kbd_colour", "kbd_effect", "kbd_brightness", "charge_limit",
                "panel_overdrive", "ppd", "effect")
# action type -> conflict key (two actions with the same key fight over the same setting)
ACTION_KEYS = {"profile": "profile", "kbd_colour": "kbd", "kbd_effect": "kbd",
               "kbd_brightness": "kbd_brightness", "charge_limit": "charge_limit",
               "panel_overdrive": "panel_overdrive", "ppd": "ppd", "effect": "effect"}
PPD_NAMES = ("power-saver", "balanced", "performance")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class RuleError(ValidationError):
    pass


def _err(where: str, msg: str):
    raise RuleError(f"{where}: {msg}")


def _int(where, v, lo, hi) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        _err(where, "expected an integer")
    if not lo <= v <= hi:
        _err(where, f"{v} outside {lo}-{hi}")
    return v


def _keys(where, d: dict, allowed: set[str]) -> None:
    extra = set(d) - allowed
    if extra:
        _err(where, f"unknown keys {sorted(extra)}")


def parse_hhmm(text: str, where: str = "time") -> int:
    m = _TIME_RE.match(str(text).strip())
    if not m:
        _err(where, f"{text!r} is not HH:MM")
    return int(m.group(1)) * 60 + int(m.group(2))


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40]
    return s or "rule"


# -- triggers -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Trigger:
    type: str
    params: dict = field(default_factory=dict, hash=False, compare=True)

    @classmethod
    def from_dict(cls, d, where: str = "trigger") -> Trigger:
        if not isinstance(d, dict) or "type" not in d:
            _err(where, "expected an object with a 'type'")
        t = d["type"]
        p = {k: v for k, v in d.items() if k != "type"}
        if t == "power":
            _keys(where, p, {"source"})
            if p.get("source") not in ("ac", "battery"):
                _err(where, "source must be 'ac' or 'battery'")
        elif t == "battery":
            _keys(where, p, {"below", "above", "hysteresis"})
            if ("below" in p) == ("above" in p):
                _err(where, "give exactly one of 'below' or 'above'")
            for k in ("below", "above"):
                if k in p:
                    _int(f"{where}.{k}", p[k], 1, 99)
            p["hysteresis"] = _int(f"{where}.hysteresis", p.get("hysteresis", 3), 0, 20)
        elif t == "app":
            _keys(where, p, {"names"})
            names = p.get("names")
            if not isinstance(names, list) or not names or len(names) > 32:
                _err(where, "names must be a list of 1-32 executable names or desktop ids")
            clean = []
            for n in names:
                if not isinstance(n, str) or not 0 < len(n.strip()) <= 128:
                    _err(where, f"bad application name {n!r}")
                clean.append(n.strip())
            p["names"] = clean
        elif t == "schedule":
            _keys(where, p, {"days", "start", "end"})
            days = p.get("days", list(DAYS))
            if not isinstance(days, list) or not days or any(x not in DAYS for x in days):
                _err(where, f"days must be a non-empty subset of {', '.join(DAYS)}")
            p["days"] = [x for x in DAYS if x in days]
            s = parse_hhmm(p.get("start", ""), f"{where}.start")
            e = parse_hhmm(p.get("end", ""), f"{where}.end")
            if s == e:
                _err(where, "start and end must differ")
            p["start"] = f"{s // 60:02d}:{s % 60:02d}"
            p["end"] = f"{e // 60:02d}:{e % 60:02d}"
        elif t == "profile":
            _keys(where, p, {"value"})
            p["value"] = Profile.parse(p.get("value", "")).value
        else:
            _err(where, f"unknown trigger type {t!r} (one of {', '.join(TRIGGER_TYPES)})")
        return cls(t, p)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.params}

    def describe(self) -> str:
        p = self.params
        if self.type == "power":
            return "On AC power" if p["source"] == "ac" else "On battery"
        if self.type == "battery":
            return (f"Battery at or below {p['below']}%" if "below" in p
                    else f"Battery at or above {p['above']}%")
        if self.type == "app":
            names = p["names"]
            return f"{names[0]} is running" if len(names) == 1 else \
                f"Any of {', '.join(names[:3])}{'…' if len(names) > 3 else ''} running"
        if self.type == "schedule":
            d = p["days"]
            days = "Every day" if len(d) == 7 else "Weekdays" if d == list(DAYS[:5]) else \
                "Weekends" if d == ["sat", "sun"] else ", ".join(x.capitalize() for x in d)
            return f"{days} {p['start']}–{p['end']}"
        return f"Profile is {p['value'].capitalize()}"


# -- actions ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    type: str
    params: dict = field(default_factory=dict, hash=False)

    @property
    def key(self) -> str:
        return ACTION_KEYS[self.type]

    @classmethod
    def from_dict(cls, d, where: str = "action") -> Action:
        from ..effects.registry import EFFECTS, coerce_params
        if not isinstance(d, dict) or "type" not in d:
            _err(where, "expected an object with a 'type'")
        t = d["type"]
        p = {k: v for k, v in d.items() if k != "type"}
        try:
            if t == "profile":
                _keys(where, p, {"value"})
                p["value"] = Profile.parse(p.get("value", "")).value
            elif t == "kbd_colour":
                _keys(where, p, {"value"})
                p["value"] = rgb_to_hex(hex_to_rgb(p.get("value")))
            elif t == "kbd_effect":
                from ..core.models import AURA_MODES as AURA_MODE_NAMES
                _keys(where, p, {"mode", "colour", "colour2", "speed", "direction"})
                mode = p.get("mode")
                if isinstance(mode, str):
                    lookup = {v.lower(): k for k, v in AURA_MODE_NAMES.items()}
                    if mode.lower() not in lookup:
                        _err(where, f"unknown keyboard mode {mode!r}")
                    mode = lookup[mode.lower()]
                mode = _int(f"{where}.mode", mode, 0, max(AURA_MODE_NAMES))
                if mode not in AURA_MODE_NAMES:
                    _err(f"{where}.mode", f"unknown keyboard mode {mode!r}")
                p["mode"] = mode
                p["colour"] = rgb_to_hex(hex_to_rgb(p.get("colour", "#8e2a44")))
                p["colour2"] = rgb_to_hex(hex_to_rgb(p.get("colour2", "#000000")))
                p["speed"] = _int(f"{where}.speed", p.get("speed", 1), 0, 2)
                p["direction"] = _int(f"{where}.direction", p.get("direction", 0), 0, 3)
            elif t == "kbd_brightness":
                _keys(where, p, {"value"})
                p["value"] = _int(f"{where}.value", p.get("value"), 0, 65535)
            elif t == "charge_limit":
                _keys(where, p, {"value"})
                p["value"] = _int(f"{where}.value", p.get("value"), 20, 100)
            elif t == "panel_overdrive":
                _keys(where, p, {"value"})
                if not isinstance(p.get("value"), bool):
                    _err(where, "value must be true or false")
            elif t == "ppd":
                _keys(where, p, {"value"})
                v = p.get("value")
                if v not in PPD_NAMES:
                    v = Profile.parse(v).ppd_name if isinstance(v, str) else None
                if v not in PPD_NAMES:
                    _err(where, f"value must be one of {', '.join(PPD_NAMES)}")
                p["value"] = v
            elif t == "effect":
                _keys(where, p, {"name", "params"})
                name = p.get("name")
                if name in (None, "off", "none"):
                    p["name"], p["params"] = None, {}
                elif name not in EFFECTS:
                    _err(where, f"unknown effect {name!r} (one of {', '.join(EFFECTS)}, off)")
                else:
                    p["params"] = coerce_params(name, p.get("params") or {})
            else:
                _err(where, f"unknown action type {t!r} (one of {', '.join(ACTION_TYPES)})")
        except RuleError:
            raise
        except ValidationError as exc:
            _err(where, str(exc))
        return cls(t, p)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.params}

    def target_value(self):
        """The value written for this action's key (also the shape of a saved baseline)."""
        p = self.params
        if self.type == "kbd_colour":
            return {"mode": 0, "colour1": list(hex_to_rgb(p["value"]))}
        if self.type == "kbd_effect":
            return {"mode": p["mode"], "colour1": list(hex_to_rgb(p["colour"])),
                    "colour2": list(hex_to_rgb(p["colour2"])), "speed": p["speed"],
                    "direction": p["direction"]}
        if self.type == "effect":
            return {"name": p["name"], "params": dict(p["params"])}
        return p["value"]

    def describe(self) -> str:
        p = self.params
        if self.type == "profile":
            return f"{p['value'].capitalize()} profile"
        if self.type == "kbd_colour":
            return f"Keyboard {p['value']}"
        if self.type == "kbd_effect":
            from ..core.models import AURA_MODES as AURA_MODE_NAMES
            return f"Keyboard {AURA_MODE_NAMES.get(p['mode'], p['mode'])} {p['colour']}"
        if self.type == "kbd_brightness":
            names = ("off", "low", "medium", "high")
            v = p["value"]
            return f"Backlight {names[v] if 0 <= v < len(names) else v}"
        if self.type == "charge_limit":
            return f"Charge limit {p['value']}%"
        if self.type == "panel_overdrive":
            return f"Overdrive {'on' if p['value'] else 'off'}"
        if self.type == "ppd":
            return f"Power mode {p['value']}"
        if not p["name"]:
            return "Software effects off"
        from ..effects.registry import EFFECTS
        return f"{EFFECTS[p['name']].title} effect"


# -- rules --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    trigger: Trigger
    actions: tuple[Action, ...]
    enabled: bool = True
    priority: int = 50
    restore: bool = True
    debounce_s: float = 2.0

    @classmethod
    def from_dict(cls, d, where: str = "rule") -> Rule:
        if not isinstance(d, dict):
            _err(where, "expected an object")
        _keys(where, d, {"id", "name", "enabled", "priority", "restore", "debounce_s",
                         "trigger", "actions"})
        name = d.get("name") or ""
        if not isinstance(name, str) or len(name) > 80:
            _err(f"{where}.name", "expected text up to 80 characters")
        rid = d.get("id") or slugify(name)
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            _err(f"{where}.id", f"{rid!r} must be lower-case letters, digits, '-' or '_'")
        trig = Trigger.from_dict(d.get("trigger"), f"{where}.trigger")
        acts_raw = d.get("actions")
        if not isinstance(acts_raw, list) or not 1 <= len(acts_raw) <= 8:
            _err(f"{where}.actions", "need 1-8 actions")
        acts = tuple(Action.from_dict(a, f"{where}.actions[{i}]") for i, a in enumerate(acts_raw))
        keys = [a.key for a in acts]
        if len(set(keys)) != len(keys):
            _err(f"{where}.actions", "two actions change the same setting")
        if trig.type == "profile" and "profile" in keys:
            _err(where, "a profile trigger cannot also set the profile (it would loop)")
        for k in ("enabled", "restore"):
            if k in d and not isinstance(d[k], bool):
                _err(f"{where}.{k}", "expected true/false")
        prio = _int(f"{where}.priority", d.get("priority", 50), 0, 100)
        deb = d.get("debounce_s", 2.0)
        if isinstance(deb, bool) or not isinstance(deb, (int, float)) or not 0 <= deb <= 600:
            _err(f"{where}.debounce_s", "expected seconds 0-600")
        return cls(rid, name or rid, trig, acts, d.get("enabled", True), prio,
                   d.get("restore", True), float(deb))

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "enabled": self.enabled,
                "priority": self.priority, "restore": self.restore,
                "debounce_s": self.debounce_s, "trigger": self.trigger.to_dict(),
                "actions": [a.to_dict() for a in self.actions]}

    def describe(self) -> str:
        return f"{self.trigger.describe()} → {', '.join(a.describe() for a in self.actions)}"


# -- compact text forms for the CLI ---------------------------------------------------------
def parse_trigger_spec(spec: str) -> dict:
    """ac | battery | battery<20 | battery>80 | app=steam,gamescope |
    time=mon-fri@22:00-07:00 | time=22:00-07:00 | profile=quiet"""
    s = spec.strip()
    low = s.lower()
    if low in ("ac", "plugged", "power=ac"):
        return {"type": "power", "source": "ac"}
    if low in ("battery", "unplugged", "power=battery"):
        return {"type": "power", "source": "battery"}
    m = re.match(r"^battery\s*([<>])=?\s*(\d+)$", low)
    if m:
        return {"type": "battery", ("below" if m.group(1) == "<" else "above"): int(m.group(2))}
    key, _, val = s.partition("=")
    key = key.strip().lower()
    if key == "app" and val:
        return {"type": "app", "names": [x.strip() for x in val.split(",") if x.strip()]}
    if key == "profile" and val:
        return {"type": "profile", "value": val.strip()}
    if key in ("time", "schedule") and val:
        days_part, _, times = val.rpartition("@")
        start, _, end = times.partition("-")
        return {"type": "schedule", "days": _parse_days(days_part) if days_part else list(DAYS),
                "start": start.strip(), "end": end.strip()}
    raise RuleError(f"cannot parse trigger {spec!r}")


def _parse_days(text: str) -> list[str]:
    t = text.strip().lower()
    if t in ("", "daily", "all", "every"):
        return list(DAYS)
    if t == "weekdays":
        return list(DAYS[:5])
    if t == "weekends":
        return ["sat", "sun"]
    out: list[str] = []
    for part in t.split(","):
        a, _, b = part.partition("-")
        if a not in DAYS or (b and b not in DAYS):
            raise RuleError(f"bad day range {part!r}")
        i, j = DAYS.index(a), DAYS.index(b or a)
        rng = range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)]
        out.extend(DAYS[k] for k in rng)
    return out


def parse_action_spec(spec: str) -> dict:
    """profile=performance | kbd_colour=#ff0000 | kbd_effect=breathe:#ff0000 |
    kbd_brightness=2 | charge_limit=80 | panel_overdrive=on | ppd=power-saver |
    effect=temperature | effect=off"""
    key, _, val = spec.strip().partition("=")
    key = key.strip().lower().replace("-", "_")
    val = val.strip()
    if key == "kbd_color":
        key = "kbd_colour"
    if key not in ACTION_TYPES or not val:
        raise RuleError(f"cannot parse action {spec!r} (want type=value, types: "
                        f"{', '.join(ACTION_TYPES)})")
    if key in ("kbd_brightness", "charge_limit"):
        try:
            return {"type": key, "value": int(val)}
        except ValueError:
            raise RuleError(f"{key}: expected an integer") from None
    if key == "panel_overdrive":
        if val.lower() not in ("on", "off", "1", "0", "true", "false"):
            raise RuleError("panel_overdrive: expected on or off")
        return {"type": key, "value": val.lower() in ("on", "1", "true")}
    if key == "kbd_effect":
        mode, _, colour = val.partition(":")
        out = {"type": key, "mode": int(mode) if mode.isdigit() else mode}
        if colour:
            out["colour"] = colour
        return out
    if key == "effect":
        return {"type": key, "name": None if val.lower() in ("off", "none") else val}
    return {"type": key, "value": val}
