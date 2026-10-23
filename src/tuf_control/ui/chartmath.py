"""Display-free chart maths: visible window, downsampling, gaps, monotone curves, ticks.

The curve is a monotone cubic (Fritsch-Carlson): it passes through every sample and
never overshoots between them, so a smooth line cannot show a peak that was not
measured. Values are not averaged except where several samples share one pixel bucket.
"""
from __future__ import annotations

import math

GAP_FACTOR = 2.6  # a pause longer than this many sample intervals breaks the line


def nice_step(span: float, target: int = 4) -> float:
    if span <= 0:
        return 1.0
    raw = span / target
    exp = math.floor(math.log10(raw))
    base = 10 ** exp
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * base + 1e-12:
            return m * base
    return 10 * base


def nice_range(lo: float, hi: float, floor: float | None = 0.0, min_span: float = 1.0,
               target: int = 4) -> tuple[float, float, float]:
    """(axis min, axis max, tick step) covering [lo, hi] with round ticks."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = (floor or 0.0), (floor or 0.0) + min_span
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    if floor is not None and lo < floor:
        hi += floor - lo
        lo = floor
    step = nice_step(hi - lo, target)
    a = math.floor(lo / step) * step
    b = math.ceil(hi / step) * step
    if floor is not None and a < floor:
        a = floor
    if b <= a:
        b = a + step
    return a, b, step


def window_points(times, values, idx_iter, slot, t0: float, t1: float, width: float,
                  out_x: list, out_y: list, bucket_px: float = 1.5, use_max: bool = True,
                  interval: float = 1.0) -> int:
    """Fill out_x/out_y with (pixel x, value) for samples in [t0, t1]; NaN marks a gap.

    When more samples than width/bucket_px fall in the window, each bucket keeps its max
    (spikes stay visible) or mean. Returns the number of points written.
    """
    span = t1 - t0
    if span <= 0 or width <= 0:
        return 0
    nb = max(2, int(width / bucket_px))
    n = 0
    prev_t = None
    cur_b = -1
    acc = 0.0
    acc_n = 0
    acc_x = 0.0
    gap_s = interval * GAP_FACTOR
    out_x.clear()
    out_y.clear()
    for k in idx_iter:
        s = slot(k)
        t = times[s]
        if t < t0:
            continue
        v = values[s]
        if prev_t is not None and t - prev_t > gap_s:
            if acc_n:
                out_x.append(acc_x)
                out_y.append(acc if use_max else acc / acc_n)
                acc_n = 0
                n += 1
            out_x.append(math.nan)
            out_y.append(math.nan)
            n += 1
            cur_b = -1
        prev_t = t
        x = (t - t0) / span * width
        if v != v:  # NaN sample: a gap
            if acc_n:
                out_x.append(acc_x)
                out_y.append(acc if use_max else acc / acc_n)
                acc_n = 0
                n += 1
            if n and out_y[-1] == out_y[-1]:
                out_x.append(math.nan)
                out_y.append(math.nan)
                n += 1
            cur_b = -1
            continue
        b = int(x / width * nb)
        if b != cur_b and acc_n:
            out_x.append(acc_x)
            out_y.append(acc if use_max else acc / acc_n)
            n += 1
            acc_n = 0
        if acc_n == 0:
            acc = v
            acc_n = 1
        else:
            acc = max(acc, v) if use_max else acc + v
            acc_n += 1
        acc_x = x
        cur_b = b
    if acc_n:
        out_x.append(acc_x)
        out_y.append(acc if use_max else acc / acc_n)
        n += 1
    return n


def segments(xs: list, ys: list) -> list[tuple[int, int]]:
    """[start, end) runs between NaN gaps."""
    runs = []
    start = None
    for i, y in enumerate(ys):
        if y != y:
            if start is not None and i - start > 0:
                runs.append((start, i))
            start = None
        elif start is None:
            start = i
    if start is not None:
        runs.append((start, len(ys)))
    return runs


def monotone_tangents(xs, ys, a: int, b: int) -> list[float]:
    """Fritsch-Carlson tangents for points a..b-1 (no overshoot between samples)."""
    n = b - a
    if n < 2:
        return [0.0] * n
    d = []
    for i in range(a, b - 1):
        dx = xs[i + 1] - xs[i]
        d.append((ys[i + 1] - ys[i]) / dx if dx else 0.0)
    m = [d[0]] + [0.0] * (n - 2) + [d[-1]]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0
        else:
            m[i] = (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        al, be = m[i] / d[i], m[i + 1] / d[i]
        s = al * al + be * be
        if s > 9:
            t = 3 / math.sqrt(s)
            m[i], m[i + 1] = t * al * d[i], t * be * d[i]
    return m


def bezier_controls(xs, ys, a: int, b: int, px_y) -> list[tuple[float, ...]]:
    """Cubic Bezier segments (x1, y1, x2, y2, x3, y3) in pixels for the run a..b-1.

    px_y maps a value to a pixel y; tangents are computed in value space.
    """
    m = monotone_tangents(xs, ys, a, b)
    out = []
    for j, i in enumerate(range(a, b - 1)):
        h = xs[i + 1] - xs[i]
        y0, y1 = ys[i], ys[i + 1]
        c1 = y0 + m[j] * h / 3
        c2 = y1 - m[j + 1] * h / 3
        out.append((xs[i] + h / 3, px_y(c1), xs[i + 1] - h / 3, px_y(c2), xs[i + 1], px_y(y1)))
    return out
