# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""`asusius daemon|automation|kbd effect` subcommands (registered from cli.py)."""
from __future__ import annotations

import json
from datetime import datetime

from ..core import AsusiusError, ValidationError, jsonable
from .config import config_path, load_config, update_config
from .rules import Rule, parse_action_spec, parse_trigger_spec


def add_parsers(sub) -> None:
    d = sub.add_parser("daemon", help="the asusiusd user service")
    ds = d.add_subparsers(dest="sub", required=True)
    st = ds.add_parser("start")
    st.add_argument("--dry-run", action="store_true", help="rules log only, change nothing")
    ds.add_parser("stop")
    ds.add_parser("status")

    a = sub.add_parser("automation", help="rules: trigger -> actions")
    as_ = a.add_subparsers(dest="sub", required=True)
    as_.add_parser("list")
    add = as_.add_parser("add", help="add or replace a rule")
    add.add_argument("--name", required=True)
    add.add_argument("--id")
    add.add_argument("--when", help="ac | battery | battery<20 | app=steam,gamescope | "
                                    "time=mon-fri@22:00-07:00 | profile=quiet")
    add.add_argument("--do", action="append", default=[], metavar="TYPE=VALUE",
                     help="profile=performance | kbd_colour=#ff0000 | kbd_effect=breathe:#f00 | "
                          "kbd_brightness=2 | charge_limit=80 | panel_overdrive=on | "
                          "ppd=power-saver | effect=temperature (repeatable)")
    add.add_argument("--priority", type=int, default=50)
    add.add_argument("--debounce", type=float, default=2.0, help="seconds")
    add.add_argument("--no-restore", action="store_true",
                     help="keep the values when the trigger ends")
    add.add_argument("--disabled", action="store_true")
    add.add_argument("--rule-json", help="a full rule as JSON instead of --when/--do")
    rm = as_.add_parser("remove")
    rm.add_argument("id")
    for name in ("enable", "disable"):
        x = as_.add_parser(name)
        x.add_argument("id")
    t = as_.add_parser("test", help="evaluate rules now and dry-run their actions")
    t.add_argument("id", nargs="?", help="only this rule (default: every rule)")
    t.add_argument("--dry-run", action="store_true", default=True,
                   help="always on: nothing is changed")
    t.add_argument("--state", default="", help="override inputs, e.g. ac=0,battery=15,"
                                               "apps=steam,profile=quiet,time=23:30")

    k = sub.add_parser("kbd", help="keyboard software effects")
    ks = k.add_subparsers(dest="sub", required=True)
    e = ks.add_parser("effect", help="choose the software effect (run by asusiusd)")
    e.add_argument("name", help="temperature | audio | ambient | profile | accent | off | list")
    e.add_argument("--param", action="append", default=[], metavar="K=V")
    e.add_argument("--notifications", choices=("on", "off"),
                   help="flash on desktop notifications above the effect")
    e.add_argument("--start", action="store_true", help="also start the daemon")


def _emit(args, data, text: str) -> None:
    print(json.dumps(jsonable(data), indent=2, default=str) if args.json else text)


def _rules(cfg) -> list[Rule]:
    return [Rule.from_dict(r) for r in cfg["automation"]["rules"]]


def _parse_state(text: str, ctl):
    from .engine import State
    from .sources import StateCollector
    st = StateCollector(ctl).collect()
    over = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        k, _, v = item.partition("=")
        over.setdefault(k.strip(), []).append(v.strip())
    kw = {"now": st.now, "on_ac": st.on_ac, "battery": st.battery, "apps": st.apps,
          "profile": st.profile}
    try:
        if "ac" in over:
            kw["on_ac"] = over["ac"][0].lower() in ("1", "true", "yes", "on")
        if "battery" in over:
            kw["battery"] = int(over["battery"][0])
        if "apps" in over:
            kw["apps"] = frozenset(a.lower() for a in over["apps"])
        if "profile" in over:
            kw["profile"] = over["profile"][0]
        if "time" in over:
            hh, mm = over["time"][0].split(":")
            kw["now"] = st.now.replace(hour=int(hh), minute=int(mm))
    except ValueError:
        raise ValidationError(f"bad --state {text!r}") from None
    return State(**kw)


def run(args, ctl) -> int:
    if args.cmd == "daemon":
        from .service import DaemonClient
        c = DaemonClient()
        if args.sub == "start":
            msg = c.start(dry_run=args.dry_run)
            _emit(args, {"result": msg}, msg)
        elif args.sub == "stop":
            msg = c.stop()
            _emit(args, {"result": msg}, msg)
        else:
            st = c.status()
            if not st["running"]:
                _emit(args, st, "asusiusd: stopped")
            else:
                fx = st.get("effects") or {}
                lines = [f"asusiusd: running (pid {st['pid']}"
                         f"{', dry run' if st.get('dry_run') else ''})",
                         f"active rules: {', '.join(st.get('active_rules') or []) or 'none'}",
                         f"effect: {fx.get('effect') or 'off'} {fx.get('colour') or ''}"
                         f"{' error: ' + fx['error'] if fx.get('error') else ''}"]
                if st.get("config_error"):
                    lines.append(f"config error (old config kept): {st['config_error']}")
                for e in (st.get("log") or [])[-5:]:
                    lines.append(f"  {e['time']} {e['kind']:<10} {e['rule']}: {e['detail']}")
                _emit(args, st, "\n".join(lines))
        return 0

    if args.cmd == "kbd":
        return _kbd(args)

    sub = args.sub
    if sub == "list":
        cfg = load_config()
        rules = _rules(cfg)
        text = "\n".join(f"{'on ' if r.enabled else 'off'} {r.id:<20} p{r.priority:<3} "
                         f"{r.describe()}" for r in rules) or f"no rules in {config_path()}"
        _emit(args, cfg["automation"], text)
    elif sub == "add":
        if args.rule_json:
            try:
                raw = json.loads(args.rule_json)
            except ValueError as exc:
                raise ValidationError(f"--rule-json: {exc}") from None
        else:
            if not args.when or not args.do:
                raise ValidationError("give --when and at least one --do (or --rule-json)")
            raw = {"name": args.name, "trigger": parse_trigger_spec(args.when),
                   "actions": [parse_action_spec(x) for x in args.do],
                   "priority": args.priority, "debounce_s": args.debounce,
                   "restore": not args.no_restore, "enabled": not args.disabled}
        if args.id:
            raw["id"] = args.id
        raw.setdefault("name", args.name)
        rule = Rule.from_dict(raw)

        def edit(cfg):
            rules = [r for r in cfg["automation"]["rules"] if r["id"] != rule.id]
            rules.append(rule.to_dict())
            cfg["automation"]["rules"] = rules
        update_config(edit)
        _emit(args, rule.to_dict(), f"added {rule.id}: {rule.describe()}")
    elif sub in ("remove", "enable", "disable"):
        found = []

        def edit(cfg):
            rules = cfg["automation"]["rules"]
            for r in rules:
                if r["id"] == args.id:
                    found.append(r)
                    if sub != "remove":
                        r["enabled"] = sub == "enable"
            if sub == "remove":
                cfg["automation"]["rules"] = [r for r in rules if r["id"] != args.id]
        update_config(edit)
        if not found:
            raise ValidationError(f"no rule with id {args.id!r}")
        _emit(args, {"id": args.id, "result": sub}, f"{sub}d {args.id}")
    elif sub == "test":
        from .actions import ControllerSink
        from .engine import evaluate, test_rule
        rules = _rules(load_config())
        if args.id:
            rules = [r for r in rules if r.id == args.id]
            if not rules:
                raise ValidationError(f"no rule with id {args.id!r}")
        st = _parse_state(args.state, ctl)
        ev = evaluate(rules, st)
        sink = ControllerSink(ctl)
        results = {r.id: test_rule(r, sink) for r in rules}
        lines = [f"inputs: ac={st.on_ac} battery={st.battery} profile={st.profile} "
                 f"time={st.now:%a %H:%M}"]
        for r in rules:
            mark = "MATCHES" if r.id in ev["matched"] else "idle   "
            lines.append(f"{mark} {r.id}: {r.describe()}")
            for a, res in zip(r.actions, results[r.id]):
                w = ev["winners"].get(a.key)
                wins = r.id not in ev["matched"] or (w is not None and w["rule"] == r.id)
                lines.append(f"    {'ok ' if res['ok'] else 'ERR'} {res['detail']}"
                             f"{'' if wins else '  (overridden by a higher priority rule)'}")
        _emit(args, {"state": {"on_ac": st.on_ac, "battery": st.battery, "profile": st.profile,
                               "time": st.now.isoformat()},
                     **ev, "actions": results}, "\n".join(lines))
    return 0


def _kbd(args) -> int:
    from ..effects.registry import EFFECTS, coerce_params, describe_all, parse_kv
    name = args.name.lower()
    if name == "list":
        rows = describe_all()
        text = "\n".join(f"{d['name']:<14} {d['description']}  params: "
                         f"{', '.join(d['params']) or '-'}" for d in rows)
        _emit(args, rows, text)
        return 0
    if name in ("off", "none"):
        target = None
        params = {}
    else:
        if name not in EFFECTS:
            raise ValidationError(f"unknown effect {name!r} (one of {', '.join(EFFECTS)}, off)")
        target = name
        params = coerce_params(name, parse_kv(args.param))

    def edit(cfg):
        fx = cfg["effects"]
        fx["active"] = target
        if target and params:
            fx["params"][target] = {**fx["params"].get(target, {}), **params}
        if args.notifications:
            fx["notifications"]["enabled"] = args.notifications == "on"
    cfg = update_config(edit)
    msg = f"effect {target or 'off'}"
    if target:
        msg += " " + json.dumps(cfg["effects"]["params"].get(target, {}))
    from .service import DaemonClient
    c = DaemonClient()
    if args.start:
        msg += f"; daemon: {c.start()}"
    elif not c.status()["running"] and target:
        msg += "; asusiusd is not running (asusius daemon start)"
    _emit(args, {"effects": cfg["effects"], "at": datetime.now().isoformat()}, msg)
    return 0


__all__ = ["add_parsers", "run", "AsusiusError"]
