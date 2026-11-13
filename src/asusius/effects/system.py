# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""CPU load from /proc/stat (read-only), for the CPU-load effect."""
from __future__ import annotations

from pathlib import Path


def read_cpu_times(path: Path = Path("/proc/stat")) -> tuple[int, int] | None:
    """(busy, total) jiffies over all cores, or None when unreadable."""
    try:
        with open(path) as f:
            first = f.readline().split()
    except OSError:
        return None
    if not first or first[0] != "cpu":
        return None
    vals = [int(x) for x in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals[:8])  # guest time is already counted in user/nice
    return total - idle, total


class CpuLoad:
    """Callable: load 0..1 since the previous call (None on the first call)."""

    def __init__(self, reader=read_cpu_times) -> None:
        self.reader = reader
        self.prev: tuple[int, int] | None = None
        self.value: float | None = None

    def __call__(self) -> float | None:
        cur = self.reader()
        if cur is None:
            return None
        if self.prev is not None:
            busy = cur[0] - self.prev[0]
            total = cur[1] - self.prev[1]
            if total > 0:
                self.value = max(0.0, min(1.0, busy / total))
        self.prev = cur
        return self.value
