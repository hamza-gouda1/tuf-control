# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free text for the Agent access page (unit-tested without a display)."""
from __future__ import annotations

import time
from datetime import datetime

from . import policy
from .sanitize import clean

# combo order on the page; "none" is the master switch being off
PAGE_LEVELS = ("read", "control-basic", "control-all")
LEVEL_HELP = {
    "read": "Status, telemetry, processes, fan curves. Nothing can be changed.",
    "control-basic": "Also profile, charge limit and keyboard. Every change is a dry run "
                     "until you approve it.",
    "control-all": "Also fan curves, GPU mode, power limits and restore defaults, once "
                   "high-risk tools are on. Those are approved one call at a time.",
}
DURATIONS = [("15 minutes", 15 * 60), ("1 hour", 3600), ("4 hours", 4 * 3600),
             ("24 hours", 24 * 3600)]
DECISIONS = {
    "ok": ("Read", ""), "dry_run": ("Dry run", ""), "pending": ("Asked you", "gold"),
    "executed": ("Changed", "accent"), "denied": ("Refused", "gold"),
    "refused": ("Refused", "gold"), "invalid": ("Invalid", ""),
    "rate_limited": ("Rate limited", "gold"), "error": ("Failed", ""),
    "grant": ("Granted", "accent"), "revoke": ("Revoked", "gold"),
    "approve": ("Approved", "accent"), "deny": ("Denied", "gold"),
}


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    s = max(0, int((time.time() if now is None else now) - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def standing_text(acc: policy.Access, now: float | None = None) -> str:
    now = time.time() if now is None else now
    g = acc.standing(now)
    if g is None:
        return "Off: you approve each change"
    until = datetime.fromtimestamp(g["expires_at"]).strftime("%H:%M")
    return (f"Basic changes approved until {until} "
            f"({policy.format_remaining(g['expires_at'] - now)} left)")


def master_subtitle(acc: policy.Access) -> str:
    if acc.error:
        return f"agent.json has an error, so all access is off: {clean(acc.error, 120)}"
    if not acc.enabled:
        return "Off: every request from an agent is refused"
    return policy.LEVEL_TITLES[acc.effective_level()]


def pending_rows(items: list[dict], now: float | None = None) -> list[dict]:
    rows = []
    for e in items:
        sub = [clean(e.get("client"), 40), ago(e.get("created_at"), now)]
        if e.get("tier") == "high":
            sub.insert(0, "High risk")
        reason = clean(e.get("reason") or "", 140)
        rows.append({"id": e["id"], "title": clean(e.get("summary"), 160),
                     "subtitle": " · ".join(x for x in sub if x),
                     "reason": f"Stated reason (unverified): “{reason}”" if reason else "",
                     "high": e.get("tier") == "high", "params": e.get("params")})
    return rows


def audit_row(e: dict) -> dict:
    label, tone = DECISIONS.get(e.get("decision"), (clean(e.get("decision"), 16), ""))
    t = str(e.get("time", ""))
    who = clean(e.get("client"), 28)
    what = clean(e.get("tool"), 40)
    detail = ""
    res = e.get("result")
    if isinstance(res, dict):
        detail = clean(res.get("summary") or res.get("message") or res.get("detail") or "", 90)
    return {"time": t[11:16] if len(t) >= 16 else t, "who": who, "what": what,
            "detail": detail, "label": label, "tone": tone}


def config_snippet() -> str:
    import json

    from .cli_cmds import client_config
    return json.dumps(client_config(), indent=2)
