#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Minimal privileged sysfs writer for asusius, run through pkexec.

    asusius-helper write <absolute sysfs path> <value>

Stdlib only and self-contained (installed as a single root-owned file, so it must not
import anything from a user-writable location). It writes ONLY to an allow-list of exact
sysfs path shapes, after resolving symlinks (the real path must stay under /sys), and
only values that pass per-file validation, including the firmware's own min/max read
from sibling files. No shell, no subprocess, no environment is consulted.
"""
from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path

PROFILES = {"low-power", "quiet", "cool", "balanced", "balanced-performance", "performance"}
_NAME = r"[a-z0-9_]+"


class Rejected(Exception):
    pass


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _int(value: str, lo: int, hi: int, what: str) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise Rejected(f"{what}: not a non-negative integer")
    n = int(value)
    if not lo <= n <= hi:
        raise Rejected(f"{what}: {n} outside {lo}-{hi}")
    return n


def _v_profile(real: Path, value: str) -> None:
    if value not in PROFILES:
        raise Rejected("unknown platform profile")
    choices = _read(real.with_name("platform_profile_choices"))
    if choices is not None and value not in choices.split():
        raise Rejected("profile not offered by firmware")


def _v_armoury(real: Path, value: str) -> None:
    d = real.parent
    typ = _read(d / "type")
    if typ == "integer":
        lo, hi = _read(d / "min_value"), _read(d / "max_value")
        if lo is None or hi is None:
            raise Rejected("firmware bounds unreadable")
        try:
            lo_i, hi_i = int(lo), int(hi)
        except ValueError:
            raise Rejected("firmware bounds unreadable") from None
        if lo_i < 0 or hi_i < lo_i:
            raise Rejected("firmware reports no usable bounds")
        n = _int(value, lo_i, hi_i, "value")
        inc = _read(d / "scalar_increment")
        if inc and inc.isdigit() and int(inc) > 1 and (n - lo_i) % int(inc):
            raise Rejected(f"value must step by {inc} from {lo_i}")
    elif typ == "enumeration":
        poss = (_read(d / "possible_values") or "").split(";")
        if value not in [p for p in poss if p]:
            raise Rejected("value not in possible_values")
    else:
        raise Rejected("unsupported attribute type")


def _v_battery(real: Path, value: str) -> None:
    _int(value, 20, 100, "charge threshold")


def _v_kbd_brightness(real: Path, value: str) -> None:
    mx = _read(real.with_name("max_brightness"))
    _int(value, 0, int(mx) if mx and mx.isdigit() else 3, "brightness")


def _v_ints(count: int, hi: int) -> Callable[[Path, str], None]:
    def check(real: Path, value: str) -> None:
        parts = value.split(" ")
        if len(parts) != count:
            raise Rejected(f"expected {count} integers")
        for p in parts:
            _int(p, 0, hi, "field")
    return check


def _v_fan(real: Path, value: str) -> None:
    if _read(real.parent / "name") != "asus_custom_fan_curve":
        raise Rejected("not the asus_custom_fan_curve hwmon")
    leaf = real.name
    if leaf.endswith("_enable"):
        _int(value, 0, 4, "pwm_enable")
    elif leaf.endswith("_temp"):
        _int(value, 0, 125, "temperature")
    else:
        _int(value, 0, 255, "pwm")


_LED_NAME = r"[A-Za-z0-9_:-]*kbd_backlight[A-Za-z0-9_:-]*"
_MAX_BATCH = 64

# (relative-to-root regex of the *resolved* path, validator)
RULES: list[tuple[re.Pattern[str], Callable[[Path, str], None]]] = [
    (re.compile(r"sys/firmware/acpi/platform_profile"), _v_profile),
    (re.compile(rf"sys/class/firmware-attributes/asus-armoury/attributes/{_NAME}/current_value"),
     _v_armoury),
    (re.compile(r"sys/class/power_supply/BAT[A-Za-z0-9_]+/charge_control_end_threshold"),
     _v_battery),
    (re.compile(rf"sys/class/leds/{_LED_NAME}/brightness"), _v_kbd_brightness),
    # kbd_rgb_*: write-only; formats per asus-wmi docs, not verifiable by reading back
    (re.compile(rf"sys/class/leds/{_LED_NAME}/kbd_rgb_mode"), _v_ints(6, 255)),
    (re.compile(rf"sys/class/leds/{_LED_NAME}/kbd_rgb_state"), _v_ints(5, 1)),
    (re.compile(r"sys/devices/virtual/hwmon/hwmon[0-9]+/pwm[123]_(?:enable|"
                r"auto_point[1-8]_(?:temp|pwm))"), _v_fan),
    (re.compile(r"sys/class/hwmon/hwmon[0-9]+/pwm[123]_(?:enable|"
                r"auto_point[1-8]_(?:temp|pwm))"), _v_fan),
]


def _validate(path: str, value: str, root: str | Path) -> tuple[Path, os.stat_result]:
    """Validate the value and retain the identity of the attribute being checked."""
    if not isinstance(path, str) or not isinstance(value, str):
        raise Rejected("bad arguments")
    if "\x00" in path or "\x00" in value or not path.startswith("/"):
        raise Rejected("path must be absolute")
    if not value or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9_\- ;]+", value):
        raise Rejected("value contains disallowed characters")
    base = Path(os.path.realpath(root))
    sys_root = base / "sys"
    asked = Path(os.path.realpath(base / path.lstrip("/")))
    try:
        rel = asked.relative_to(base)
    except ValueError:
        raise Rejected("path escapes root") from None
    try:
        asked.relative_to(sys_root)
    except ValueError:
        raise Rejected("path is not under /sys") from None
    # symlinks like /sys/class/hwmon/hwmonN resolve into /sys/devices/..., so both the
    # lexical request and the resolved target are matched against the rules
    lexical = os.path.normpath(path).lstrip("/")
    candidates = {str(rel), lexical}
    for pattern, check in RULES:
        if any(pattern.fullmatch(c) for c in candidates):
            try:
                identity = asked.stat(follow_symlinks=False)
            except OSError:
                raise Rejected("no such attribute") from None
            if not stat.S_ISREG(identity.st_mode):
                raise Rejected("no such attribute")
            check(asked, value)
            return asked, identity
    raise Rejected("path is not on the allow-list")


def validate(path: str, value: str, root: str | Path = "/") -> Path:
    """Return the resolved path if (path, value) is allowed, else raise Rejected."""
    return _validate(path, value, root)[0]


def _open_attribute(real: Path) -> int:
    """Walk the resolved path without following a symlink at any component.

    Class symlinks were already resolved during validation. Directory descriptors
    keep the walk anchored even if a parent directory is replaced concurrently.
    """
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open(real.anchor, flags)
    try:
        for part in real.parts[1:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(real.name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                       os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def write(path: str, value: str, root: str | Path = "/") -> None:
    write_batch([(path, value)], root=root)


def write_batch(pairs: list[tuple[str, str]], root: str | Path = "/") -> None:
    if not pairs or len(pairs) > _MAX_BATCH:
        raise Rejected(f"batch must contain 1-{_MAX_BATCH} entries")
    base = Path(os.path.realpath(root))
    validated = [_validate(path, value, root) for path, value in pairs]
    fds: list[tuple[int, str]] = []
    try:
        for (real, expected), (_, value) in zip(validated, pairs):
            # defence in depth, right at the sink: validate() already resolved and allow-listed
            # the path; this second containment check on the resolved value makes the guarantee
            # local to the open() call
            if not real.is_relative_to(base / "sys"):
                raise Rejected("path is not under /sys")
            fd = _open_attribute(real)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                    os.close(fd)
                    raise Rejected("attribute changed after validation")
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            fds.append((fd, value))
        for fd, value in fds:
            os.write(fd, value.encode())
    finally:
        for fd, _ in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 3 and args[0] == "write":
        pairs = [(args[1], args[2])]
    elif len(args) >= 3 and len(args) % 2 == 1 and args[0] == "write-batch":
        pairs = list(zip(args[1::2], args[2::2]))
    else:
        print("usage: asusius-helper write <sysfs path> <value>", file=sys.stderr)
        return 2
    try:
        write_batch(pairs)
    except Rejected as exc:
        print(f"asusius-helper: rejected: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"asusius-helper: write failed: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
