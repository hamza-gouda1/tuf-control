"""Append-only history of update runs: ~/.local/state/tuf-control/updates-history.jsonl.

One JSON object per line and run: start/end, trigger (manual, automatic, detected), the
items with old and new version and the verified result (installed, pending, failed,
restart), reboot flag, duration. The file is 0600 and rotated to `.1` at 1 MiB (two
generations kept). "detected" runs record updates that happened outside TUF Control
(unattended-upgrades, GNOME Software, a terminal) and were noticed by a later check.
"""
from __future__ import annotations

import json
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


def append(run: dict, path: Path | None = None, max_bytes: int = MAX_BYTES) -> None:
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(run, separators=(",", ":"), default=str) + "\n").encode()
    try:
        if path.stat().st_size + len(data) > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def read(since_ts: float | None = None, source: str | None = None, path: Path | None = None,
         limit: int = 500) -> list[dict]:
    """Runs, newest first. `source` keeps runs (and their items) from that source."""
    path = path or history_path()
    runs = []
    for line in _lines(path.with_name(path.name + ".1")) + _lines(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue  # a torn last line after a crash
        if not isinstance(r, dict):
            continue
        if since_ts is not None and float(r.get("end") or r.get("start") or 0) < since_ts:
            continue
        if source:
            items = [i for i in r.get("items") or [] if i.get("source") == source]
            if not items:
                continue
            r = {**r, "items": items}
        runs.append(r)
    runs.reverse()
    return runs[:limit]


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
