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
        exe = shutil.which(self.binary)
        if exe is None:
            return {c: CapStatus(False, "asusctl not installed") for c in Capability}
        try:
            run_cmd([exe, "info"], timeout=min(self.timeout, 1.5))
        except BackendUnavailable:
            return {c: CapStatus(False, "asusctl not installed") for c in Capability}
        except BackendError as exc:
            msg = str(exc)
            if any(k in msg for k in ("ServiceUnknown", "NameHasNoOwner", "D-Bus", "dbus")):
                return {c: CapStatus(False, f"asusd unreachable via asusctl: {msg}")
                        for c in Capability}
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
        out = self._run(["profile", "get"])
        m = re.search(r"Active profile(?:\s+is|:)\s*(\w[\w-]*)", out, re.IGNORECASE)
        if not m:
            raise BackendError("cannot parse asusctl profile output")
        return Profile.parse(m.group(1))

    def set_profile(self, profile: Profile) -> None:
        self._run(["profile", "set", profile.cli_name])

    def get_battery_threshold(self) -> int:
        out = self._run(["battery", "info"])
        m = re.search(r"limit(?:\s+is|:)\s*(\d+)%", out, re.IGNORECASE)
        if not m:
            raise BackendError("cannot parse asusctl battery output")
        return int(m.group(1))

    def set_battery_threshold(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 20 <= value <= 100:
            raise ValidationError("battery threshold must be an integer in 20-100")
        self._run(["battery", "limit", str(value)])

    def get_kbd_brightness(self) -> tuple[int, int]:
        out = self._run(["leds", "get"])
        m = re.search(r"brightness(?:\s+is|:)\s*(\w+)", out, re.IGNORECASE)
        if not m:
            raise BackendError("cannot parse asusctl leds output")
        token = m.group(1).lower()
        if token.isdigit():
            val = int(token)
            return val, max(len(LED_LEVELS) - 1, val)
        if token not in LED_LEVELS:
            raise BackendError("cannot parse asusctl leds output")
        return LED_LEVELS.index(token), len(LED_LEVELS) - 1

    def set_kbd_brightness(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("keyboard brightness must be a non-negative integer")
        if value >= len(LED_LEVELS):
            raise Unsupported(f"asusctl leds supports 0-{len(LED_LEVELS) - 1} only")
        self._run(["leds", "set", LED_LEVELS[value]])


__all__ = ["CliBackend", "check_args", "Unsupported"]
