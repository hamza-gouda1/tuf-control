"""Start, stop and query `tuf-controld` from the GUI and CLI.

With the systemd user unit installed (and no fake tree) start/stop go through
`systemctl --user`; otherwise the daemon is spawned detached with the current Python and
stopped with SIGTERM via its pid. Status comes from the daemon's status file."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..controller import FAKE_ROOT_ENV
from ..core import BackendError
from .config import runtime_dir

UNIT = "tuf-controld.service"
_SPAWNED: list = []  # detached children; kept so Popen is not reaped (and warned about) by GC
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
    return b"tuf_control.daemon" in cmd or b"tuf-controld" in cmd


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
        try:
            data = json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            return {"running": False}
        pid = data.get("pid")
        fresh = time.time() - float(data.get("updated_ts", 0)) < STALE_S
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
        self.runtime.mkdir(parents=True, exist_ok=True)
        env = dict(self.env)
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(x for x in (pkg_root, env.get("PYTHONPATH")) if x)
        cmd = [sys.executable, "-m", "tuf_control.daemon"] + (["--dry-run"] if dry_run else [])
        log = open(self.runtime / "daemon.log", "ab")
        try:
            proc = self.popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                              start_new_session=True, env=env)
        finally:
            log.close()
        _SPAWNED.append(proc)
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
        raise BackendError("tuf-controld did not start" + (f": {tail}" if tail else
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
