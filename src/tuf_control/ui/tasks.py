"""Run blocking controller calls off the GTK main thread; deliver results on it."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import GLib

from ..backends.pool import run_async


def run_bg(fn: Callable, *args, done: Callable | None = None,
           error: Callable[[BaseException], None] | None = None, **kwargs) -> None:
    """Call fn(*args) on the worker pool; done(result) / error(exc) run on the main loop."""
    fut = run_async(fn, *args, **kwargs)

    def deliver(f):
        exc = f.exception()
        if exc is not None:
            if error:
                error(exc)
        elif done:
            done(f.result())
        return GLib.SOURCE_REMOVE

    fut.add_done_callback(lambda f: GLib.idle_add(deliver, f))
