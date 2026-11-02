# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Access levels and the owner's grants (~/.config/tuf-control/agent.json).

Levels, lowest first:
  none           nothing, not even reads
  read           read tools and resources (the default)
  control-basic  adds profile, charge limit, keyboard writes and the automation dry run
  control-all    adds the high-risk tools (fan curves, GPU mode, power limits, restore
                 defaults); those also need "high_risk_tools": true and an approval per call

A real write (dry_run: false) needs an approval: either a per-call approval from the
queue, or, for control-basic tools only, a standing grant with an expiry
("allow control-basic for 1 hour"). High-risk tools are never covered by a standing grant.

A missing file means the defaults (enabled, read). A file that cannot be read or does
not validate fails closed: no access at all until it is fixed or rewritten.
"""
from __future__ import annotations

import re
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core import ValidationError
from .store import access_path, locked, read_json, write_json

LEVELS = ("none", "read", "control-basic", "control-all")
RANK = {name: i for i, name in enumerate(LEVELS)}
TIER_LEVEL = {"read": "read", "basic": "control-basic", "high": "control-all"}
LEVEL_TITLES = {"none": "No access", "read": "Read only", "control-basic": "Basic control",
                "control-all": "Full control"}
MIN_GRANT_S = 60
MAX_GRANT_S = 24 * 3600
VERSION = 1
DEFAULTS = {"version": VERSION, "enabled": True, "level": "read", "high_risk_tools": False,
            "standing_grant": None, "notifications": True, "revoked_at": None}


def parse_duration(text: str) -> int:
    """'90s', '15m', '1h', '1h30m', '2h' -> seconds (bounded to MIN..MAX_GRANT_S)."""
    s = str(text).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(?:(\d{1,3})h)?(?:(\d{1,4})m(?:in)?)?(?:(\d{1,5})s)?", s)
    if not s or not m or not any(m.groups()):
        raise ValidationError(f"bad duration {text!r} (examples: 15m, 1h, 1h30m)")
    h, mi, se = (int(x or 0) for x in m.groups())
    secs = h * 3600 + mi * 60 + se
    if not MIN_GRANT_S <= secs <= MAX_GRANT_S:
        raise ValidationError(f"duration must be between 1 minute and 24 hours, not {text!r}")
    return secs


def format_remaining(seconds: float) -> str:
    s = max(0, int(seconds))
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{h} h {m:02d} min" if h else f"{max(1, m)} min"


@dataclass
class Access:
    enabled: bool = True
    level: str = "read"
    high_risk_tools: bool = False
    standing_grant: dict | None = None
    notifications: bool = True
    revoked_at: float | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)

    def standing(self, now: float | None = None) -> dict | None:
        g = self.standing_grant
        now = time.time() if now is None else now
        if g and self.enabled and g["granted_at"] <= now < g["expires_at"]:
            return g
        return None

    def effective_level(self, now: float | None = None) -> str:
        if not self.enabled or self.error:
            return "none"
        g = self.standing(now)
        if g and RANK[g["level"]] > RANK[self.level]:
            return g["level"]
        return self.level

    def allows(self, tier: str, now: float | None = None) -> bool:
        if tier == "high" and not self.high_risk_tools:
            return False
        return RANK[self.effective_level(now)] >= RANK[TIER_LEVEL[tier]]

    def pre_approved(self, tier: str, now: float | None = None) -> bool:
        """A standing grant approves real writes of control-basic tools only."""
        g = self.standing(now)
        return tier == "basic" and g is not None and RANK[g["level"]] >= RANK["control-basic"]

    def public(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        g = self.standing(now)
        return {
            "enabled": self.enabled, "level": self.level,
            "effective_level": self.effective_level(now),
            "high_risk_tools": self.high_risk_tools,
            "standing_grant": None if g is None else {
                "level": g["level"], "expires_at": g["expires_at"],
                "remaining_s": int(g["expires_at"] - now)},
            "notifications": self.notifications,
            "error": self.error,
        }


def _valid_timestamp(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_access(data) -> dict:
    if not isinstance(data, dict):
        raise ValidationError("agent.json: expected an object")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValidationError(f"agent.json: unknown keys {sorted(unknown)}")
    out = dict(DEFAULTS)
    if data.get("version", VERSION) != VERSION:
        raise ValidationError(f"agent.json: unsupported version {data.get('version')!r}")
    for k in ("enabled", "high_risk_tools", "notifications"):
        if k in data:
            if not isinstance(data[k], bool):
                raise ValidationError(f"agent.json: {k} must be true or false")
            out[k] = data[k]
    if "level" in data:
        if data["level"] not in LEVELS:
            raise ValidationError(f"agent.json: level must be one of {', '.join(LEVELS)}")
        out["level"] = data["level"]
    g = data.get("standing_grant")
    if g is not None:
        if not isinstance(g, dict) or set(g) - {"level", "granted_at", "expires_at"}:
            raise ValidationError("agent.json: standing_grant must be "
                                  "{level, granted_at, expires_at}")
        if g.get("level") not in ("control-basic", "control-all"):
            raise ValidationError("agent.json: standing_grant.level must be control-basic or "
                                  "control-all")
        for k in ("granted_at", "expires_at"):
            if not _valid_timestamp(g.get(k)):
                raise ValidationError(f"agent.json: standing_grant.{k} must be a timestamp")
        if g["expires_at"] < g["granted_at"]:
            raise ValidationError("agent.json: standing grant expires before it starts")
        if g["expires_at"] - g["granted_at"] > MAX_GRANT_S + 1:
            raise ValidationError("agent.json: standing grants last at most 24 hours")
        out["standing_grant"] = {"level": g["level"], "granted_at": float(g["granted_at"]),
                                 "expires_at": float(g["expires_at"])}
    r = data.get("revoked_at")
    if r is not None and not _valid_timestamp(r):
        raise ValidationError("agent.json: revoked_at must be a timestamp")
    out["revoked_at"] = r
    return out


def load_access(path: Path | None = None) -> Access:
    path = path or access_path()
    try:
        data = read_json(path, 64 << 10)
        norm = validate_access(DEFAULTS if data is None else data)
    except (OSError, ValueError) as exc:  # ValidationError is a ValueError
        return Access(enabled=False, level="none", error=str(exc)[:300])
    return Access(enabled=norm["enabled"], level=norm["level"],
                  high_risk_tools=norm["high_risk_tools"],
                  standing_grant=norm["standing_grant"], notifications=norm["notifications"],
                  revoked_at=norm["revoked_at"], raw=norm)


def update_access(fn, path: Path | None = None) -> Access:
    """Locked read-modify-write. A broken file is replaced by the defaults first."""
    path = path or access_path()
    with locked(path):
        try:
            cur = validate_access(read_json(path, 64 << 10) or DEFAULTS)
        except (OSError, ValueError):
            cur = dict(DEFAULTS)
        new = fn(dict(cur))
        write_json(path, validate_access(cur if new is None else new))
    return load_access(path)


def grant(level: str, duration_s: int | None = None, *, high_risk: bool | None = None,
          now: float | None = None, path: Path | None = None) -> Access:
    """Set the access level. With a duration (control levels only) it is a standing grant:
    the level rises for that long and control-basic writes need no per-call approval."""
    if level not in LEVELS:
        raise ValidationError(f"unknown level {level!r} (one of {', '.join(LEVELS)})")
    now = time.time() if now is None else now
    if duration_s is not None:
        if level not in ("control-basic", "control-all"):
            raise ValidationError("a timed grant needs control-basic or control-all")
        if not MIN_GRANT_S <= duration_s <= MAX_GRANT_S:
            raise ValidationError("a timed grant lasts between 1 minute and 24 hours")

    def edit(cfg):
        cfg["enabled"] = level != "none" or cfg["enabled"]
        if duration_s is None:
            cfg["level"] = level
            if RANK[level] < RANK["control-basic"]:
                cfg["standing_grant"] = None
        else:
            if RANK[cfg["level"]] < RANK["read"]:
                cfg["level"] = "read"
            cfg["standing_grant"] = {"level": level, "granted_at": now,
                                     "expires_at": now + duration_s}
        if high_risk is not None:
            cfg["high_risk_tools"] = high_risk
        cfg["revoked_at"] = None
        return cfg
    return update_access(edit, path)


def set_enabled(on: bool, path: Path | None = None) -> Access:
    def edit(cfg):
        cfg["enabled"] = on
        if not on:
            cfg["standing_grant"] = None
        return cfg
    return update_access(edit, path)


def set_high_risk(on: bool, path: Path | None = None) -> Access:
    def edit(cfg):
        cfg["high_risk_tools"] = on
        return cfg
    return update_access(edit, path)


def end_standing(path: Path | None = None) -> Access:
    def edit(cfg):
        cfg["standing_grant"] = None
        return cfg
    return update_access(edit, path)


def revoke(now: float | None = None, path: Path | None = None) -> Access:
    """Kill switch: access off, level back to read, no standing grant, no high-risk tools.
    (The caller also cancels pending approvals; see approvals.ApprovalQueue.cancel_all.)"""
    now = time.time() if now is None else now

    def edit(cfg):
        cfg.update(enabled=False, level="read", standing_grant=None, high_risk_tools=False,
                   revoked_at=now)
        return cfg
    return update_access(edit, path)
