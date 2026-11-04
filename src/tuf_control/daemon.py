# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""tuf-controld: the per-user automation and keyboard-effects daemon.

    tuf-controld [--dry-run] [--once] [--config PATH]

Runs as a systemd --user service (data/systemd/tuf-controld.service) or detached from the
GUI/CLI. Single instance per user (flock in $XDG_RUNTIME_DIR/tuf-control/daemon.lock).
Hot-reloads ~/.config/tuf-control/config.json; SIGHUP forces a reload; SIGTERM/SIGINT stop
cleanly: rules let go of what they hold (restoring previous values where asked) and the
keyboard returns to its hardware mode. Status for the GUI/CLI is written to
$XDG_RUNTIME_DIR/tuf-control/status.json about once a second.

It also checks for software/firmware updates in the background (updates.scheduler: at most
every 6 hours, never on a metered connection, in a short-lived child process) and posts a
desktop notification only when something new needs an update.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .automation.actions import ControllerSink
from .automation.config import (
    DEFAULTS,
    ConfigError,
    ConfigWatcher,
    config_path,
    load_config,
    runtime_dir,
    write_json_atomic,
)
from .automation.engine import Engine, app_matches
from .automation.rules import Rule
from .automation.sources import StateCollector
from .controller import FAKE_ROOT_ENV, Controller
from .effects.runner import EffectsRunner
from .updates.scheduler import UpdateScheduler

UPDATES_TICK_S = 60.0  # how often the loop asks whether an update check is due

STATUS_NAME = "status.json"
LOCK_NAME = "daemon.lock"


class Daemon:
    def __init__(self, ctl: Controller | None = None, *, config_file: Path | None = None,
                 status_file: Path | None = None, collector: StateCollector | None = None,
                 effects: EffectsRunner | None = None, clock=time.monotonic,
                 wall=datetime.now, dry_run: bool | None = None,
                 updates: UpdateScheduler | None = None) -> None:
        self.ctl = ctl or Controller()
        self.clock, self.wall = clock, wall
        self.config_file = config_file or config_path()
        self.status_file = status_file or runtime_dir() / STATUS_NAME
        self.effects = effects or EffectsRunner(self.ctl, clock=clock,
                                                token_path=self.config_file.parent /
                                                "screencast-token",
                                                recovery_path=self.config_file.parent /
                                                "keyboard-effect-state.json")
        sink = ControllerSink(self.ctl, effect_get=self.effects.get_override,
                              effect_set=self.effects.set_override,
                              kbd_changed=self.effects.hardware_mode_changed,
                              kbd_saved=lambda: self.effects.writer.saved)
        self.engine = Engine(sink, clock=clock, wall=wall)
        self.collector = collector or StateCollector(self.ctl, wall=wall)
        self.watcher = ConfigWatcher(self.config_file)
        self.cfg: dict = DEFAULTS
        self.config_error: str | None = None
        self.forced_dry_run = dry_run
        self.started = wall().isoformat(timespec="seconds")
        self.last_state = None
        self.wake = threading.Event()
        self.updates = updates if updates is not None else UpdateScheduler()

    # -- config --------------------------------------------------------------
    def reload(self, force: bool = False) -> bool:
        if not self.watcher.changed() and not force:
            return False
        try:
            cfg = load_config(self.config_file)
        except ConfigError as exc:
            self.config_error = str(exc)  # keep running with the last good config
            return False
        self.cfg, self.config_error = cfg, None
        auto = cfg["automation"]
        rules = [Rule.from_dict(r) for r in auto["rules"]] if auto["enabled"] else []
        self.engine.set_rules(rules)
        self.engine.dry_run = auto["dry_run"] if self.forced_dry_run is None \
            else self.forced_dry_run
        self.engine.sync_ppd = auto["sync_ppd"]
        self.collector.needs_apps = any(r.trigger.type == "app" for r in rules)
        self.collector.needs_profile = any(r.trigger.type == "profile" for r in rules)
        effects_cfg = cfg["effects"]
        if self.forced_dry_run:
            effects_cfg = {**effects_cfg, "active": None,
                           "notifications": {"enabled": False}}
        self.effects.configure(effects_cfg)
        self.updates.configure(cfg.get("updates") or {})
        return True

    # -- one automation step --------------------------------------------------------------
    def step(self):
        st = self.collector.collect()
        self.last_state = st
        return self.engine.step(st)

    def status(self) -> dict:
        st = self.last_state
        apps = sorted({n for r in self.engine.rules if r.trigger.type == "app"
                       for n in r.trigger.params["names"] if st and app_matches([n], st.apps)})
        return {
            "pid": os.getpid(), "version": __version__, "started": self.started,
            "updated": self.wall().isoformat(timespec="seconds"), "updated_ts": time.time(),
            "fake": bool(os.environ.get(FAKE_ROOT_ENV)),
            "dry_run": self.engine.dry_run, "config_error": self.config_error,
            "automation_enabled": self.cfg["automation"]["enabled"],
            "active_rules": self.engine.active_ids(),
            "holders": {k: h.rule_id for k, h in self.engine.holds.items()},
            "state": None if st is None else {
                "on_ac": st.on_ac, "battery": st.battery, "profile": st.profile,
                "matched_apps": apps, "time": st.now.isoformat(timespec="minutes")},
            "log": [e.as_dict() for e in list(self.engine.log)[-40:]],
            "effects": self.effects.status(),
            "updates": self.updates.status(),
        }

    def write_status(self, running: bool = True) -> None:
        data = self.status()
        data["running"] = running
        with contextlib.suppress(OSError):
            write_json_atomic(self.status_file, data)

    # -- main loop --------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        self.reload(force=True)
        fx = threading.Thread(target=self.effects.run, args=(stop,), name="tuf-effects",
                              daemon=True)
        fx.start()
        next_auto = 0.0
        next_status = 0.0
        next_updates = 0.0
        try:
            while not stop.is_set():
                now = self.clock()
                self.reload()
                if now >= next_auto or self.wake.is_set():
                    self.wake.clear()
                    self.step()
                    next_auto = now + float(self.cfg["automation"]["poll_s"])
                if now >= next_updates:  # cheap unless a check is due; that runs in a child
                    next_updates = now + UPDATES_TICK_S
                    try:
                        self.updates.tick()
                        self.updates.status(refresh=True)
                    except Exception:
                        pass
                if now >= next_status:
                    self.write_status()
                    next_status = now + 1.0
                stop.wait(0.25)
        finally:
            stop.set()
            fx.join(5)  # no frame may land after the hardware mode is restored
            self.shutdown()

    def shutdown(self) -> None:
        from .updates.notify import close_clicks
        close_clicks()
        self.effects.shutdown()  # first: hands the keyboard back before rules restore it
        self.engine.shutdown()
        self.write_status(running=False)


def watch_upower(wake: threading.Event) -> None:  # pragma: no cover - needs the system bus
    """Wake the loop on UPower property changes (read-only signal subscription)."""
    def body():
        try:
            from gi.repository import Gio, GLib
            ctx = GLib.MainContext.new()
            ctx.push_thread_default()
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            conn.signal_subscribe("org.freedesktop.UPower", "org.freedesktop.DBus.Properties",
                                  "PropertiesChanged", None, None, Gio.DBusSignalFlags.NONE,
                                  lambda *a: wake.set())
            GLib.MainLoop(ctx).run()
        except Exception:
            pass
    threading.Thread(target=body, name="tuf-upower", daemon=True).start()


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tuf-controld", description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="log rule actions, change nothing")
    p.add_argument("--once", action="store_true",
                   help="one dry-run automation step, print status, exit")
    p.add_argument("--config", type=Path, help="config file (default ~/.config/tuf-control)")
    args = p.parse_args(argv)
    daemon = Daemon(config_file=args.config,
                    dry_run=True if (args.dry_run or args.once) else None)
    if args.once:
        daemon.reload(force=True)
        daemon.step()
        print(json.dumps(daemon.status(), indent=2, default=str))
        daemon.engine.shutdown()
        return 0
    lock = acquire_lock(runtime_dir() / LOCK_NAME)
    if lock is None:
        print("tuf-controld is already running", file=sys.stderr)
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, lambda *_: (daemon.reload(force=True), daemon.wake.set()))
    if not os.environ.get(FAKE_ROOT_ENV):
        watch_upower(daemon.wake)
    try:
        daemon.run(stop)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
