# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Live state of an update run, shared between the worker thread and the page.

The worker calls `update()` / `line()` as the backends report progress; the page takes a
`snapshot()` a few times a second (it never blocks on the worker). Cancel is a request: a
backend that can cancel (PackageKit while AllowCancel, snapd changes, flatpak) honours it.
"""
from __future__ import annotations

import threading
import time
from collections import deque

LOG_LINES = 400
_UNSET = object()
TICK_MS = 250               # the page snapshots at about 4 fps


class SourceWatch:
    """What one backend reports while it runs, forwarded into the RunState."""

    def __init__(self, state: "RunState", source: str) -> None:
        self.state, self.source = state, source

    def phase(self, text: str = "", *, percent=_UNSET, package=_UNSET,
              speed=_UNSET, remaining_s=_UNSET, auth: bool | None = None,
              cancellable: bool | None = None, done: bool = False,
              error: str = "") -> None:
        kw = {}
        if text:
            kw.update(phase=text, auth=False if auth is None else bool(auth))
        elif auth is not None:
            kw["auth"] = bool(auth)
        if percent is not _UNSET:
            kw["percent"] = None if percent is None else max(0.0, min(100.0, float(percent)))
        if package is not _UNSET:
            kw["package"] = str(package or "")[:120]
        if speed is not _UNSET:
            kw["speed"] = None if speed is None else max(0.0, float(speed))
        if remaining_s is not _UNSET:
            kw["remaining_s"] = None if remaining_s is None else max(0.0, float(remaining_s))
        if cancellable is not None:
            kw["cancellable"] = bool(cancellable)
        if done:
            kw.update(done=True, auth=False, cancellable=False, speed=None, remaining_s=None)
        if error:
            kw["error"] = error[:300]
        self.state.update(self.source, **kw)

    def line(self, text: str) -> None:
        self.state.line(self.source, text)

    def callback(self):
        """A `progress(text)` function for simple backends: a log line and the package."""
        return self.state.callback(self.source)


class RunState:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self.started = clock()
        self.ended: float | None = None
        self.sources: dict[str, dict] = {}
        self.order: list[str] = []
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.lines = 0               # total lines ever logged (the page appends the new ones)
        self.cancel = threading.Event()
        self.current: str | None = None
        self.summary: str = ""

    # -- worker side --------------------------------------------------------------------
    def update(self, source: str, **kw) -> None:
        with self._lock:
            st = self.sources.get(source)
            if st is None:
                st = self.sources[source] = {"phase": "Waiting", "percent": None,
                                             "package": "", "speed": None,
                                             "remaining_s": None, "auth": False,
                                             "cancellable": False, "done": False,
                                             "error": ""}
                self.order.append(source)
            st.update(kw)
            if not kw.get("done"):
                self.current = source

    def line(self, source: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            if self.log and self.log[-1].endswith(f"{source}: {text}"):
                return  # repeated status lines
            self.log.append(f"{stamp}  {source}: {text}")
            self.lines += 1

    def finish(self, summary: str = "") -> None:
        with self._lock:
            self.ended = self.clock()
            self.summary = summary
            self.current = None

    def callback(self, source: str):
        """A `progress(text)` function for simple backends: each call is a log line."""
        def cb(text: str) -> None:
            self.update(source, package=text[:120])
            self.line(source, text)
        return cb

    def watcher(self, source: str) -> SourceWatch:
        """A live handle for one backend (see actions); phase/line, thread-safe."""
        return SourceWatch(self, source)

    # -- page side --------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.ended is None

    def elapsed(self) -> float:
        return (self.ended if self.ended is not None else self.clock()) - self.started

    def snapshot(self) -> dict:
        with self._lock:
            return {"sources": [(k, dict(self.sources[k])) for k in self.order],
                    "log": list(self.log), "lines": self.lines, "elapsed": self.elapsed(),
                    "running": self.running, "current": self.current,
                    "summary": self.summary, "cancel_requested": self.cancel.is_set(),
                    "auth": any(s.get("auth") and not s.get("done")
                                for s in self.sources.values()),
                    "cancellable": any(s.get("cancellable") and not s.get("done")
                                       for s in self.sources.values())}


def fmt_duration(s: float | None) -> str:
    if s is None or s < 0:
        return ""
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else \
        f"{s // 60}:{s % 60:02d}"


def fmt_speed(bps: float | None) -> str:
    if not bps:
        return ""
    for unit, size in (("GB/s", 1e9), ("MB/s", 1e6), ("kB/s", 1e3)):
        if bps >= size:
            return f"{bps / size:.1f} {unit}"
    return f"{bps:.0f} B/s"
