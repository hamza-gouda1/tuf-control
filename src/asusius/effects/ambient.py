# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Ambient screen colour through the xdg-desktop-portal ScreenCast interface.

GNOME on Wayland gives no silent screen access. The portal asks the user once (monitor
picker dialog); with persist_mode=2 the portal returns a restore token that we keep in
~/.config/asusius/screencast-token, so later starts skip the dialog. While the cast
runs GNOME Shell shows its screen-sharing indicator in the top bar; that is by design.

Pipeline: portal (CreateSession -> SelectSources -> Start -> OpenPipeWireRemote) ->
GStreamer `pipewiresrc` -> 4 fps, 16x9 RGB -> mean colour with dark pixels ignored and a
saturation boost. The portal flow, token handling and averaging are tested against a fake
bus and a fake frame source; the real dialog has not been exercised by the test suite.
"""
from __future__ import annotations

import os
import secrets
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from ..automation.config import _atomic_write
from .colour import RGB, boost_saturation, clamp8

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SOURCE_MONITOR = 1
CURSOR_HIDDEN = 1
PERSIST_UNTIL_REVOKED = 2
MAX_TOKEN_BYTES = 64 * 1024


class PortalError(Exception):
    pass


class PortalDenied(PortalError):
    """The user cancelled the dialog (response code 1)."""


def average_rgb(data: bytes, width: int, height: int, stride: int | None = None,
                boost: float = 1.3, dark: int = 24) -> RGB | None:
    """Mean of RGB pixels brighter than `dark` (skips letterboxing and black UI)."""
    stride = stride or width * 3
    r = g = b = n = 0
    for y in range(height):
        row = data[y * stride:y * stride + width * 3]
        for i in range(0, len(row) - 2, 3):
            pr, pg, pb = row[i], row[i + 1], row[i + 2]
            if pr + pg + pb > dark * 3:
                r += pr
                g += pg
                b += pb
                n += 1
    if n == 0:
        return None
    mean = (clamp8(r / n), clamp8(g / n), clamp8(b / n))
    return boost_saturation(mean, boost) if boost != 1 else mean


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> str | None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                data = stream.read(MAX_TOKEN_BYTES + 1)
            if len(data) > MAX_TOKEN_BYTES:
                return None
            return data.decode("utf-8").strip() or None
        except (OSError, UnicodeError):
            return None

    def set(self, token: str | None) -> None:
        if token:
            _atomic_write(self.path, token + "\n")
        else:
            try:
                self.path.unlink()
            except OSError:
                pass


class ScreenCastFlow:
    """Portal handshake against a bus with `request(method, args, options) -> results`
    (raising PortalDenied / PortalError) and `open_pipewire_remote(session) -> fd`."""

    def __init__(self, bus, tokens: TokenStore) -> None:
        self.bus, self.tokens = bus, tokens
        self.session: str | None = None

    def open(self) -> tuple[int, int]:
        res = self.bus.request("CreateSession", [], {
            "session_handle_token": "asusius" + secrets.token_hex(4)})
        self.session = res["session_handle"]
        opts = {"types": SOURCE_MONITOR, "multiple": False, "cursor_mode": CURSOR_HIDDEN,
                "persist_mode": PERSIST_UNTIL_REVOKED}
        token = self.tokens.get()
        if token:
            opts["restore_token"] = token
        self.bus.request("SelectSources", [self.session], opts)
        try:
            res = self.bus.request("Start", [self.session, ""], {})
        except PortalDenied:
            self.tokens.set(None)  # a revoked token must not be retried silently
            raise
        streams = res.get("streams") or []
        if not streams:
            raise PortalError("portal returned no streams")
        self.tokens.set(res.get("restore_token") or token)
        fd = self.bus.open_pipewire_remote(self.session)
        return fd, int(streams[0][0])

    def close(self) -> None:
        if self.session:
            try:
                self.bus.close_session(self.session)
            except Exception:
                pass
            self.session = None
        if hasattr(self.bus, "close"):
            try:
                self.bus.close()
            except Exception:
                pass


class GioPortalBus:  # pragma: no cover - needs a desktop session and a user's consent
    """Gio implementation. Must be used from one thread; it iterates its own context."""

    _SIGS = {"types": "u", "multiple": "b", "cursor_mode": "u", "persist_mode": "u",
             "restore_token": "s", "handle_token": "s", "session_handle_token": "s"}
    _owns_fd = True

    def __init__(self, timeout_s: float = 120.0) -> None:
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.ctx = GLib.MainContext.new()
        self.ctx.push_thread_default()
        self._pushed = True
        self._cancel = threading.Event()
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.timeout_s = timeout_s
            self.sender = self.conn.get_unique_name()[1:].replace(".", "_")
        except BaseException:
            self.close()
            raise

    def cancel(self) -> None:
        self._cancel.set()
        try:
            self.ctx.wakeup()
        except Exception:
            pass

    def close(self) -> None:
        if self._pushed:
            self._pushed = False
            try:
                self.ctx.pop_thread_default()
            except Exception:
                pass

    def _opts(self, options: dict):
        V = self.GLib.Variant
        return {k: V(self._SIGS[k], v) for k, v in options.items()}

    def request(self, method: str, args: list, options: dict) -> dict:
        GLib = self.GLib
        token = "asusius" + secrets.token_hex(6)
        options = {**options, "handle_token": token}
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        box: dict = {}
        done = threading.Event()

        def on_response(_c, _s, _p, _i, _sig, params):
            code, results = params.unpack()
            box["code"], box["results"] = code, results
            done.set()

        sub = self.conn.signal_subscribe(PORTAL, REQUEST, "Response", path, None,
                                         self.Gio.DBusSignalFlags.NONE, on_response)
        try:
            sig = {"CreateSession": "(a{sv})", "SelectSources": "(oa{sv})",
                   "Start": "(osa{sv})"}[method]
            body = GLib.Variant(sig, (*args, self._opts(options)))
            self.conn.call_sync(PORTAL, PORTAL_PATH, SCREENCAST, method, body,
                                GLib.VariantType("(o)"), 0, 10000, None)
            deadline = GLib.get_monotonic_time() + int(self.timeout_s * 1e6)
            while (not done.is_set() and not self._cancel.is_set()
                   and GLib.get_monotonic_time() < deadline):
                self.ctx.iteration(True)
        finally:
            self.conn.signal_unsubscribe(sub)
        if self._cancel.is_set() and not done.is_set():
            raise PortalError(f"{method}: cancelled")
        if not done.is_set():
            raise PortalError(f"{method}: no answer from the portal")
        if box["code"] == 1:
            raise PortalDenied("screen sharing was not allowed")
        if box["code"] != 0:
            raise PortalError(f"{method} failed (code {box['code']})")
        return box["results"]

    def open_pipewire_remote(self, session: str) -> int:
        GLib, Gio = self.GLib, self.Gio
        res, fds = self.conn.call_with_unix_fd_list_sync(
            PORTAL, PORTAL_PATH, SCREENCAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})), GLib.VariantType("(h)"), 0, 10000,
            None, None)
        return fds.get(res.unpack()[0])

    def close_session(self, session: str) -> None:
        self.conn.call_sync(PORTAL, session, "org.freedesktop.portal.Session", "Close",
                            None, None, 0, 2000, None)


def gst_frames(fd: int, node: int, width: int = 16, height: int = 9,
               fps: int = 4):  # pragma: no cover - needs a live PipeWire stream
    """Generator of (bytes, width, height, stride) from a PipeWire node via GStreamer."""
    pipe = None
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        pipe = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={node} always-copy=true ! videoconvert ! videoscale ! "
            f"videorate ! video/x-raw,format=RGB,width={width},height={height},"
            f"framerate={fps}/1 ! appsink name=sink max-buffers=1 drop=true sync=false")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        if pipe is not None:
            pipe.set_state(Gst.State.NULL)
        raise
    sink = pipe.get_by_name("sink")
    pipe.set_state(Gst.State.PLAYING)
    try:
        while True:
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                yield None
                continue
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                s = sample.get_caps().get_structure(0)
                w, h = s.get_value("width"), s.get_value("height")
                yield bytes(info.data), w, h, len(info.data) // h
            finally:
                buf.unmap(info)
    finally:
        pipe.set_state(Gst.State.NULL)


class ScreenColourSource:
    """Background thread: portal handshake, then frames -> latest average colour.

    status: idle | waiting-permission | running | denied | unavailable"""

    def __init__(self, token_path: Path, *, bus_factory: Callable[[], object] | None = None,
                 frames_factory: Callable[[int, int], object] | None = None,
                 boost: float = 1.3) -> None:
        self.tokens = TokenStore(token_path)
        self.bus_factory = bus_factory or GioPortalBus
        self.frames_factory = frames_factory or gst_frames
        self.boost = boost
        self.status = "idle"
        self.error: str | None = None
        self._latest: RGB | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bus = None
        self._thread: threading.Thread | None = None

    @property
    def has_token(self) -> bool:
        return self.tokens.get() is not None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            ev = threading.Event()
            self._stop = ev
            t = threading.Thread(target=self._run, args=(ev,), name="asusius-ambient", daemon=True)
            self._thread = t
            t.start()

    def run_sync(self) -> None:
        """The thread body; tests call it directly."""
        self._run(self._stop)

    def _run(self, stop_ev: threading.Event | None = None) -> None:
        stop_ev = stop_ev or self._stop
        flow = None
        bus = None
        try:
            self.status = "waiting-permission" if not self.has_token else "starting"
            bus = self.bus_factory()
            with self._lock:
                self._bus = bus
            if stop_ev.is_set():
                return
            flow = ScreenCastFlow(bus, self.tokens)
            fd, node = flow.open()
            if stop_ev.is_set():
                if getattr(bus, "_owns_fd", False):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                return
            self.status = "running"
            frames = self.frames_factory(fd, node)
            try:
                for frame in frames:
                    if stop_ev.is_set():
                        break
                    if frame is None:
                        continue
                    data, w, h, stride = frame
                    c = average_rgb(data, w, h, stride, self.boost)
                    if c is not None:
                        self._latest = c
            finally:
                if hasattr(frames, "close"):
                    frames.close()
            if self.status == "running":
                self.status = "idle"
        except PortalDenied as exc:
            self.status, self.error = "denied", str(exc)
        except Exception as exc:  # no portal, no GStreamer plugin, no session bus...
            self.status, self.error = "unavailable", f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._bus = None
            if flow:
                flow.close()
            elif bus is not None and hasattr(bus, "close"):
                try:
                    bus.close()
                except Exception:
                    pass

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            bus = self._bus
            t = self._thread
        if bus is not None and hasattr(bus, "cancel"):
            bus.cancel()
        if t is not None:
            t.join(2)
            with self._lock:
                if self._thread is t and not t.is_alive():
                    self._thread = None

    def latest(self) -> RGB | None:
        return self._latest
