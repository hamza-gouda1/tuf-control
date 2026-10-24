"""Tell the owner when something new needs an update, once.

An update is identified by (id, offered version). Every offered key is remembered after it
was announced, so the same update never nags twice, a source that fails for one check and
comes back does not re-announce, and fewer updates (some installed) say nothing. Only a
new key (a new package, a newer version) produces a notification, which then summarises
everything outstanding: "14 updates available (apt 12, firmware 1, snap 1)".
"""
from __future__ import annotations

import os
import time

from .model import Report

MAX_REMEMBERED = 2000
NOTIFY_ENV = "TUF_CONTROL_UPDATES_NOTIFY"  # "0" disables


def decide(report: Report, state: dict | None) -> tuple[str | None, dict]:
    """(notification text or None, new state). `state` is {"seen": [...], ...}."""
    state = dict(state or {})
    seen = list(state.get("seen") or [])
    have = set(seen)
    keys = report.keys()
    new = [k for k in keys if k not in have]
    if not new:
        return None, state
    seen.extend(new)
    state["seen"] = seen[-MAX_REMEMBERED:]
    state["last_ts"] = report.checked_ts or time.time()
    state["last_summary"] = report.summary()
    return notification_text(report), state


def notification_text(report: Report) -> str:
    text = report.summary()
    if report.security_count:
        text += f" · {report.security_count} security"
    return text


def body_text(report: Report) -> str:
    names = []
    for src, items in report.by_source().items():
        for i in items[:3]:
            names.append(i.name)
    more = report.total - len(names)
    body = ", ".join(names) + (f" and {more} more" if more > 0 else "")
    if report.reboot_required:
        body += "\nA restart is needed to finish earlier updates."
    return body


def send(summary: str, body: str = "", replaces_id: int = 0) -> int:
    """org.freedesktop.Notifications.Notify; returns the notification id (0 if none)."""
    from ..controller import FAKE_ROOT_ENV
    if os.environ.get(NOTIFY_ENV) == "0":
        return 0
    if os.environ.get(FAKE_ROOT_ENV):  # fake mode: record instead of notifying
        from .check import cache_dir
        p = cache_dir() / "notifications.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(summary + "\n")
        return 0
    try:
        from gi.repository import GLib

        from .. import dbusutil
        (nid,) = dbusutil.call(
            "session", "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                "TUF Control", replaces_id, "software-update-available-symbolic", summary,
                body, [], {"desktop-entry": GLib.Variant("s", "io.github.hamzagouda.TufControl"),
                           "urgency": GLib.Variant("y", 1)}, -1)),
            "(u)", 5000)
        return int(nid)
    except Exception:
        return 0
