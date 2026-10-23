"""~/.config/tuf-control/config.json: defaults, validation, atomic writes, hot reload.

The file is shared by the daemon, the GUI and the CLI. Writers take an flock on a sibling
lock file and replace the file atomically, so a reader never sees half a document. The
daemon keeps the last valid config when a hand-edited file fails validation and reports
the error in its status.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from ..core import ValidationError

CONFIG_VERSION = 1
CONFIG_DIR_ENV = "TUF_CONTROL_CONFIG_DIR"
RUNTIME_DIR_ENV = "TUF_CONTROL_RUNTIME_DIR"


class ConfigError(ValidationError):
    """The configuration document is malformed; the message names the offending path."""


def config_dir() -> Path:
    if os.environ.get(CONFIG_DIR_ENV):
        return Path(os.environ[CONFIG_DIR_ENV])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tuf-control"


def runtime_dir() -> Path:
    """Where the daemon's status, pid lock and log live (tmpfs, per user)."""
    if os.environ.get(RUNTIME_DIR_ENV):
        return Path(os.environ[RUNTIME_DIR_ENV])
    base = os.environ.get("XDG_RUNTIME_DIR") or f"{tempfile.gettempdir()}/tuf-control-{os.getuid()}"
    return Path(base) / "tuf-control"


def config_path(directory: Path | None = None) -> Path:
    return (directory or config_dir()) / "config.json"


DEFAULTS: dict = {
    "version": CONFIG_VERSION,
    "automation": {
        "enabled": True,
        "dry_run": False,      # log what rules would do, change nothing
        "poll_s": 2.0,         # state sampling period (power, processes, clock, profile)
        "sync_ppd": False,     # mirror profile actions into power-profiles-daemon
        "rules": [],
    },
    "effects": {
        "active": None,        # name of the software effect, or None for hardware mode
        "fps": 30,             # compute rate
        "max_write_hz": 10.0,  # keyboard writes per second (asusd persists every write)
        "smoothing_s": 0.25,   # colour interpolation time constant
        "params": {},          # effect name -> {param: value}
        "notifications": {"enabled": False, "colour": "#ffffff", "flashes": 2,
                          "duration_ms": 900, "apps": []},
    },
}


def _type(path: str, value, kinds, what: str):
    if isinstance(value, bool) and bool not in (kinds if isinstance(kinds, tuple) else (kinds,)):
        raise ConfigError(f"{path}: expected {what}")
    if not isinstance(value, kinds):
        raise ConfigError(f"{path}: expected {what}")
    return value


def _num(path: str, value, lo: float, hi: float) -> float:
    _type(path, value, (int, float), "a number")
    if not lo <= value <= hi:
        raise ConfigError(f"{path}: {value} outside {lo}-{hi}")
    return value


def validate_config(data) -> dict:
    """Return a normalised copy (defaults filled in) or raise ConfigError."""
    from ..effects.registry import validate_effects_section  # lazy: avoids an import cycle
    from .rules import Rule

    _type("config", data, dict, "an object")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"config: unknown keys {sorted(unknown)}")
    out = copy.deepcopy(DEFAULTS)
    version = data.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(f"version: unsupported {version!r} (expected {CONFIG_VERSION})")

    auto = _type("automation", data.get("automation", {}), dict, "an object")
    bad = set(auto) - set(DEFAULTS["automation"])
    if bad:
        raise ConfigError(f"automation: unknown keys {sorted(bad)}")
    for key in ("enabled", "dry_run", "sync_ppd"):
        if key in auto:
            out["automation"][key] = _type(f"automation.{key}", auto[key], bool, "true/false")
    if "poll_s" in auto:
        out["automation"]["poll_s"] = float(_num("automation.poll_s", auto["poll_s"], 0.5, 60))
    rules = _type("automation.rules", auto.get("rules", []), list, "a list")
    seen: set[str] = set()
    norm = []
    for i, r in enumerate(rules):
        try:
            rule = Rule.from_dict(r, where=f"automation.rules[{i}]")
        except ConfigError:
            raise
        except ValidationError as exc:
            raise ConfigError(str(exc)) from None
        if rule.id in seen:
            raise ConfigError(f"automation.rules[{i}]: duplicate id {rule.id!r}")
        seen.add(rule.id)
        norm.append(rule.to_dict())
    out["automation"]["rules"] = norm

    out["effects"] = validate_effects_section(data.get("effects", {}), DEFAULTS["effects"])
    return out


def load_config(path: Path | None = None) -> dict:
    """Read and validate. A missing file yields the defaults; a broken one raises."""
    path = path or config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror or exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from None
    return validate_config(data)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def write_json_atomic(path: Path, data) -> None:
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_config(data: dict, path: Path | None = None) -> dict:
    path = path or config_path()
    norm = validate_config(data)
    with _locked(path):
        write_json_atomic(path, norm)
    return norm


def update_config(fn: Callable[[dict], dict | None], path: Path | None = None) -> dict:
    """Locked read-modify-write. `fn` edits the dict in place or returns a new one."""
    path = path or config_path()
    with _locked(path):
        cfg = load_config(path)
        new = fn(cfg)
        norm = validate_config(cfg if new is None else new)
        write_json_atomic(path, norm)
    return norm


class ConfigWatcher:
    """Cheap hot-reload check: stat() the file and compare (mtime_ns, size, inode)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_path()
        self._sig: tuple | None = None

    def _stat(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def changed(self) -> bool:
        sig = self._stat()
        if sig != self._sig:
            self._sig = sig
            return True
        return False
