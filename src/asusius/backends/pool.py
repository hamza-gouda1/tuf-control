# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def run_async(fn: Callable, *args, **kwargs) -> Future:
    """Run a blocking call on a shared worker pool; never touches the caller's loop."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="asusius")
        pool = _pool
    return pool.submit(fn, *args, **kwargs)
