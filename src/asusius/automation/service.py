# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Start, stop and query `asusiusd` from the GUI and CLI.

With the systemd user unit installed (and no fake tree) start/stop go through
`systemctl --user`; otherwise the daemon is spawned detached with the current Python and
stopped with SIGTERM via its pid. Status comes from the daemon's status file."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
import threading
import stat
from pathlib import Path

from ..controller import FAKE_ROOT_ENV
from ..core import BackendError
from .config import runtime_dir

UNIT = "asusiusd.service"
_SPAWNED: list = []  # active detached children; poll() reaps and removes finished ones
_SPAWN_LOCK = threading.Lock()
MAX_LOG_BYTES = 1 << 20


def _prune_children() -> None:
    with _SPAWN_LOCK:
        _SPAWNED[:] = [p for p in _SPAWNED if getattr(p, "poll", lambda: None)() is None]


def _rotate_if_oversized(path: Path) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISREG(st.st_mode) and st.st_size >= MAX_LOG_BYTES:
        try:
            os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass


def _capture_log(stream, path: Path) -> None:
    """Drain the child's pipe with bounded memory and a 1 MiB rotating private log."""
    fd = None
    try:
        while True:
            chunk = getattr(stream, "read1", stream.read)(8192)
            if not chunk:
                break
            if fd is None:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW |
                             os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("daemon log must be a regular file")
                os.fchmod(fd, 0o600)
            if os.fstat(fd).st_size + len(chunk) > MAX_LOG_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
                os.close(fd)
                fd = None
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW |
                             os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
            remaining = memoryview(chunk)
            while remaining:
                n = os.write(fd, remaining)
                if n <= 0:
                    raise OSError("could not append daemon log")
                remaining = remaining[n:]
    except OSError:
        # Keep draining after a logging failure so the child cannot block on a full pipe.
        while stream.read(8192):
            pass
    finally:
        if fd is not None:
            os.close(fd)
        stream.close()
        _prune_children()
STALE_S = 10.0


def _alive(pid: int, proc: Path = Path("/proc")) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        cmd = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return True
    return b"asusius.daemon" in cmd or b"asusiusd" in cmd


class DaemonClient:
    def __init__(self, runtime: Path | None = None, *, run=subprocess.run,
                 popen=subprocess.Popen, env: dict | None = None, alive=_alive,
                 sleep=time.sleep) -> None:
        self.runtime = runtime or runtime_dir()
        self.run, self.popen, self.alive, self.sleep = run, popen, alive, sleep
        self.env = dict(os.environ if env is None else env)

    @property
    def status_file(self) -> Path:
        return self.runtime / "status.json"

    def status(self) -> dict:
        _prune_children()
        try:
            data = json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            return {"running": False}
        if not isinstance(data, dict):
            return {"running": False}
        pid = data.get("pid")
        try:
            if "updated_mono" in data and data["updated_mono"] is not None:
                mono = float(data["updated_mono"])
                fresh = math.isfinite(mono) and 0 <= time.monotonic() - mono < STALE_S
            else:
                stamp = float(data.get("updated_ts", 0))
                fresh = math.isfinite(stamp) and 0 <= time.time() - stamp < STALE_S
        except (ValueError, TypeError, OverflowError):
            fresh = False
        data["running"] = bool(data.get("running", True) and isinstance(pid, int)
                               and self.alive(pid) and fresh)
        return data

    def _systemd(self) -> bool:
        if self.env.get(FAKE_ROOT_ENV):
            return False
        try:
            r = self.run(["systemctl", "--user", "cat", UNIT], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0

    def start(self, dry_run: bool = False) -> str:
        if self.status()["running"]:
            return "already running"
        if not dry_run and self._systemd():
            r = self.run(["systemctl", "--user", "start", UNIT], capture_output=True,
                         text=True, timeout=15)
            if r.returncode != 0:
                raise BackendError(f"systemctl --user start {UNIT}: {r.stderr.strip()}")
            return f"started {UNIT}"
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = dict(self.env)
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(x for x in (pkg_root, env.get("PYTHONPATH")) if x)
        cmd = [sys.executable, "-m", "asusius.daemon"] + (["--dry-run"] if dry_run else [])
        log_path = self.runtime / "daemon.log"
        _rotate_if_oversized(log_path)
        _prune_children()
        with _SPAWN_LOCK:
            if len(_SPAWNED) >= 8:
                raise BackendError("Too many detached daemon starts are still running")
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW |
                         os.O_CLOEXEC, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise BackendError("daemon log must be a regular file")
                os.fchmod(fd, 0o600)
                proc = self.popen(cmd, stdin=subprocess.DEVNULL, stdout=fd,
                                  stderr=subprocess.STDOUT, start_new_session=True, env=env)
            finally:
                os.close(fd)
            _SPAWNED.append(proc)
        stream = getattr(proc, "stdout", None)
        if stream is not None:
            threading.Thread(target=_capture_log, args=(stream, log_path),
                             name="asusius-daemon-log", daemon=True).start()
        return f"started pid {proc.pid}"

    def wait_running(self, timeout_s: float = 6.0) -> dict:
        """After start(): wait for the first status write; raise with the daemon's own
        last log lines when it died instead (missing module, broken config...)."""
        for _ in range(max(1, int(timeout_s / 0.15))):
            st = self.status()
            if st["running"]:
                return st
            self.sleep(0.15)
        tail = ""
        try:
            lines = (self.runtime / "daemon.log").read_text(errors="replace").strip().splitlines()
            tail = lines[-1] if lines else ""
        except OSError:
            pass
        raise BackendError("asusiusd did not start" + (f": {tail}" if tail else
                                                           " (no status written)"))

    def stop(self, timeout_s: float = 6.0) -> str:
        st = self.status()
        if self._systemd():
            r = self.run(["systemctl", "--user", "is-active", UNIT], capture_output=True,
                         text=True, timeout=5)
            if r.stdout.strip() in ("active", "activating", "reloading"):
                self.run(["systemctl", "--user", "stop", UNIT], capture_output=True,
                         timeout=15)
                return f"stopped {UNIT}"
        if not st["running"]:
            return "not running"
        pid = st["pid"]
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and self.alive(pid):
            self.sleep(0.1)
        return f"stopped pid {pid}" if not self.alive(pid) else f"pid {pid} still stopping"
