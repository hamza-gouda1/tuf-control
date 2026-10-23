"""Display formatting for telemetry values (GTK-free, unit-tested)."""
from __future__ import annotations

import math

DASH = "–"


def ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def num(v, digits: int = 0) -> str:
    return f"{v:.{digits}f}" if ok(v) else DASH


def pct(v, digits: int = 0) -> str:
    return f"{v:.{digits}f} %" if ok(v) else DASH


def bytes_(v, digits: int = 1) -> str:
    """Binary units, as `free -h` and file managers' 'GiB' figures."""
    if not ok(v):
        return DASH
    v = float(v)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(v) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{v:.0f} B"
            return f"{v:.{digits}f} {unit}"
        v /= 1024
    return DASH


def split_bytes(v, digits: int = 1) -> tuple[str, str]:
    s = bytes_(v, digits)
    return tuple(s.split(" ", 1)) if " " in s else (s, "")  # type: ignore[return-value]


def rate(v, digits: int = 1) -> str:
    """Bytes per second in decimal units (kB/s, MB/s), like network tools."""
    if not ok(v):
        return DASH
    v = float(v)
    for unit in ("B/s", "kB/s", "MB/s", "GB/s"):
        if abs(v) < 1000 or unit == "GB/s":
            return f"{v:.0f} {unit}" if unit == "B/s" else f"{v:.{digits}f} {unit}"
        v /= 1000
    return DASH


def bits(v) -> str:
    if not ok(v):
        return DASH
    v = float(v) * 8
    for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if abs(v) < 1000 or unit == "Gbit/s":
            return f"{v:.0f} {unit}" if unit == "bit/s" else f"{v:.1f} {unit}"
        v /= 1000
    return DASH


def mhz(v) -> str:
    if not ok(v):
        return DASH
    return f"{v / 1000:.2f} GHz" if v >= 1000 else f"{v:.0f} MHz"


def temp(v) -> str:
    return f"{v:.0f} °C" if ok(v) else DASH


def watts(v, digits: int = 1) -> str:
    return f"{v:.{digits}f} W" if ok(v) else DASH


def rpm(v) -> str:
    return f"{int(v):,}".replace(",", " ") + " rpm" if ok(v) else DASH


def duration(seconds) -> str:
    if not ok(seconds):
        return DASH
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} d {h} h {m} min"
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def hours(h) -> str:
    if not ok(h):
        return DASH
    m = int(round(h * 60))
    return f"{m // 60} h {m % 60:02d} min"


def count(v) -> str:
    if not ok(v):
        return DASH
    v = float(v)
    if v >= 1e6:
        return f"{v / 1e6:.1f} M"
    if v >= 1e4:
        return f"{v / 1e3:.0f} k"
    return f"{v:.0f}"


def axis_rate(v) -> str:
    """Short axis label for a bytes/s value."""
    if v >= 1e9:
        return f"{v / 1e9:g} GB/s"
    if v >= 1e6:
        return f"{v / 1e6:g} MB/s"
    if v >= 1e3:
        return f"{v / 1e3:g} kB/s"
    return f"{v:g} B/s"


def axis_bytes(v) -> str:
    if v >= 1 << 30:
        return f"{v / (1 << 30):g} GiB"
    if v >= 1 << 20:
        return f"{v / (1 << 20):g} MiB"
    return f"{v / 1024:g} KiB" if v else "0"
