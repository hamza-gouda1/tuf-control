from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

_pool: ThreadPoolExecutor | None = None


def run_async(fn: Callable, *args, **kwargs) -> Future:
    """Run a blocking call on a shared worker pool; never touches the caller's loop."""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tuf-control")
    return _pool.submit(fn, *args, **kwargs)
