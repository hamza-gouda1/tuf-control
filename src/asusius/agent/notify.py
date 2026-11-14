# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Desktop notification for a pending agent request, with Approve once / Deny buttons.

Optional: used only when PyGObject and a notification server on the session bus are
available, never under ASUSIUS_FAKE_ROOT or with ASUSIUS_AGENT_NOTIFY=0. The
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
    if os.environ.get(FAKE_ROOT_ENV) or os.environ.get("ASUSIUS_AGENT_NOTIFY") == "0":
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
        self._ctx = None
        self._loop = None
        self._owner: str | None = None
        self._subs: list[int] = []
        self._lock = threading.Lock()

    def _refresh_owner(self, conn, GLib, Gio) -> str | None:
        try:
            owner = conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "GetNameOwner", GLib.Variant("(s)", (BUS,)), GLib.VariantType("(s)"),
                Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            return owner or None
        except Exception:
            return None

    def _on_owner_changed(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        name, _old, new = params.unpack()
        if name == BUS:
            with self._lock:
                self._owner = new or None

    def _start(self) -> bool:
        if (self._conn is not None and not self._conn.is_closed()
                and self._ctx is not None and self._loop is not None):
            return True
        from gi.repository import Gio, GLib
        ctx = GLib.MainContext.new()
        ready = threading.Event()
        aborted = threading.Event()
        state: dict = {}

        def run():
            ctx.push_thread_default()
            conn = None
            subs: list[int] = []
            try:
                conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                subs.append(conn.signal_subscribe(
                    "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                    "/org/freedesktop/DBus", BUS, Gio.DBusSignalFlags.NONE,
                    self._on_owner_changed))
                owner = self._refresh_owner(conn, GLib, Gio)
                with self._lock:
                    self._owner = owner
                subs.append(conn.signal_subscribe(
                    None, BUS, "ActionInvoked", PATH, None,
                    Gio.DBusSignalFlags.NONE, self._on_action))
                subs.append(conn.signal_subscribe(
                    None, BUS, "NotificationClosed", PATH, None,
                    Gio.DBusSignalFlags.NONE, self._on_closed))
                if not aborted.is_set():
                    loop = GLib.MainLoop.new(ctx, False)
                    state.update(conn=conn, ctx=ctx, loop=loop, subs=subs)
            except Exception as exc:  # no notification server or bus failure
                state["error"] = exc
            finally:
                ready.set()
            try:
                if "loop" in state and not aborted.is_set():
                    state["loop"].run()
            finally:
                if conn is not None:
                    for sid in subs:
                        try:
                            conn.signal_unsubscribe(sid)
                        except Exception:
                            pass
                try:
                    ctx.pop_thread_default()
                except Exception:
                    pass

        threading.Thread(target=run, name="agent-notify", daemon=True).start()
        if not ready.wait(3) or "conn" not in state:
            aborted.set()
            self._conn = self._ctx = self._loop = None
            self._subs = []
            return False
        self._conn = state["conn"]
        self._ctx = state["ctx"]
        self._loop = state["loop"]
        self._subs = state["subs"]
        return True

    def notify(self, entry: dict) -> None:
        from gi.repository import Gio, GLib
        with self._lock:
            if not self._start():
                return
            conn, ctx = self._conn, self._ctx
            high = entry.get("tier") == "high"
            body = escape_markup(clean(entry["summary"], 160))
            if entry.get("reason"):
                body += "\nStated reason: “" + escape_markup(clean(entry["reason"], 120)) + "”"
            body += "\nClaimed client: " + escape_markup(clean(entry.get("client"), 60))
            from .present import process_label
            launcher = process_label(entry)
            if launcher:
                body += "\n" + escape_markup(launcher)
            # high-risk requests are approved in the app, where every parameter is shown
            actions = ["deny", "Deny"] if high else ["approve", "Approve once", "deny", "Deny"]
            if high:
                body += "\nReview and approve it in Asusius → Agent access."
            hints = {"urgency": GLib.Variant("y", 2 if high else 1),
                     "desktop-entry": GLib.Variant("s", "io.github.hamzagouda.Asusius")}
            payload = GLib.Variant("(susssasa{sv}i)", (
                "Asusius", 0, "io.github.hamzagouda.Asusius",
                ("High-risk agent request" if high else "Agent request"), body,
                actions, hints, 0))
            rid = entry["id"]

        done = threading.Event()
        box: dict = {}

        def dispatch(*_):
            def on_done(src, result):
                try:
                    res = src.call_finish(result)
                    nid = res.unpack()[0]
                    with self._lock:
                        self.ids[nid] = rid
                        if self._owner is None:
                            self._owner = self._refresh_owner(src, GLib, Gio)
                except Exception as exc:
                    box["error"] = exc
                finally:
                    done.set()

            conn.call(BUS, PATH, BUS, "Notify", payload, GLib.VariantType("(u)"),
                      Gio.DBusCallFlags.NONE, 3000, None, on_done)
            return False

        src = GLib.idle_source_new()
        src.set_callback(dispatch)
        src.attach(ctx)
        if done.wait(3.5) and "error" in box:
            raise box["error"]

    def _sender_ok(self, conn, sender: str | None) -> bool:
        if sender is None:
            return True
        with self._lock:
            owner = self._owner
        if owner is None and conn is not None:
            from gi.repository import Gio, GLib
            owner = self._refresh_owner(conn, GLib, Gio)
            with self._lock:
                self._owner = owner
        return owner is not None and sender == owner

    def _on_closed(self, conn, sender, _path, _iface, _signal, params) -> None:
        if not self._sender_ok(conn, sender):
            return
        nid, _reason = params.unpack()
        with self._lock:
            self.ids.pop(nid, None)

    def _on_action(self, conn, sender, _path, _iface, _signal, params) -> None:
        if not self._sender_ok(conn, sender):
            return
        nid, action = params.unpack()
        with self._lock:
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

    def close(self) -> None:
        with self._lock:
            loop, ctx = self._loop, self._ctx
            self._conn = self._ctx = self._loop = None
            self.ids.clear()
        if loop is not None and ctx is not None:
            try:
                from gi.repository import GLib
                src = GLib.idle_source_new()
                src.set_callback(lambda *_: (loop.quit(), False)[1])
                src.attach(ctx)
            except Exception:
                pass
