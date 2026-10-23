"""The GUI's single telemetry timer.

One GLib timer drives the shared Sampler: 1 Hz normally, 2 Hz while a monitor detail
page is visible, stopped while the window is hidden or minimised. Each tick reads the
sources on the worker pool (never two at once) and commits on the main loop, then
notifies listeners. The process walk joins the same tick, only while asked for.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from gi.repository import GLib

from ..backends.pool import run_async
from ..telemetry import Sampler

PROCS_EVERY_S = 2.0


class LiveHub:
    def __init__(self, fake_root: str | None = None) -> None:
        self.fake = fake_root is not None
        root = fake_root or "/"
        self.world = None
        if self.fake:
            from ..telemetry.fake import FakeWorld
            self.world = FakeWorld(Path(root))
            self.world.advance(time.monotonic() - 1.0)
        self.sampler = Sampler(root, fake=self.fake)
        self.store = self.sampler.store
        self.root = root
        self.interval_s = 1.0
        self.listeners: list = []
        self._timer: int | None = None
        self._busy = False
        self.running = False
        self.hold = False  # screenshot harness: drive ticks by hand with a fake clock
        self.ticks = 0
        self.procs_wanted = False
        self.procs: list = []
        self.procs_t = -1e9
        self.procs_gen = 0
        self._walker = None

    # -- control ----------------------------------------------------------------------
    def start(self) -> None:
        if self.running or self.hold:
            return
        self.running = True
        self._schedule()
        self.tick()

    def stop(self) -> None:
        self.running = False
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def close(self) -> None:
        self.stop()
        self.sampler.close()

    def set_interval(self, seconds: float) -> None:
        if abs(seconds - self.interval_s) < 1e-6:
            return
        self.interval_s = seconds
        if self.running:
            self._schedule()

    def _schedule(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
        if self.interval_s >= 1.0 and float(self.interval_s).is_integer():
            # second-granular timers are batched by GLib with other wakeups
            self._timer = GLib.timeout_add_seconds(int(self.interval_s), self._on_timer)
        else:
            self._timer = GLib.timeout_add(int(self.interval_s * 1000), self._on_timer)

    def want_processes(self, on: bool) -> None:
        self.procs_wanted = on
        if not on:
            self.procs = []
            if self._walker is not None:
                self._walker.clear()
                self._walker = None
        else:
            self.procs_t = -1e9

    def _on_timer(self) -> bool:
        self.tick()
        return GLib.SOURCE_CONTINUE

    # -- one tick ---------------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        if self._busy:
            return
        self._busy = True
        fut = run_async(self._collect, now)
        fut.add_done_callback(lambda f: GLib.idle_add(self._deliver, f))

    def _collect(self, now):
        now = time.monotonic() if now is None else now
        if self.world is not None:
            self.world.advance(now)
        now, vals, info = self.sampler.collect(now)
        procs = None
        if self.procs_wanted and now - self.procs_t >= PROCS_EVERY_S - 0.05:
            if self._walker is None:
                from ..telemetry.procs import ProcessWalker
                self._walker = ProcessWalker(self.root, self.sampler.cpu.static["count"])
            walker = self._walker
            procs = walker.walk(now, info.get("igpu", {}).get("per_pid"),
                                info.get("dgpu", {}).get("procs"))
            self.procs_t = now
        return now, vals, info, procs

    def _deliver(self, fut) -> bool:
        self._busy = False
        exc = fut.exception()
        if exc is not None:
            import sys
            print(f"telemetry tick failed: {exc!r}", file=sys.stderr)
            return GLib.SOURCE_REMOVE
        now, vals, info, procs = fut.result()
        self.sampler.commit(now, vals, info)
        if procs is not None and self.procs_wanted:
            self.procs = procs
            self.procs_gen += 1
        self.ticks += 1
        for fn in list(self.listeners):
            fn(self)
        return GLib.SOURCE_REMOVE

    def collect_now(self, now: float, notify: bool = False) -> None:
        """Synchronous tick (screenshot harness: build history with a fake clock)."""
        now, vals, info, procs = self._collect(now)
        self.sampler.commit(now, vals, info)
        if procs is not None:
            self.procs = procs
            self.procs_gen += 1
        if notify:
            self.ticks += 1
            for fn in list(self.listeners):
                fn(self)

    @property
    def info(self) -> dict:
        return self.sampler.info


def rss_kib() -> int | None:
    """This process's resident set (About page / PERFORMANCE.md figures)."""
    try:
        with open(f"/proc/{os.getpid()}/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 1024
    except OSError:
        return None
