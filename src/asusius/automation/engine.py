# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Rule engine: trigger evaluation with debounce and hysteresis, per-setting conflict
resolution by priority, and restore of the previous value when the last rule lets go.

Everything time-dependent takes an injected clock, so tests drive it step by step.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core import Profile, AsusiusError
from .rules import DAYS, Rule, Trigger, parse_hhmm


@dataclass(frozen=True)
class State:
    """One snapshot of the world the triggers look at. None means 'unknown'."""

    now: datetime
    on_ac: bool | None = None
    battery: int | None = None
    apps: frozenset[str] = frozenset()
    profile: str | None = None


def _norm_app(name: str) -> str:
    n = name.strip().lower()
    return n[:-8] if n.endswith(".desktop") else n


def app_matches(names: list[str], apps: frozenset[str]) -> bool:
    for n in names:
        key = _norm_app(n)
        if key in apps:
            return True
        # a desktop id like com.valvesoftware.Steam also matches its last component
        if "." in key and key.rsplit(".", 1)[1] in apps:
            return True
        # /proc comm is truncated to 15 characters
        if len(key) > 15 and key[:15] in apps:
            return True
    return False


def in_schedule(p: dict, now: datetime) -> bool:
    start, end = parse_hhmm(p["start"]), parse_hhmm(p["end"])
    minute = now.hour * 60 + now.minute
    today = DAYS[now.weekday()]
    yesterday = DAYS[(now - timedelta(days=1)).weekday()]
    if start < end:
        return today in p["days"] and start <= minute < end
    # overnight window: the part after midnight belongs to the day it started on
    return (today in p["days"] and minute >= start) or (yesterday in p["days"] and minute < end)


def trigger_active(trig: Trigger, st: State, prev: bool) -> bool | None:
    """Raw (undebounced) state; None when the input is unknown (keep previous)."""
    p = trig.params
    if trig.type == "power":
        if st.on_ac is None:
            return None
        return st.on_ac if p["source"] == "ac" else not st.on_ac
    if trig.type == "battery":
        if st.battery is None:
            return None
        h = p["hysteresis"]
        if "below" in p:
            return st.battery <= p["below"] or (prev and st.battery < p["below"] + h)
        return st.battery >= p["above"] or (prev and st.battery > p["above"] - h)
    if trig.type == "app":
        return app_matches(p["names"], st.apps)
    if trig.type == "schedule":
        return in_schedule(p, st.now)
    if trig.type == "profile":
        if st.profile is None:
            return None
        return st.profile == p["value"]
    return False


@dataclass
class _Runtime:
    pending: bool = False
    pending_since: float = 0.0
    active: bool = False
    seen: bool = False


@dataclass
class _Hold:
    rule_id: str
    value: object
    restore: bool


@dataclass
class LogEntry:
    time: str
    kind: str          # activate | deactivate | apply | restore | error | conflict
    rule: str
    detail: str
    key: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class StepResult:
    active: list[str] = field(default_factory=list)
    log: list[LogEntry] = field(default_factory=list)
    holders: dict[str, str] = field(default_factory=dict)


RESTORE_CONFIG = "__config__"  # baseline marker for the software effect: follow config


class Engine:
    def __init__(self, sink, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = datetime.now, dry_run: bool = False,
                 sync_ppd: bool = False, log_size: int = 200) -> None:
        self.sink = sink
        self.clock = clock
        self.wall = wall
        self.dry_run = dry_run
        self.sync_ppd = sync_ppd
        self.rules: list[Rule] = []
        self.rt: dict[str, _Runtime] = {}
        self.holds: dict[str, _Hold] = {}
        self.baseline: dict[str, object] = {}
        self.log: deque[LogEntry] = deque(maxlen=log_size)

    # -- configuration ----------------------------------------------------------------------
    def set_rules(self, rules: list[Rule]) -> None:
        self.rules = list(rules)
        ids = {r.id for r in rules}
        for rid in list(self.rt):
            if rid not in ids:
                del self.rt[rid]

    def active_ids(self) -> list[str]:
        return [r.id for r in self.rules if self.rt.get(r.id) and self.rt[r.id].active]

    # -- stepping ---------------------------------------------------------------------------
    def _entry(self, kind, rule, detail, key="") -> LogEntry:
        e = LogEntry(self.wall().isoformat(timespec="seconds"), kind, rule, detail, key,
                     self.dry_run)
        self.log.append(e)
        return e

    def step(self, st: State) -> StepResult:
        now = self.clock()
        out = StepResult()
        for rule in self.rules:
            rt = self.rt.setdefault(rule.id, _Runtime(pending_since=now))
            if not rule.enabled:
                if rt.active:
                    rt.active = rt.pending = False
                    out.log.append(self._entry("deactivate", rule.id, "rule disabled"))
                continue
            raw = trigger_active(rule.trigger, st, rt.pending)
            if raw is None:
                continue
            if raw != rt.pending or not rt.seen:
                if raw != rt.pending:
                    rt.pending_since = now
                rt.pending = raw
                rt.seen = True
            # a rule that is already true at start-up still waits out its debounce
            if rt.pending != rt.active and now - rt.pending_since >= rule.debounce_s:
                rt.active = rt.pending
                out.log.append(self._entry("activate" if rt.active else "deactivate", rule.id,
                                           rule.trigger.describe()))
        self._resolve(out)
        out.active = self.active_ids()
        out.holders = {k: h.rule_id for k, h in self.holds.items()}
        return out

    def _winners(self) -> dict[str, tuple[Rule, object, list[str]]]:
        """key -> (winning rule, value, losing rule ids). Higher priority wins, then
        the rule listed first."""
        order = sorted((r for r in self.rules if self.rt.get(r.id) and self.rt[r.id].active
                        and r.enabled), key=lambda r: (-r.priority, self.rules.index(r)))
        win: dict[str, tuple[Rule, object, list[str]]] = {}
        for r in order:
            for a in r.actions:
                if a.key in win:
                    win[a.key][2].append(r.id)
                else:
                    win[a.key] = (r, a.target_value(), [])
        return win

    def _resolve(self, out: StepResult) -> None:
        win = self._winners()
        for key in sorted(set(win) | set(self.holds)):
            hold = self.holds.get(key)
            if key in win:
                rule, value, losers = win[key]
                if hold and hold.rule_id == rule.id and hold.value == value:
                    hold.restore = rule.restore
                    continue
                if losers:
                    out.log.append(self._entry(
                        "conflict", rule.id, f"{key}: wins over {', '.join(losers)} "
                        f"(priority {rule.priority})", key))
                had_baseline = key in self.baseline
                if not had_baseline:
                    self.baseline[key] = self._read(key)
                if self._apply(out, "apply", rule.id, key, value):
                    self.holds[key] = _Hold(rule.id, value, rule.restore)
                elif not had_baseline and key not in self.holds:
                    self.baseline.pop(key, None)
            elif hold:
                base = self.baseline.pop(key, None)
                del self.holds[key]
                if hold.restore and base is not None:
                    self._apply(out, "restore", hold.rule_id, key, base)

    def _read(self, key: str):
        try:
            return self.sink.read(key)
        except AsusiusError:
            return None

    def _apply(self, out: StepResult, kind: str, rule_id: str, key: str, value) -> bool:
        try:
            detail = self.sink.apply(key, value, dry_run=self.dry_run)
            out.log.append(self._entry(kind, rule_id, detail, key))
        except AsusiusError as exc:
            out.log.append(self._entry("error", rule_id, f"{key}: {exc}", key))
            return False
        if key == "profile" and self.sync_ppd:
            try:
                ppd = Profile.parse(value).ppd_name
                out.log.append(self._entry(kind, rule_id,
                                           self.sink.apply("ppd", ppd, dry_run=self.dry_run),
                                           "ppd"))
            except AsusiusError as exc:
                out.log.append(self._entry("error", rule_id, f"ppd: {exc}", "ppd"))
        return True

    def shutdown(self) -> list[LogEntry]:
        """Release every held setting (restoring where asked); used on daemon stop."""
        out = StepResult()
        for rt in self.rt.values():
            rt.active = rt.pending = False
        self._resolve(out)
        return out.log


# -- one-shot evaluation for `automation test` and the UI test button ---------------------------
def evaluate(rules: list[Rule], st: State) -> dict:
    """Which rules match right now (no debounce) and who would win each setting."""
    eng = Engine(sink=None)
    eng.set_rules(rules)
    matched = []
    for r in rules:
        if r.enabled and trigger_active(r.trigger, st, False):
            eng.rt[r.id] = _Runtime(True, 0.0, True, True)
            matched.append(r.id)
    win = eng._winners()
    return {"matched": matched,
            "winners": {k: {"rule": v[0].id, "value": v[1], "overrides": v[2]}
                        for k, v in win.items()}}


def test_rule(rule: Rule, sink) -> list[dict]:
    """Dry-run every action of one rule through the sink (the Controller)."""
    out = []
    for a in rule.actions:
        try:
            out.append({"action": a.describe(), "ok": True,
                        "detail": sink.apply(a.key, a.target_value(), dry_run=True)})
        except AsusiusError as exc:
            out.append({"action": a.describe(), "ok": False, "detail": str(exc)})
    return out
