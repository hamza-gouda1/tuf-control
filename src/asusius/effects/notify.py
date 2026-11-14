# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Notification events from the session bus without a notification server of our own.

A second, private connection to the session bus calls
org.freedesktop.DBus.Monitoring.BecomeMonitor with one match rule for
`org.freedesktop.Notifications.Notify` method calls. The bus allows this for
connections of the same user (dbus-daemon and dbus-broker); the connection then only
receives the matched messages and can send nothing, which keeps it read-only. If the bus
refuses (sandboxed session, policy), `error` says so and the flash overlay stays idle.
"""
from __future__ import annotations

import threading
from collections import deque

MATCH = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"


def parse_notify(args) -> dict | None:
    """Notify(s app_name, u replaces_id, s app_icon, s summary, s body, as actions,
    a{sv} hints, i expire_timeout) -> event dict."""
    try:
        app, _rid, _icon, summary, _body, _actions, hints, _exp = args
    except (TypeError, ValueError):
        return None
    urgency = 1
    if isinstance(hints, dict):
        u = hints.get("urgency")
        if isinstance(u, int):
            urgency = u
        desk = hints.get("desktop-entry")
        if isinstance(desk, str) and desk:
            app = app or desk
    return {"app": str(app), "summary": str(summary), "urgency": urgency}


class NotificationQueue:
    def __init__(self, maxlen: int = 32) -> None:
        self._q: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def push(self, event: dict) -> None:
        with self._lock:
            self._q.append(event)

    def drain(self) -> list[dict]:
        with self._lock:
            out = list(self._q)
            self._q.clear()
        return out


class NotificationMonitor(NotificationQueue):
    def __init__(self, address: str | None = None) -> None:
        super().__init__()
        self.address = address
        self.conn = None
        self.error: str | None = None
        self._filter = None

    def start(self) -> None:
        if self.conn is not None:
            return
        conn = None
        try:
            from gi.repository import Gio, GLib
            addr = self.address or Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                     | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
            conn = Gio.DBusConnection.new_for_address_sync(addr, flags, None, None)
            self._filter = conn.add_filter(self._on_message)
            conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus.Monitoring", "BecomeMonitor",
                           GLib.Variant("(asu)", ([MATCH], 0)), None,
                           Gio.DBusCallFlags.NONE, 3000, None)
            self.conn = conn
            self.error = None
        except Exception as exc:
            if conn is not None and self.conn is not conn:
                try:
                    conn.close_sync(None)
                except Exception:
                    pass
            self._filter = None
            self.error = f"cannot monitor notifications: {exc}"

    def _on_message(self, _conn, msg, incoming):
        from gi.repository import Gio
        if not incoming or msg.get_message_type() != Gio.DBusMessageType.METHOD_CALL:
            return msg  # replies (BecomeMonitor's own) and bus signals go on as usual
        try:
            if (msg.get_member() == "Notify"
                    and msg.get_interface() == "org.freedesktop.Notifications"):
                body = msg.get_body()
                ev = parse_notify(body.unpack() if body is not None else None)
                if ev:
                    self.push(ev)
        except Exception:
            pass
        return None  # never let GDBus answer a monitored call: a monitor may not send

    def stop(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close_sync(None)
            except Exception:
                pass
            self.conn = None
