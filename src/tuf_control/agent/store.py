"""Paths and locked, atomic JSON files shared by the MCP server, the GUI and the CLI."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path

from ..automation.config import config_dir, write_json_atomic

STATE_DIR_ENV = "TUF_CONTROL_STATE_DIR"


def state_dir() -> Path:
    """~/.local/state/tuf-control ($XDG_STATE_HOME, or $TUF_CONTROL_STATE_DIR)."""
    if os.environ.get(STATE_DIR_ENV):
        return Path(os.environ[STATE_DIR_ENV])
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "tuf-control"


def access_path() -> Path:
    return config_dir() / "agent.json"


def approvals_path() -> Path:
    return state_dir() / "agent-approvals.json"


def audit_path() -> Path:
    return state_dir() / "agent-audit.log"


def _mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive flock on a sibling .lock file for read-modify-write cycles."""
    _mkdir(path.parent)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_json(path: Path, limit: int = 1 << 20):
    """Parsed JSON, None when the file is missing. Raises ValueError/OSError otherwise."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise ValueError(f"{path} is larger than {limit} bytes")
    return json.loads(data)


def write_json(path: Path, data) -> None:
    _mkdir(path.parent)
    write_json_atomic(path, data)  # mkstemp: the file is created 0600
