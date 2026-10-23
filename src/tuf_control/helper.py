#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Minimal privileged sysfs writer for tuf-control, run through pkexec.

    tuf-control-helper write <absolute sysfs path> <value>

Stdlib only and self-contained (installed as a single root-owned file, so it must not
import anything from a user-writable location). It writes ONLY to an allow-list of exact
sysfs path shapes, after resolving symlinks (the real path must stay under /sys), and
only values that pass per-file validation, including the firmware's own min/max read
from sibling files. No shell, no subprocess, no environment is consulted.
"""
from __future__ import annotations

import os
import re
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
        _int(value, 1, 3, "pwm_enable")
    elif leaf.endswith("_temp"):
        _int(value, 0, 125, "temperature")
    else:
        _int(value, 0, 255, "pwm")


# (relative-to-root regex of the *resolved* path, validator)
RULES: list[tuple[re.Pattern[str], Callable[[Path, str], None]]] = [
    (re.compile(r"sys/firmware/acpi/platform_profile"), _v_profile),
    (re.compile(rf"sys/class/firmware-attributes/asus-armoury/attributes/{_NAME}/current_value"),
     _v_armoury),
    (re.compile(r"sys/class/power_supply/BAT[0-9]/charge_control_end_threshold"), _v_battery),
    (re.compile(r"sys/class/leds/asus::kbd_backlight/brightness"), _v_kbd_brightness),
    # kbd_rgb_*: write-only; formats per asus-wmi docs, not verifiable by reading back
    (re.compile(r"sys/class/leds/asus::kbd_backlight/kbd_rgb_mode"), _v_ints(6, 255)),
    (re.compile(r"sys/class/leds/asus::kbd_backlight/kbd_rgb_state"), _v_ints(5, 1)),
    (re.compile(r"sys/devices/virtual/hwmon/hwmon[0-9]+/pwm[123]_(?:enable|"
                r"auto_point[1-8]_(?:temp|pwm))"), _v_fan),
    (re.compile(r"sys/class/hwmon/hwmon[0-9]+/pwm[123]_(?:enable|"
                r"auto_point[1-8]_(?:temp|pwm))"), _v_fan),
]


def validate(path: str, value: str, root: str | Path = "/") -> Path:
    """Return the resolved path if (path, value) is allowed, else raise Rejected."""
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
            if not asked.is_file():
                raise Rejected("no such attribute")
            check(asked, value)
            return asked
    raise Rejected("path is not on the allow-list")


def write(path: str, value: str, root: str | Path = "/") -> None:
    real = validate(path, value, root)
    fd = os.open(real, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "write":
        print("usage: tuf-control-helper write <sysfs path> <value>", file=sys.stderr)
        return 2
    try:
        write(args[1], args[2])
    except Rejected as exc:
        print(f"tuf-control-helper: rejected: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"tuf-control-helper: write failed: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
