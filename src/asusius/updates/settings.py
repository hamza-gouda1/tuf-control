# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The "updates" section of ~/.config/asusius/config.json."""
from __future__ import annotations

import copy

from ..core import ValidationError
from .repos import DEFAULT_REPOS

SOURCES = ("apt", "snap", "flatpak", "firmware", "system")
DEFAULTS: dict = {
    "enabled": True,          # background checks by asusiusd
    "interval_h": 6.0,        # at most one background check per this many hours
    "respect_metered": True,  # skip background checks on a metered connection
    "notify": True,           # desktop notification when the set of updates changes
    "sources": {s: True for s in SOURCES},
    "check_repos": False,     # local Asusius / Bordeaux GNOME checkouts behind upstream
    "repos": list(DEFAULT_REPOS),
}


def validate_updates_section(data, defaults: dict = DEFAULTS) -> dict:
    from ..automation.config import ConfigError
    if not isinstance(data, dict):
        raise ConfigError("updates: expected an object")
    bad = set(data) - set(defaults)
    if bad:
        raise ConfigError(f"updates: unknown keys {sorted(bad)}")
    out = copy.deepcopy(defaults)
    for key in ("enabled", "respect_metered", "notify", "check_repos"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ConfigError(f"updates.{key}: expected true/false")
            out[key] = data[key]
    if "interval_h" in data:
        v = data["interval_h"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 6 <= v <= 24 * 30:
            raise ConfigError("updates.interval_h: a number of hours from 6 to 720")
        out["interval_h"] = float(v)
    if "sources" in data:
        src = data["sources"]
        if not isinstance(src, dict) or set(src) - set(SOURCES) or not all(
                isinstance(v, bool) for v in src.values()):
            raise ConfigError(f"updates.sources: true/false per source ({', '.join(SOURCES)})")
        out["sources"].update(src)
    if "repos" in data:
        r = data["repos"]
        if not isinstance(r, list) or len(r) > 10 or not all(
                isinstance(p, str) and 0 < len(p) < 256 for p in r):
            raise ConfigError("updates.repos: a list of up to 10 paths")
        out["repos"] = list(r)
    return out


def load_settings() -> dict:
    """The validated section, or the defaults when the config cannot be read."""
    try:
        from ..automation.config import load_config
        return load_config().get("updates", copy.deepcopy(DEFAULTS))
    except (ValidationError, OSError):
        return copy.deepcopy(DEFAULTS)
