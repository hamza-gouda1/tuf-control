"""Small synchronous D-Bus helpers over Gio, for worker threads (updates, services).

Every call has a timeout. `interactive=True` sets ALLOW_INTERACTIVE_AUTHORIZATION so a
system service may ask polkit, which shows the desktop's own authentication dialog; this
module never escalates privileges itself.
"""
from __future__ import annotations


class DBusUnavailable(RuntimeError):
    """PyGObject or the bus is missing."""


def _gio():
    try:
        import gi  # noqa: F401
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as exc:  # pragma: no cover - depends on the system
        raise DBusUnavailable(f"PyGObject unavailable: {exc}") from None
    return Gio, GLib


def bus(kind: str = "system"):
    Gio, GLib = _gio()
    try:
        return Gio.bus_get_sync(Gio.BusType.SYSTEM if kind == "system" else Gio.BusType.SESSION,
                                None)
    except GLib.Error as exc:
        raise DBusUnavailable(f"{kind} bus: {exc.message}") from None


def call(kind: str, name: str, path: str, iface: str, method: str, args=None,
         reply: str | None = None, timeout_ms: int = 10000, interactive: bool = False,
         conn=None, fd_list=None):
    """Call and return the unpacked reply tuple. Raises GLib.Error on a D-Bus error."""
    Gio, GLib = _gio()
    conn = conn or bus(kind)
    flags = Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION if interactive \
        else Gio.DBusCallFlags.NONE
    rtype = GLib.VariantType(reply) if reply else None
    if fd_list is not None:
        res, _out = conn.call_with_unix_fd_list_sync(name, path, iface, method, args, rtype,
                                                     flags, timeout_ms, fd_list, None)
    else:
        res = conn.call_sync(name, path, iface, method, args, rtype, flags, timeout_ms, None)
    return res.unpack() if res is not None else ()


def get_property(kind: str, name: str, path: str, iface: str, prop: str,
                 timeout_ms: int = 5000, conn=None):
    _Gio, GLib = _gio()
    (val,) = call(kind, name, path, "org.freedesktop.DBus.Properties", "Get",
                  GLib.Variant("(ss)", (iface, prop)), "(v)", timeout_ms, conn=conn)
    return val


def has_owner(kind: str, name: str, conn=None) -> bool:
    """True when the name has an owner or can be activated (does not activate it)."""
    _Gio, GLib = _gio()
    try:
        (owned,) = call(kind, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", (name,)),
                        "(b)", 3000, conn=conn)
        if owned:
            return True
        (names,) = call(kind, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "ListActivatableNames", None, "(as)", 3000,
                        conn=conn)
        return name in names
    except (GLib.Error, DBusUnavailable):
        return False


def error_name(exc) -> str:
    """'org.freedesktop.fwupd.NothingToDo' from a GLib.Error raised by a remote call."""
    try:
        from gi.repository import Gio
        return Gio.DBusError.get_remote_error(exc) or ""
    except Exception:
        return ""


def error_text(exc) -> str:
    try:
        from gi.repository import Gio
        Gio.DBusError.strip_remote_error(exc)
    except Exception:
        pass
    return getattr(exc, "message", None) or str(exc)
