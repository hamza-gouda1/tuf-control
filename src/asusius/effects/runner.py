# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Runs the effect stack: compute at `fps`, smooth, write the keyboard at most
`max_write_hz` through the Controller, pause while the backlight is off, and put the
hardware mode back when effects stop."""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..automation.config import config_dir, write_json_atomic
from ..core import Capability, KbdEffect, AsusiusError
from .base import EffectStack, Inputs
from .colour import RGB, Smoother, distance, rgb_to_hex
from .registry import EFFECTS, OVERLAY_EFFECTS

RESTORE_CONFIG = "__config__"


class KbdWriter:
    def __init__(self, ctl, *, max_hz: float = 10.0, min_delta: float = 4.0,
                 keepalive_s: float = 5.0, brightness_every_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic,
                 recovery_path: Path | None = None) -> None:
        self.ctl, self.max_hz, self.min_delta = ctl, max_hz, min_delta
        self.keepalive, self.bri_every, self.clock = keepalive_s, brightness_every_s, clock
        self.saved: KbdEffect | None = None
        self.saved_static: KbdEffect | None = None
        self.last_rgb: RGB | None = None
        self.last_write = -1e9
        self.last_bri_check = -1e9
        self.backlight_off = False
        self.writes = 0
        self.recovery_path = recovery_path
        self._recovery_checked = False
        self._frames: list[KbdEffect] = []

    @staticmethod
    def _effect(data: dict) -> KbdEffect:
        return KbdEffect(int(data.get("mode", 0)), int(data.get("zone", 0)),
                         tuple(data.get("colour1", (142, 42, 68))),
                         tuple(data.get("colour2", (0, 0, 0))),
                         int(data.get("speed", 1)), int(data.get("direction", 0)))

    @staticmethod
    def _stored_effect(data) -> KbdEffect:
        if not isinstance(data, dict) or set(data) != {f.name for f in dataclasses.fields(KbdEffect)}:
            raise ValueError("invalid saved keyboard effect")
        return KbdEffect(**{**data, "colour1": tuple(data["colour1"]),
                            "colour2": tuple(data["colour2"])}).validate()

    def _discard_recovery(self) -> None:
        if self.recovery_path is not None:
            try:
                self.recovery_path.unlink(missing_ok=True)
            except OSError as exc:
                raise AsusiusError(f"cannot clear keyboard recovery state: {exc}") from exc

    def recover(self) -> None:
        if self._recovery_checked or self.recovery_path is None:
            return
        try:
            if self.recovery_path.stat().st_size > 16384:
                raise ValueError("keyboard recovery state is too large")
            data = json.loads(self.recovery_path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("unknown keyboard recovery state version")
            saved = self._stored_effect(data["saved"])
            static = (self._stored_effect(data["saved_static"])
                      if data["saved_static"] is not None else None)
            frames = [self._stored_effect(frame) for frame in data["frames"]]
            if not 1 <= len(frames) <= 2:
                raise ValueError("invalid keyboard recovery frames")
        except FileNotFoundError:
            self._recovery_checked = True
            return
        except (OSError, ValueError, TypeError, KeyError):
            # Corrupt or incomplete data is never a hardware write instruction.
            self._discard_recovery()
            self._recovery_checked = True
            return
        current = self._effect((self.ctl.get_kbd_rgb()[0] or {}).get("mode_data") or {})
        self._recovery_checked = True
        if current in frames:
            self.saved, self.saved_static = saved, static
            self._frames = [current]
            self.writes = 1  # recovered frames also changed Static's stored colour
        else:
            # The user picked another hardware mode after the daemon died.
            self._discard_recovery()

    def _journal(self, frame: KbdEffect | None = None) -> None:
        if self.recovery_path is None:
            return
        frames = self._frames[-1:] + ([frame] if frame is not None else [])
        try:
            write_json_atomic(self.recovery_path, {
                "version": 1, "saved": dataclasses.asdict(self.saved),
                "saved_static": dataclasses.asdict(self.saved_static) if self.saved_static else None,
                "frames": [dataclasses.asdict(value) for value in frames],
            })
        except OSError as exc:
            raise AsusiusError(f"cannot save keyboard recovery state: {exc}") from exc

    def _write(self, frame: KbdEffect) -> None:
        # Write ahead: recovery accepts both sides of a crash during the D-Bus call.
        self._journal(frame)
        self.ctl.set_kbd_effect(frame)
        self._frames = [frame]

    def save(self) -> None:
        if self.saved is not None:
            return
        self.recover()
        if self.saved is not None:
            return
        info = self.ctl.get_kbd_rgb()[0] or {}
        self.saved = self._effect(info.get("mode_data") or {})
        # frames are written as Static, which asusd also stores as Static's own colour:
        # remember it so a Breathe/Pulse user gets their Static colour back too
        static = (info.get("builtins") or {}).get(0)
        self.saved_static = self._effect(static) if static and self.saved.mode != 0 else None
        self._frames = [self.saved]
        self._journal()

    def restore(self) -> None:
        self.recover()
        if self.saved is not None:
            if self.saved_static is not None and self.writes:
                self._write(self.saved_static)
            self._write(self.saved)
            self._discard_recovery()
            self.saved = self.saved_static = None
            self._frames = []
        self.last_rgb = None

    def push(self, rgb: RGB) -> bool:
        now = self.clock()
        if now - self.last_write < 1.0 / self.max_hz:
            return False
        if self.last_rgb is not None and distance(rgb, self.last_rgb) < self.min_delta \
                and now - self.last_write < self.keepalive:
            return False
        if now - self.last_bri_check >= self.bri_every:
            self.last_bri_check = now
            try:
                self.backlight_off = self.ctl.get_kbd_brightness()[0][0] == 0
            except AsusiusError:
                self.backlight_off = False
        if self.backlight_off:
            return False
        if self.saved is None:
            self.save()
        self._write(dataclasses.replace(self.saved, mode=0, colour1=tuple(rgb)))
        self.last_rgb, self.last_write = tuple(rgb), now
        self.writes += 1
        return True


class _Cached:
    """Call `fn` at most every `every` seconds; errors read as None."""

    def __init__(self, fn, every: float, clock) -> None:
        self.fn, self.every, self.clock = fn, every, clock
        self.at, self.value = -1e9, None

    def __call__(self):
        now = self.clock()
        if now - self.at >= self.every:
            self.at = now
            try:
                self.value = self.fn()
            except Exception:
                self.value = None
        return self.value


class EffectsRunner:
    def __init__(self, ctl, *, clock: Callable[[], float] = time.monotonic,
                 sources: dict | None = None, token_path: Path | None = None,
                 writer: KbdWriter | None = None, recovery_path: Path | None = None) -> None:
        self.ctl, self.clock = ctl, clock
        self.sources = dict(sources or {})
        self.token_path = token_path
        self.writer = writer or KbdWriter(ctl, clock=clock, recovery_path=(
            recovery_path or config_dir() / "keyboard-effect-state.json"))
        self.cfg: dict = {"active": None, "params": {}, "fps": 30, "max_write_hz": 10.0,
                          "smoothing_s": 0.25, "notifications": {"enabled": False}}
        self.override: object = RESTORE_CONFIG
        self.stack: EffectStack | None = None
        self.spec: tuple | None = None
        self.smoother = Smoother(0.25)
        self.colour: RGB | None = None
        self.layer: str | None = None
        self.error: str | None = None
        self._live: dict[str, object] = {}  # started sources by kind
        self._fail = 0
        self._paused_until = 0.0
        self._lock = threading.RLock()

    # -- configuration ------------------------------------------------------------------------
    def configure(self, cfg: dict) -> None:
        with self._lock:
            self.cfg = cfg
            self.writer.max_hz = float(cfg.get("max_write_hz", 10.0))
            self.smoother.tau = float(cfg.get("smoothing_s", 0.25))

    def set_override(self, value, dry_run: bool = False) -> str:
        """Automation hook: {'name', 'params'} or RESTORE_CONFIG."""
        if value != RESTORE_CONFIG and isinstance(value, dict) and value.get("name") \
                and value["name"] not in EFFECTS:
            raise AsusiusError(f"unknown effect {value['name']!r}")
        what = ("follow the configured effect" if value == RESTORE_CONFIG else
                f"start effect {value['name']}" if value.get("name") else "stop effects")
        if dry_run:
            return f"dry run: would {what}"
        with self._lock:
            self.override = value
        return what

    def hardware_mode_changed(self, effect: KbdEffect) -> None:
        """Someone else (an automation rule) set the hardware mode while an effect runs:
        that becomes the mode restored when the effect stops."""
        with self._lock:
            if self.writer.saved is not None:
                self.writer.saved = effect
                if effect.mode == 0:
                    self.writer.saved_static = None
                self.writer.last_rgb = None  # repaint over it on the next frame

    def get_override(self):
        return self.override

    def wanted(self) -> tuple[str | None, dict, bool, dict]:
        cfg = self.cfg
        if self.override != RESTORE_CONFIG and isinstance(self.override, dict):
            name, params = self.override.get("name"), dict(self.override.get("params") or {})
            base = dict(cfg.get("params", {}).get(name, {})) if name else {}
            params = {**base, **params}
        else:
            name = cfg.get("active")
            params = dict(cfg.get("params", {}).get(name, {})) if name else {}
        notif = dict(cfg.get("notifications") or {})
        on = bool(notif.pop("enabled", False))
        return name, params, on, notif

    # -- sources --------------------------------------------------------------------------------
    def _source(self, kind: str):
        if kind in self.sources:
            return self.sources[kind]
        if kind not in self._live:
            if kind == "audio":
                from .audio import AudioCapture
                src = AudioCapture()
            elif kind == "frames":
                from ..automation.config import config_dir
                from .ambient import ScreenColourSource
                src = ScreenColourSource(self.token_path or config_dir() / "screencast-token")
            elif kind == "notifications":
                from .notify import NotificationMonitor
                src = NotificationMonitor()
            else:
                return None
            src.start()
            self._live[kind] = src
        return self._live[kind]

    def _inputs(self, needs: set[str]) -> Inputs:
        clock = self.clock
        tel = self.sources.get("telemetry") or _Cached(
            lambda: self.ctl.read(Capability.TELEMETRY)[0], 1.0, clock)
        prof = self.sources.get("profile") or _Cached(
            lambda: self.ctl.get_profile()[0].value, 1.0, clock)
        if "theme" in self.sources:
            theme = self.sources["theme"]
        else:
            from .theme import theme_accent
            theme = _Cached(theme_accent, 10.0, clock)
        cpu = self.sources.get("cpu_load")
        if cpu is None and "cpu_load" in needs:
            from .system import CpuLoad
            cpu = _Cached(CpuLoad(), 0.5, clock)
        kw = {"now": self.sources["now"]} if "now" in self.sources else {}
        return Inputs(telemetry=tel, profile=prof, cpu_load=cpu or (lambda: None), **kw,
                      audio=self._source("audio") if "audio" in needs else None,
                      frames=self._source("frames") if "frames" in needs else None,
                      notifications=self._source("notifications")
                      if "notifications" in needs else None,
                      theme_accent=theme)

    def _stop_sources(self, keep: set[str]) -> None:
        for kind in [k for k in self._live if k not in keep]:
            try:
                self._live.pop(kind).stop()
            except Exception:
                pass

    # -- running --------------------------------------------------------------------------------
    def _rebuild(self) -> None:
        name, params, notif_on, notif = self.wanted()
        spec = (name, repr(sorted(params.items())), notif_on, repr(sorted(notif.items())))
        if spec == self.spec:
            return
        self.spec = spec
        if self.stack:
            self.stack.stop()
        self.stack = None
        layers = []
        needs: set[str] = set()
        if name:
            needs |= set(EFFECTS[name].needs)
        if notif_on and name:  # the flash overlays an effect; alone it would need a base
            needs.add("notifications")
        self._stop_sources(needs)
        if name:
            inputs = self._inputs(needs)
            layers.append((0, EFFECTS[name](params, inputs)))
            if notif_on:
                layers.append((100, OVERLAY_EFFECTS["notifications"](notif, inputs)))
        if layers:
            self.stack = EffectStack(layers)
            self.stack.start()
            self.error = None
            self._fail = 0
            try:
                self.writer.save()
            except AsusiusError as exc:
                self.error = f"keyboard colour unavailable: {exc}"
        else:
            self.colour = self.layer = None
            self._release()

    def _release(self) -> None:
        try:
            self.writer.restore()
        except AsusiusError as exc:
            self.error = f"could not restore keyboard mode: {exc}"

    def step(self, dt: float) -> RGB | None:
        with self._lock:
            self._rebuild()
            if self.stack is None:
                return None
            rgb, who = self.stack.tick(dt)
            if rgb is None:
                return self.colour
            out = self.smoother.step(rgb, dt)
            self.colour, self.layer = out, who
            if self.clock() < self._paused_until:
                return out
            try:
                self.writer.push(out)
                self._fail = 0
                if self.error and self.error.startswith("write failed"):
                    self.error = None
            except AsusiusError as exc:
                self._fail += 1
                self.error = f"write failed: {exc}"
                self._paused_until = self.clock() + min(30.0, 2.0 * self._fail)
            return out

    def run(self, stop: threading.Event) -> None:
        last = self.clock()
        while not stop.is_set():
            fps = self.frame_rate()
            if stop.wait(1.0 / fps):
                break
            now = self.clock()
            self.step(now - last)
            last = now

    def frame_rate(self) -> int:
        """Configured fps, capped by what the layers need (slow effects need few frames)."""
        if self.stack is None:
            return 2 if not self.wanted()[0] else 10
        need = max(getattr(e, "max_fps", 30) for _p, e in self.stack.layers)
        return max(1, min(int(self.cfg.get("fps", 30)), need))

    def shutdown(self) -> None:
        with self._lock:
            if self.stack:
                self.stack.stop()
            self.stack, self.spec = None, None
            self._stop_sources(set())
            self._release()

    def status(self) -> dict:
        with self._lock:
            name, _p, notif_on, _n = self.wanted()
            out = {"effect": name, "running": self.stack is not None,
                   "colour": rgb_to_hex(self.colour) if self.colour else None,
                   "layer": self.layer, "error": self.error, "writes": self.writer.writes,
                   "backlight_off": self.writer.backlight_off, "notifications": notif_on,
                   "override": self.override != RESTORE_CONFIG}
            for kind, src in self._live.items():
                info = {"error": getattr(src, "error", None)}
                if hasattr(src, "status"):
                    info["status"] = src.status
                out[f"source_{kind}"] = info
                if info["error"] and not out["error"] and kind != "notifications":
                    out["error"] = f"{kind} input: {info['error']}"  # shown in the UI, never silent
                elif info.get("status") == "waiting-permission":
                    out["waiting"] = "waiting for screen-sharing permission"
            return out
