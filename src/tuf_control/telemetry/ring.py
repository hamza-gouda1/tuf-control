"""Fixed-size history: one shared time ring plus one float32 ring per series.

All series are sampled on the same tick, so they share the time axis. Storage is
preallocated `array` memory (4 bytes per value, 8 per timestamp); pushing a sample
allocates nothing per series. A value the source could not read is stored as NaN and
drawn as a gap, never as zero.
"""
from __future__ import annotations

import math
from array import array

NAN = float("nan")
DEFAULT_CAPACITY = 300  # 120 s at 2 Hz plus headroom; at 1 Hz it covers 5 minutes


class Store:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = capacity
        self.times = array("d", bytes(8 * capacity))
        self.series: dict[str, array] = {}
        self.head = 0  # next write slot
        self.count = 0
        self.last_t: float | None = None

    # -- writing ----------------------------------------------------------------------
    def _new_series(self) -> array:
        return array("f", [NAN]) * self.capacity

    def push(self, t: float, values: dict[str, float | None]) -> None:
        """Append one tick. Keys never seen before get a NaN-filled history."""
        if self.last_t is not None and t <= self.last_t:
            return  # monotonic clock only; a duplicate tick is dropped
        i = self.head
        self.times[i] = t
        for key, v in values.items():
            arr = self.series.get(key)
            if arr is None:
                arr = self.series[key] = self._new_series()
            arr[i] = NAN if v is None else v
        if len(values) != len(self.series):
            for key, arr in self.series.items():
                if key not in values:
                    arr[i] = NAN
        self.head = (i + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)
        self.last_t = t

    def clear(self) -> None:
        self.head = self.count = 0
        self.last_t = None
        for arr in self.series.values():
            for i in range(self.capacity):
                arr[i] = NAN

    # -- reading ----------------------------------------------------------------------
    def index_range(self, since: float | None = None) -> range:
        """Logical indices 0..count-1 (oldest first) whose time is >= since."""
        n = self.count
        start = 0
        if since is not None and n:
            # binary search on the logical order
            lo, hi = 0, n
            cap, base = self.capacity, self.head - n
            times = self.times
            while lo < hi:
                mid = (lo + hi) // 2
                if times[(base + mid) % cap] < since:
                    lo = mid + 1
                else:
                    hi = mid
            start = lo
        return range(start, n)

    def slot(self, logical: int) -> int:
        return (self.head - self.count + logical) % self.capacity

    def last(self, key: str) -> float | None:
        arr = self.series.get(key)
        if arr is None or not self.count:
            return None
        v = arr[(self.head - 1) % self.capacity]
        return None if math.isnan(v) else float(v)

    def has(self, key: str) -> bool:
        return key in self.series

    def stats(self, key: str, since: float | None = None) -> tuple[float, float, float] | None:
        """(min, max, mean) over the window, ignoring gaps; None when empty."""
        arr = self.series.get(key)
        if arr is None:
            return None
        lo = math.inf
        hi = -math.inf
        total = 0.0
        n = 0
        for k in self.index_range(since):
            v = arr[self.slot(k)]
            if v != v:  # NaN
                continue
            if v < lo:
                lo = v
            if v > hi:
                hi = v
            total += v
            n += 1
        if not n:
            return None
        return lo, hi, total / n

    def values(self, key: str, since: float | None = None) -> list[tuple[float, float]]:
        """(t, v) pairs oldest first, NaN kept (tests and validation; charts read in place)."""
        arr = self.series.get(key)
        if arr is None:
            return []
        out = []
        for k in self.index_range(since):
            s = self.slot(k)
            out.append((self.times[s], arr[s]))
        return out

    def nbytes(self) -> int:
        return self.times.itemsize * self.capacity + sum(
            a.itemsize * self.capacity for a in self.series.values())
