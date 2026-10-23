from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from ..core import BackendError, BackendUnavailable, PrivilegeRequired


def run_cmd(argv: list[str], timeout: float = 3.0) -> str:
    """Run a command without a shell, with a hard timeout. Returns stdout."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise BackendError("invalid command")
    env = {**os.environ, "LC_ALL": "C"}
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise BackendUnavailable(f"{argv[0]} not found") from None
    except subprocess.TimeoutExpired:
        raise BackendError(f"{argv[0]} timed out after {timeout}s") from None
    if proc.returncode != 0:
        raise BackendError(f"{argv[0]} failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
    return proc.stdout


def read_text(path: Path) -> str | None:
    """Read and strip a sysfs file; None if absent or unreadable."""
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


class Writer(Protocol):
    def write(self, path: Path, value: str) -> None: ...


class DirectWriter:
    """Write in-process. Root-only files raise PrivilegeRequired (a polkit helper
    Writer can be substituted later)."""

    def write(self, path: Path, value: str) -> None:
        try:
            with open(path, "w") as fh:
                fh.write(value)
        except PermissionError:
            raise PrivilegeRequired(f"no permission to write {path}") from None
        except OSError as exc:
            raise BackendError(f"write to {path} failed: {exc}") from None


HELPER_PATH = "/usr/libexec/tuf-control-helper"


class PkexecWriter:
    """PRIVILEGED FALLBACK. Runs the allow-listed polkit helper through pkexec.

    The helper (installed root-owned by the user via install.sh --system) only accepts
    exact sysfs paths and validated values; this class never builds a shell command.
    """

    def __init__(self, helper: str = HELPER_PATH, timeout: float = 120.0) -> None:
        self.helper, self.timeout = helper, timeout

    def available(self) -> bool:
        return os.access(self.helper, os.X_OK) and shutil.which("pkexec") is not None

    def write(self, path: Path, value: str) -> None:
        pkexec = shutil.which("pkexec")
        if not self.available() or pkexec is None:
            raise PrivilegeRequired(
                f"no permission to write {path}; install the polkit helper "
                "(see docs/INSTALL.md, install.sh --system)")
        try:
            proc = subprocess.run([pkexec, self.helper, "write", str(path), str(value)],
                                  capture_output=True, text=True, timeout=self.timeout,
                                  check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise BackendError("authentication timed out") from None
        if proc.returncode in (126, 127):  # dismissed / not authorised
            raise PrivilegeRequired("authentication was cancelled or refused")
        if proc.returncode != 0:
            raise BackendError(f"privileged write refused: {proc.stderr.strip()[:200]}")


class FallbackWriter:
    """Direct write first; the pkexec helper only if the direct write got EACCES."""

    def __init__(self, primary: Writer | None = None, fallback: Writer | None = None) -> None:
        self.primary = primary or DirectWriter()
        self.fallback = fallback or PkexecWriter()

    def write(self, path: Path, value: str) -> None:
        try:
            self.primary.write(path, value)
        except PrivilegeRequired:
            self.fallback.write(path, value)


class RecordingWriter:
    """Records writes instead of performing them (dry runs, tests)."""

    def __init__(self) -> None:
        self.writes: list[tuple[Path, str]] = []

    def write(self, path: Path, value: str) -> None:
        self.writes.append((path, value))
