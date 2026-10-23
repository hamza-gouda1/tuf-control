from __future__ import annotations

import os
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


class RecordingWriter:
    """Records writes instead of performing them (dry runs, tests)."""

    def __init__(self) -> None:
        self.writes: list[tuple[Path, str]] = []

    def write(self, path: Path, value: str) -> None:
        self.writes.append((path, value))
