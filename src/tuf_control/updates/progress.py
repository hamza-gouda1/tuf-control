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

    # -- page side --------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.ended is None

    def elapsed(self) -> float:
        return (self.ended or self.clock()) - self.started

    def snapshot(self) -> dict:
        with self._lock:
            return {"sources": [(k, dict(self.sources[k])) for k in self.order],
                    "log": list(self.log), "lines": self.lines, "elapsed": self.elapsed(),
                    "running": self.running, "current": self.current,
                    "summary": self.summary, "cancel_requested": self.cancel.is_set(),
                    "auth": any(s.get("auth") for s in self.sources.values()),
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
