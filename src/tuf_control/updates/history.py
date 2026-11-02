# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Append-only history of update runs: ~/.local/state/tuf-control/updates-history.jsonl.

One JSON object per line and run: start/end, trigger (manual, automatic, detected), the
items with old and new version and the verified result (installed, pending, failed,
restart), reboot flag, duration. The file is 0600 and rotated to `.1` at 1 MiB (two
generations kept). "detected" runs record updates that happened outside TUF Control
(unattended-upgrades, GNOME Software, a terminal) and were noticed by a later check.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import stat
import os
import re
import time
from pathlib import Path

MAX_BYTES = 1 << 20
STATE_DIR_ENV = "TUF_CONTROL_STATE_DIR"


def history_path() -> Path:
    from ..controller import FAKE_ROOT_ENV
    if os.environ.get(STATE_DIR_ENV):
        return Path(os.environ[STATE_DIR_ENV]) / "updates-history.jsonl"
    fake = os.environ.get(FAKE_ROOT_ENV)
    if fake:  # never the user's real history with a fake tree
        return Path(fake) / "fake-cache" / "updates-history.jsonl"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tuf-control" / "updates-history.jsonl"


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_name(path.name + ".lock"),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("history lock is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _open_append(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW |
                 os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("history is not a regular file")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def append(run: dict, path: Path | None = None, max_bytes: int = MAX_BYTES) -> None:
    path = path or history_path()
    data = (json.dumps(run, separators=(",", ":"), default=str, allow_nan=False) + "\n").encode()
    if len(data) > MAX_BYTES:
        raise ValueError("one update history entry exceeds 1 MiB")
    with _locked(path):
        fd = _open_append(path)
        try:
            if os.fstat(fd).st_size + len(data) > max_bytes and os.fstat(fd).st_size:
                os.replace(path, path.with_name(path.name + ".1"))
                os.close(fd)
                fd = _open_append(path)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("could not append update history")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


def _lines(path: Path) -> list[str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return []
            start = max(0, st.st_size - MAX_BYTES)
            f.seek(start)
            data = f.read(MAX_BYTES)
        lines = data.decode(errors="replace").splitlines()
        return lines[1:] if start else lines
    except OSError:
        return []


def read(since_ts: float | None = None, source: str | None = None, path: Path | None = None,
         limit: int = 500) -> list[dict]:
    """Runs, newest first. Malformed/torn records are skipped; reads are bounded."""
    path = path or history_path()
    if not path.parent.exists():
        return []
    try:
        with _locked(path):
            lines = _lines(path.with_name(path.name + ".1")) + _lines(path)
    except OSError:
        return []
    runs = []
    for line in lines:
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                continue
            for key in ("start", "end", "duration_s"):
                value = r.get(key)
                if value is not None and (isinstance(value, bool) or
                        not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError("invalid timestamp or duration")
            when = r.get("end") or r.get("start") or 0
            if since_ts is not None and when < since_ts:
                continue
            items = r.get("items") or []
            if not isinstance(items, list):
                continue
            r["items"] = [i for i in items if isinstance(i, dict)]
        except (ValueError, TypeError, OverflowError):
            continue
        if source:
            r["items"] = [i for i in r["items"] if i.get("source") == source]
            if not r["items"]:
                continue
        runs.append(r)
    runs.reverse()
    return runs[:max(0, min(int(limit), 5000))]


_SINCE = re.compile(r"^(\d+)\s*([mhdw])$")


def parse_since(text: str | None, now: float | None = None) -> float | None:
    """'7d', '24h', '30m', '2w', 'yesterday', 'today' -> a timestamp (None for 'all')."""
    if not text or text == "all":
        return None
    now = time.time() if now is None else now
    if text in ("today", "yesterday"):
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return midnight - (86400 if text == "yesterday" else 0)
    m = _SINCE.match(text.strip())
    if not m:
        raise ValueError(f"since: use e.g. 7d, 24h, 30m, 2w, today, yesterday (not {text!r})")
    return now - int(m[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]


def changes(runs: list[dict]) -> list[dict]:
    """Every item that actually changed, newest first (for 'what changed since...')."""
    out = []
    for r in runs:
        for i in r.get("items") or []:
            if i.get("result") in ("installed", "restart"):
                out.append({**i, "when": r.get("end") or r.get("start"),
                            "trigger": r.get("trigger")})
    return out


def run_title(r: dict) -> str:
    items = r.get("items") or []
    n = len(items)
    ok = sum(1 for i in items if i.get("result") in ("installed", "restart"))
    if r.get("trigger") == "detected":
        return f"{ok} updated outside TUF Control"
    return f"{ok} of {n} installed" if n else "Nothing to install"
