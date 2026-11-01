# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Desktop notification for a pending agent request, with Approve once / Deny buttons.

Optional: used only when PyGObject and a notification server on the session bus are
available, never under TUF_CONTROL_FAKE_ROOT or with TUF_CONTROL_AGENT_NOTIFY=0. The
button signals are only accepted from the current owner of org.freedesktop.Notifications
and only for notification ids this process created. The approval itself goes through
the same queue as the app and the CLI.
"""
from __future__ import annotations

import os
import threading

from .sanitize import clean, escape_markup

BUS = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


def make_notifier(queue, audit=None):
    from ..controller import FAKE_ROOT_ENV
    if os.environ.get(FAKE_ROOT_ENV) or os.environ.get("TUF_CONTROL_AGENT_NOTIFY") == "0":
        return None
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return None
    try:
        import gi  # noqa: F401
        from gi.repository import Gio  # noqa: F401
    except (ImportError, ValueError):
        return None
    return DesktopNotifier(queue, audit)


class DesktopNotifier:
    def __init__(self, queue, audit=None) -> None:
        self.queue, self.audit = queue, audit
        self.ids: dict[int, str] = {}   # notification id -> request id
        self._conn = None
        self._loop = None
        self._lock = threading.Lock()

    def _start(self) -> bool:
        if self._conn is not None:
            return True
        from gi.repository import Gio, GLib
        ctx = GLib.MainContext.new()
        ready = threading.Event()
        state = {}

        def run():
            ctx.push_thread_default()
            try:
                conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                owner = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                       "org.freedesktop.DBus", "GetNameOwner",
                                       GLib.Variant("(s)", (BUS,)), GLib.VariantType("(s)"),
                                       Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
                conn.signal_subscribe(owner, BUS, "ActionInvoked", PATH, None,
                                      Gio.DBusSignalFlags.NONE, self._on_action)
                state["conn"] = conn
            except Exception as exc:  # no notification server
                state["error"] = exc
            ready.set()
            if "conn" in state:
                self._loop = GLib.MainLoop.new(ctx, False)
                self._loop.run()

        threading.Thread(target=run, name="agent-notify", daemon=True).start()
        ready.wait(3)
        self._conn = state.get("conn")
        return self._conn is not None

    def notify(self, entry: dict) -> None:
        from gi.repository import Gio, GLib
        with self._lock:
            if not self._start():
                return
            high = entry.get("tier") == "high"
            body = escape_markup(clean(entry["summary"], 160))
            if entry.get("reason"):
                body += "\nStated reason: “" + escape_markup(clean(entry["reason"], 120)) + "”"
            body += "\nFrom: " + escape_markup(clean(entry.get("client"), 60))
            # high-risk requests are approved in the app, where every parameter is shown
            actions = ["deny", "Deny"] if high else ["approve", "Approve once", "deny", "Deny"]
            if high:
                body += "\nReview and approve it in TUF Control → Agent access."
            hints = {"urgency": GLib.Variant("y", 2 if high else 1),
                     "desktop-entry": GLib.Variant("s", "io.github.hamzagouda.TufControl")}
            res = self._conn.call_sync(
                BUS, PATH, BUS, "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    "TUF Control", 0, "io.github.hamzagouda.TufControl",
                    ("High-risk agent request" if high else "Agent request"), body,
                    actions, hints, 0)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 3000, None)
            self.ids[res.unpack()[0]] = entry["id"]

    def _on_action(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        nid, action = params.unpack()
        rid = self.ids.pop(nid, None)
        if rid is None or action not in ("approve", "deny"):
            return
        try:
            e = self.queue.get(rid)
            if e is None or (action == "approve" and e.get("tier") == "high"):
                return
            e = self.queue.decide(rid, action == "approve", by="notification")
            if self.audit is not None:
                self.audit.write(client="owner:notification", kind="owner", name=e["tool"],
                                 decision="approve" if action == "approve" else "deny",
                                 request_id=rid)
        except Exception:  # already decided elsewhere, or expired
            pass
