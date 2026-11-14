# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
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


HELPER_PATH = "/usr/libexec/asusius-helper"
HELPER_PATHS = (HELPER_PATH, "/usr/lib/asusius/asusius-helper")


def _resolve_helper(explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit
    for candidate in HELPER_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return HELPER_PATH


class PkexecWriter:
    """PRIVILEGED FALLBACK. Runs the allow-listed polkit helper through pkexec.

    The helper (installed root-owned by the user via install.sh --system) only accepts
    exact sysfs paths and validated values; this class never builds a shell command.
    """

    def __init__(self, helper: str | None = None, timeout: float = 120.0) -> None:
        self._explicit_helper = helper
        self.timeout = timeout

    @property
    def helper(self) -> str:
        return _resolve_helper(self._explicit_helper)

    @helper.setter
    def helper(self, value: str) -> None:
        self._explicit_helper = value

    @staticmethod
    def no_new_privileges() -> bool:
        status = read_text(Path("/proc/self/status")) or ""
        return any(line.split() == ["NoNewPrivs:", "1"] for line in status.splitlines())

    def available(self) -> bool:
        return (not self.no_new_privileges() and os.access(self.helper, os.X_OK)
                and shutil.which("pkexec") is not None)

    def _run_helper(self, helper_args: list[str], path_hint: Path) -> subprocess.CompletedProcess:
        if self.no_new_privileges():
            raise PrivilegeRequired(
                "privileged sysfs writes are unavailable under NoNewPrivileges; "
                "the background service needs asusd or power-profiles-daemon for this change")
        pkexec = shutil.which("pkexec")
        helper = self.helper
        if not self.available() or pkexec is None:
            raise PrivilegeRequired(
                f"no permission to write {path_hint}; install the polkit helper "
                "(see docs/INSTALL.md, install.sh --system)")
        try:
            proc = subprocess.run([pkexec, helper, *helper_args],
                                  capture_output=True, text=True, timeout=self.timeout,
                                  check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise BackendError("authentication timed out") from None
        if proc.returncode in (126, 127):  # dismissed / not authorised
            raise PrivilegeRequired("authentication was cancelled or refused")
        return proc

    def write(self, path: Path, value: str) -> None:
        proc = self._run_helper(["write", str(path), str(value)], path)
        if proc.returncode != 0:
            raise BackendError(f"privileged write refused: {proc.stderr.strip()[:200]}")

    def write_many(self, writes: list[tuple[Path, str]]) -> None:
        if not writes:
            return
        if len(writes) == 1:
            self.write(*writes[0])
            return
        args = ["write-batch"]
        for p, v in writes:
            args.extend((str(p), str(v)))
        proc = self._run_helper(args, writes[0][0])
        if proc.returncode == 0:
            return
        # Older installed root helpers only support `write <path> <value>` (rc=2 usage).
        if proc.returncode == 2:
            for p, v in writes:
                self.write(p, v)
            return
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

    def write_many(self, writes: list[tuple[Path, str]]) -> None:
        if not writes:
            return
        try:
            for p, v in writes:
                self.primary.write(p, v)
        except PrivilegeRequired:
            batch = getattr(self.fallback, "write_many", None)
            if callable(batch):
                batch(writes)
            else:
                for p, v in writes:
                    self.fallback.write(p, v)


class RecordingWriter:
    """Records writes instead of performing them (dry runs, tests)."""

    def __init__(self) -> None:
        self.writes: list[tuple[Path, str]] = []

    def write(self, path: Path, value: str) -> None:
        self.writes.append((path, value))
