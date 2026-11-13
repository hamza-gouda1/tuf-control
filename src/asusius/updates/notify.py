# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
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
import threading

from .model import Report

MAX_REMEMBERED = 2000
NOTIFY_ENV = "ASUSIUS_UPDATES_NOTIFY"  # "0" disables


def decide(report: Report, state: dict | None) -> tuple[str | None, dict]:
    """(notification text or None, candidate state). `state` is {"seen": [...], ...}.

    Persist the candidate only after delivery succeeds, or when the owner already
    saw the report in the app (including intentionally suppressed notifications).
    """
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


def _launch_app() -> None:
    """Open the app on its Updates page (the desktop action runs `--page updates`)."""
    try:
        from gi.repository import Gio
        app = Gio.DesktopAppInfo.new("io.github.hamzagouda.Asusius.desktop")
        if app is None:
            return
        if "updates" in app.list_actions():
            app.launch_action("updates", None)
        else:
            app.launch(None, None)
    except Exception:
        pass


_click_watch = None
_click_lock = threading.Lock()


class _ClickWatch:
    """One notification subscription and a private dispatched GLib context.

    Replacing or closing a notification retires the old subscription. The daemon
    does not run GTK's main loop, so relying on its default context loses clicks.
    """
    def __init__(self, conn, nid: int):
        self.conn, self.nid = conn, nid
        self.stop = threading.Event()
        self.ready = threading.Event()
        self._context = None
        self.thread = threading.Thread(target=self._run, name="asusius-update-notification",
                                       daemon=True)
        self.thread.start()
        self.ready.wait(2.0)

    def _signal(self, _c, _s, _p, _i, name, params):
        value = params.unpack()
        if not value or int(value[0]) != self.nid:
            return
        if name == "ActionInvoked" and len(value) > 1 and value[1] == "default":
            _launch_app()
            self._request_stop()
        elif name == "NotificationClosed":
            self._request_stop()

    def _request_stop(self):
        self.stop.set()
        context = getattr(self, "_context", None)
        if context is not None:
            context.wakeup()

    def _run(self):
        from gi.repository import Gio, GLib
        context = GLib.MainContext.new()
        self._context = context
        context.push_thread_default()
        sub = source = None
        try:
            sub = self.conn.signal_subscribe(
                "org.freedesktop.Notifications", "org.freedesktop.Notifications", None,
                "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE,
                self._signal)
            deadline = time.monotonic() + 24 * 3600
            source = GLib.timeout_source_new_seconds(1)
            source.set_callback(lambda *_: not self.stop.is_set() and time.monotonic() < deadline)
            source.attach(context)
            self.ready.set()
            while not self.stop.is_set() and time.monotonic() < deadline:
                context.iteration(True)
        finally:
            if source is not None:
                source.destroy()
            if sub is not None:
                self.conn.signal_unsubscribe(sub)
            context.pop_thread_default()
            self._context = None
            self.ready.set()

    def close(self):
        self._request_stop()
        if threading.current_thread() is not self.thread:
            self.thread.join(2.0)


def close_clicks() -> None:
    global _click_watch
    with _click_lock:
        if _click_watch is not None:
            _click_watch.close()
            _click_watch = None


def _watch_clicks(conn, nid: int) -> None:
    global _click_watch
    with _click_lock:
        if _click_watch is not None:
            _click_watch.close()
        _click_watch = _ClickWatch(conn, nid)


def send(summary: str, body: str = "", replaces_id: int = 0,
         open_on_click: bool = False) -> int:
    """org.freedesktop.Notifications.Notify; returns the notification id (0 if none).
    With `open_on_click`, clicking the notification opens the app."""
    from ..controller import FAKE_ROOT_ENV
    if os.environ.get(NOTIFY_ENV) == "0":
        return 0
    if os.environ.get(FAKE_ROOT_ENV):  # fake mode: record instead of notifying
        from .check import cache_dir
        p = cache_dir() / "notifications.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(summary + (" [click opens the app]" if open_on_click else "") + "\n")
        return 0
    try:
        from gi.repository import Gio, GLib

        from .. import dbusutil
        actions = ("default", "") if open_on_click else ()
        (nid,) = dbusutil.call(
            "session", "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                "Asusius", replaces_id, "software-update-available-symbolic", summary,
                body, list(actions),
                {"desktop-entry": GLib.Variant("s", "io.github.hamzagouda.Asusius"),
                 "urgency": GLib.Variant("y", 1)}, -1)),
            "(u)", 5000)
        nid = int(nid)
        if open_on_click and nid:
            try:
                _watch_clicks(dbusutil.bus("session"), nid)
            except Exception:
                pass
        return nid
    except Exception:
        return 0
