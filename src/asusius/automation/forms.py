# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free helpers for the Automation page and rule dialog (unit-tested, no GTK)."""
from __future__ import annotations

from .rules import DAYS, Rule

# (key, title, icon) - the trigger picker. "power-ac"/"power-battery" are two faces of
# the power trigger, which reads better than a nested choice.
TRIGGERS = [
    ("power-ac", "Plugged in", "ac-adapter-symbolic"),
    ("power-battery", "On battery", "battery-level-50-symbolic"),
    ("battery", "Battery level", "battery-level-20-symbolic"),
    ("app", "App is running", "view-app-grid-symbolic"),
    ("schedule", "Schedule", "alarm-symbolic"),
    ("profile", "Profile changes to", "power-profile-balanced-symbolic"),
]
TRIGGER_ICONS = {k: icon for k, _t, icon in TRIGGERS}
PROFILES = ["quiet", "balanced", "performance"]
PPD = ["power-saver", "balanced", "performance"]
BACKLIGHT = ["Off", "Low", "Medium", "High"]
# action type -> (title, subtitle)
ACTIONS = {
    "profile": ("Power profile", "Platform profile through the Controller"),
    "kbd_colour": ("Keyboard colour", "Static colour"),
    "kbd_brightness": ("Keyboard backlight", "Brightness level"),
    "charge_limit": ("Charge limit", "Stop charging at"),
    "panel_overdrive": ("Panel overdrive", "Faster pixel response"),
    "ppd": ("power-profiles-daemon", "GNOME's power mode"),
    "effect": ("Software effect", "Runs in asusiusd"),
}


def trigger_key(trigger: dict) -> str:
    t = trigger.get("type")
    if t == "power":
        return f"power-{trigger.get('source', 'ac')}"
    return t or "power-ac"


def trigger_icon(trigger: dict) -> str:
    return TRIGGER_ICONS.get(trigger_key(trigger), "media-playlist-repeat-symbolic")


def build_trigger(key: str, *, level: int = 20, below: bool = True, apps: str = "",
                  days: list[str] | None = None, start: str = "22:00", end: str = "07:00",
                  profile: str = "performance") -> dict:
    if key in ("power-ac", "power-battery"):
        return {"type": "power", "source": key.split("-", 1)[1]}
    if key == "battery":
        return {"type": "battery", ("below" if below else "above"): int(level)}
    if key == "app":
        return {"type": "app", "names": [a.strip() for a in apps.replace("\n", ",").split(",")
                                         if a.strip()]}
    if key == "schedule":
        return {"type": "schedule", "days": [d for d in DAYS if d in (days or DAYS)],
                "start": start.strip(), "end": end.strip()}
    return {"type": "profile", "value": profile}


def default_action(kind: str) -> dict:
    return {"profile": {"type": "profile", "value": "performance"},
            "kbd_colour": {"type": "kbd_colour", "value": "#8e2a44"},
            "kbd_brightness": {"type": "kbd_brightness", "value": 1},
            "charge_limit": {"type": "charge_limit", "value": 80},
            "panel_overdrive": {"type": "panel_overdrive", "value": True},
            "ppd": {"type": "ppd", "value": "balanced"},
            "effect": {"type": "effect", "name": "temperature"}}[kind]


PRESETS = [
    {"name": "Games get Performance", "priority": 80, "debounce_s": 3,
     "trigger": {"type": "app", "names": ["steam", "gamescope", "lutris", "heroic"]},
     "actions": [{"type": "profile", "value": "performance"},
                 {"type": "panel_overdrive", "value": True}]},
    {"name": "Unplugged: Quiet", "priority": 50, "debounce_s": 2,
     "trigger": {"type": "power", "source": "battery"},
     "actions": [{"type": "profile", "value": "quiet"},
                 {"type": "kbd_brightness", "value": 1}]},
    {"name": "Low battery", "priority": 90, "debounce_s": 0,
     "trigger": {"type": "battery", "below": 20},
     "actions": [{"type": "profile", "value": "quiet"},
                 {"type": "kbd_brightness", "value": 0}]},
    {"name": "Quiet nights", "priority": 40, "debounce_s": 0,
     "trigger": {"type": "schedule", "days": list(DAYS), "start": "23:00", "end": "07:00"},
     "actions": [{"type": "profile", "value": "quiet"}]},
]


def unique_id(base: str, taken: set[str]) -> str:
    from .rules import slugify
    s = slugify(base)
    if s not in taken:
        return s
    i = 2
    while f"{s}-{i}" in taken:
        i += 1
    return f"{s}-{i}"


def preset_rules(existing: list[Rule]) -> list[dict]:
    """Presets not already covered: same name, or a rule with the same kind of trigger."""
    names = {r.name.lower() for r in existing}
    kinds = {trigger_key(r.trigger.to_dict()) for r in existing}
    return [p for p in PRESETS
            if p["name"].lower() not in names and trigger_key(p["trigger"]) not in kinds]


def log_line(e: dict, names: dict[str, str] | None = None) -> tuple[str, str]:
    """(title, subtitle) for one daemon log entry: what happened, then when/why."""
    kinds = {"activate": "Started", "deactivate": "Ended", "apply": "Applied",
             "restore": "Restored", "error": "Failed", "conflict": "Conflict"}
    detail = e.get("detail", "")
    if e.get("dry_run") and not detail.startswith("dry run"):
        detail = f"dry run: {detail}"
    title = detail[:1].upper() + detail[1:]
    rule = (names or {}).get(e.get("rule", ""), e.get("rule", ""))
    kind = kinds.get(e.get("kind", ""), e.get("kind", "").title())
    return title, f"{e.get('time', '')[11:16]} · {kind} · {rule}"
