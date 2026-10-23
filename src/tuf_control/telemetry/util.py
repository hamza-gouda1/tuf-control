"""Small, allocation-light file helpers for /proc and /sys.

Every reader takes a `root` so the same code runs against the live system ("/") or a
fixture/fake tree. Nothing here ever writes.
"""
from __future__ import annotations

import os

NAN = float("nan")


def read_bytes(path: str, limit: int = 1 << 20) -> bytes | None:
    """Whole file as bytes (proc files may need several reads); None when unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        chunk = os.read(fd, 65536)
        if len(chunk) < 65536:
            return chunk
        parts = [chunk]
        total = len(chunk)
        while total < limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
        return b"".join(parts)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_str(path: str) -> str | None:
    data = read_bytes(path, 1 << 16)
    if data is None:
        return None
    try:
        return data.decode().strip()
    except UnicodeDecodeError:
        return None


def read_int(path: str) -> int | None:
    data = read_bytes(path, 4096)
    if not data:
        return None
    try:
        return int(data)
    except ValueError:
        return None


def listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def join(root: str, rel: str) -> str:
    """root + absolute-style rel ('proc/stat' or '/proc/stat')."""
    return os.path.join(root, rel.lstrip("/"))


def rate(cur: float, prev: float, dt: float) -> float | None:
    """Counter delta per second; None on the first sample, a reset or a zero interval."""
    if dt <= 0 or cur < prev:
        return None
    return (cur - prev) / dt


def nice_ceiling(v: float) -> float:
    """Smallest 1/2/2.5/5 x 10^n at or above v (axis maxima)."""
    if v <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(v))
    base = 10 ** exp
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * base + 1e-9:
            return m * base
    return 10 * base
