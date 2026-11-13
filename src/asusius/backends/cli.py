# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""asusctl wrapper. Strict allow-list of subcommands and argument patterns; no shell."""
from __future__ import annotations

import re
import shutil

from ..core import (
    BackendError,
    BackendUnavailable,
    Capability,
    CapStatus,
    Profile,
    Unsupported,
    ValidationError,
)
from .base import Backend
from .util import run_cmd

_NAME = r"[a-z0-9_]{1,40}"
_WORD = r"[A-Za-z]{1,16}"
_INT = r"[0-9]{1,5}"
# argv tail (after the binary) -> mutating? Each entry is a tuple of regexes, fully matched.
READ_COMMANDS = [
    ("profile", "get"), ("profile", "list"), ("battery", "info"), ("leds", "get"),
    ("armoury", "list"), ("info",),
]
WRITE_COMMANDS = [
    ("profile", "set", _WORD), ("battery", "limit", _INT), ("leds", "set", _WORD),
    ("armoury", "set", _NAME, _INT),
]
LED_LEVELS = ["off", "low", "med", "high"]


def check_args(args: list[str]) -> list[str]:
    """Return args if they match the allow-list exactly, else raise ValidationError."""
    if not all(isinstance(a, str) for a in args):
        raise ValidationError("arguments must be strings")
    for pat in READ_COMMANDS:
        if tuple(args) == pat:
            return args
    for pat in WRITE_COMMANDS:
        if len(args) == len(pat) and all(re.fullmatch(p, a) for p, a in zip(pat, args)):
            return args
    raise ValidationError(f"asusctl arguments not allowed: {args!r}")


class CliBackend(Backend):
    name = "cli"

    def __init__(self, binary: str = "asusctl", timeout: float = 4.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def _run(self, args: list[str]) -> str:
        exe = shutil.which(self.binary)
        if exe is None:
            raise BackendUnavailable("asusctl not installed")
        return run_cmd([exe, *check_args(args)], timeout=self.timeout)

    def probe(self):
        if shutil.which(self.binary) is None:
            return {c: CapStatus(False, "asusctl not installed") for c in Capability}
        ok = CapStatus(True)
        no = CapStatus(False, "not exposed by asusctl wrapper")
        return {
            Capability.PROFILE: ok, Capability.BATTERY_THRESHOLD: ok,
            Capability.KBD_BRIGHTNESS: ok,
            **{c: no for c in Capability
               if c not in (Capability.PROFILE, Capability.BATTERY_THRESHOLD,
                            Capability.KBD_BRIGHTNESS)},
        }

    def get_profile(self) -> Profile:
        m = re.search(r"Active profile:\s*(\w[\w-]*)", self._run(["profile", "get"]))
        if not m:
            raise BackendError("cannot parse asusctl profile output")
        return Profile.parse(m.group(1))

    def set_profile(self, profile: Profile) -> None:
        self._run(["profile", "set", profile.cli_name])

    def get_battery_threshold(self) -> int:
        m = re.search(r"limit:\s*(\d+)%", self._run(["battery", "info"]))
        if not m:
            raise BackendError("cannot parse asusctl battery output")
        return int(m.group(1))

    def set_battery_threshold(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 20 <= value <= 100:
            raise ValidationError("battery threshold must be an integer in 20-100")
        self._run(["battery", "limit", str(value)])

    def get_kbd_brightness(self) -> tuple[int, int]:
        m = re.search(r"brightness:\s*(\w+)", self._run(["leds", "get"]))
        if not m or m.group(1).lower() not in LED_LEVELS:
            raise BackendError("cannot parse asusctl leds output")
        return LED_LEVELS.index(m.group(1).lower()), len(LED_LEVELS) - 1

    def set_kbd_brightness(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
            raise ValidationError("keyboard brightness must be 0-3")
        self._run(["leds", "set", LED_LEVELS[value]])


__all__ = ["CliBackend", "check_args", "Unsupported"]
