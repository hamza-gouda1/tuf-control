# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Read-only agent tool: what needs an update, from the cached check (no check is run,
nothing is installed; installing stays with the owner)."""
from __future__ import annotations

import time

from .model import age_text

MAX_ITEMS = 100


def get_updates(_svc, a: dict) -> dict:
    from ..agent.sanitize import clean
    from .check import load_report
    rep = load_report()
    if rep is None:
        return {"checked": False, "note": "No update check yet; the owner can run one from "
                                          "the Updates page or `tuf-control updates check`."}
    src = a.get("source")
    items = [i for i in rep.updates() if not src or i.source == src]
    out = {
        "checked": True, "checked_age_s": int(max(0, time.time() - rep.checked_ts)),
        "checked_ago": age_text(rep.checked_ts), "summary": rep.summary(),
        "total": rep.total, "security": rep.security_count, "counts": rep.counts(),
        "restart_needed": rep.reboot_required,
        "restart_reasons": [clean(r, 120) for r in rep.reboot_reasons[:10]],
        "items": [{"source": i.source, "name": clean(i.name, 80),
                   "current": clean(i.current, 60), "available": clean(i.available, 60),
                   "security": i.security, "needs_restart": i.needs_reboot, "kind": i.kind}
                  for i in items[:MAX_ITEMS]],
        "sources": {s.source: ("unavailable: " + clean(s.reason, 120)) if not s.available
                    else ("error: " + clean(s.error, 120)) if s.error else "ok"
                    for s in rep.sources},
        "untrusted_fields": ["items[].name", "items[].current", "items[].available"],
        "note": "Installing updates is not available to agents.",
    }
    if len(items) > MAX_ITEMS:
        out["truncated"] = len(items) - MAX_ITEMS
    return out


def get_update_history(_svc, a: dict) -> dict:
    """Past update runs from the persistent history. Read-only."""
    from ..agent.sanitize import clean
    from . import history
    try:
        since = history.parse_since(a.get("since"))
    except ValueError as exc:
        return {"error": clean(str(exc), 200)}
    runs = history.read(since_ts=since, source=a.get("source"),
                        limit=min(int(a.get("limit") or 20), 100))
    out = {
        "runs": [{
            "start": r.get("start"), "end": r.get("end"), "trigger": r.get("trigger"),
            "duration_s": r.get("duration_s"), "cancelled": bool(r.get("cancelled")),
            "reboot": bool(r.get("reboot")), "summary": clean(r.get("summary") or "", 160),
            "items": [{"source": i.get("source"), "name": clean(i.get("name"), 80),
                       "old": clean(i.get("old"), 60), "new": clean(i.get("new"), 60),
                       "result": i.get("result"), "reason": clean(i.get("reason") or "", 300)}
                      for i in (r.get("items") or [])[:MAX_ITEMS]],
        } for r in runs],
        "untrusted_fields": ["runs[].items[].name", "runs[].items[].old",
                             "runs[].items[].new", "runs[].items[].reason"],
        "note": "History is recorded by TUF Control itself; installing updates is not "
                "available to agents.",
    }
    return out
