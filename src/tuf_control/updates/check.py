"""Run every update source once and cache the report in ~/.cache/tuf-control/updates.json.

    python3 -m tuf_control.updates.check [--json]

One check at a time per user (flock on updates.lock): a second caller waits for the
running check and then reads its result. The GUI and the daemon run the check in this
child process so the package cache (tens of MB for apt) is freed when it ends.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .model import Report, SourceResult

CACHE_DIR_ENV = "TUF_CONTROL_CACHE_DIR"
CACHE_NAME = "updates.json"
CHECK_TIMEOUT_S = 240


def _fake_root() -> str | None:
    from ..controller import FAKE_ROOT_ENV
    return os.environ.get(FAKE_ROOT_ENV) or None


def cache_dir() -> Path:
    if os.environ.get(CACHE_DIR_ENV):
        return Path(os.environ[CACHE_DIR_ENV])
    fake = _fake_root()
    if fake:  # never the user's real cache with a fake tree
        return Path(fake) / "fake-cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tuf-control"


def cache_path() -> Path:
    return cache_dir() / CACHE_NAME


def load_cache(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or cache_path()).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_report(path: Path | None = None) -> Report | None:
    d = load_cache(path).get("report")
    if not d:
        return None
    try:
        return Report.from_dict(d)
    except (KeyError, TypeError, ValueError):
        return None


def update_cache(fn, path: Path | None = None) -> dict:
    """Locked read-modify-write of the cache document (report + notification state)."""
    from ..automation.config import write_json_atomic
    path = path or cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".wlock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = load_cache(path)
        fn(data)
        write_json_atomic(path, data)
    return data


@contextlib.contextmanager
def check_lock(path: Path | None = None, wait: bool = True):
    """Yields True when this caller got the lock (should check), False when it waited for
    someone else's check (should just read the cache)."""
    path = path or cache_dir() / "updates.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            if not wait:
                yield None
                return
            fcntl.flock(f, fcntl.LOCK_EX)  # someone else is checking: wait for it
            got = False
        try:
            yield got
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_checking(path: Path | None = None) -> bool:
    path = path or cache_dir() / "updates.lock"
    try:
        with open(path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except OSError:
        return True


def _timed(name: str, fn) -> SourceResult:
    t0 = time.monotonic()
    try:
        res = fn()
    except Exception as exc:  # a broken source never breaks the report
        res = SourceResult(name, error=f"{type(exc).__name__}: {exc}")
    res.duration_s = round(time.monotonic() - t0, 2)
    return res


def collect(settings: dict | None = None, now: float | None = None) -> Report:
    """Query every enabled source (no cache, no lock)."""
    from .settings import load_settings
    settings = settings or load_settings()
    now = now or time.time()
    if _fake_root():
        from .fake import fake_sources
        sources = fake_sources(now)
    else:
        from . import apt, firmware, flatpak, repos, snap, system
        enabled = settings.get("sources", {})
        plan = [("apt", apt.check), ("snap", snap.check), ("flatpak", flatpak.check),
                ("firmware", firmware.check), ("system", system.check)]
        sources = []
        for name, fn in plan:
            if not enabled.get(name, True):
                sources.append(SourceResult(name, available=False, reason="turned off in "
                                                                          "settings"))
                continue
            sources.append(_timed(name, fn))
        if settings.get("check_repos"):
            sources.append(_timed("repos", lambda: repos.check(settings.get("repos", []))))
    rep = Report(checked_ts=now, sources=sources, fake=bool(_fake_root()))
    for s in sources:
        for i in s.items:
            if i.apply_method == "reboot":
                rep.reboot_required = True
                rep.reboot_reasons.append(i.name if i.kind != "reboot" else i.description)
    return rep


def run_check(settings: dict | None = None) -> Report:
    """Check (or wait for the check already running) and cache the result."""
    with check_lock() as mine:
        if not mine:
            rep = load_report()
            if rep is not None:
                return rep
        rep = collect(settings)

        def put(d):
            d["report"] = rep.to_dict()
        update_cache(put)
        return rep


def check_in_child(timeout_s: float = CHECK_TIMEOUT_S) -> Report:
    """run_check() in a child Python, so its memory is returned when it exits."""
    pkg_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(x for x in (pkg_root, env.get("PYTHONPATH")) if x)
    r = subprocess.run([sys.executable, "-m", "tuf_control.updates.check"], env=env,
                       capture_output=True, text=True, timeout=timeout_s,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["update check failed"]
        raise RuntimeError(tail[0])
    rep = load_report()
    if rep is None:
        raise RuntimeError("update check wrote no report")
    return rep


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python3 -m tuf_control.updates.check")
    p.add_argument("--json", action="store_true", help="print the report")
    args = p.parse_args(argv)
    rep = run_check()
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
