# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""`asusius services ...`: list systemd units; start/stop/... the safe ones."""
from __future__ import annotations

import argparse
import json

from .units import FILTERS, OPERATIONS, TYPES, filter_units, refusal, sort_units


def add_parsers(sub) -> None:
    sv = sub.add_parser("services", help="systemd services (list; start/stop/restart/enable/"
                                         "disable through systemd and polkit)")
    ss = sv.add_subparsers(dest="sub")
    ls = ss.add_parser("list", help="units with state, enabled, PID and memory (default)")
    ss.add_parser("show", help="one unit").add_argument("name")
    for p in (ls,):
        p.add_argument("--filter", default="", help="substring of name or description")
        p.add_argument("--state", default="all", choices=list(FILTERS))
        p.add_argument("--type", default="service", choices=list(TYPES))
        p.add_argument("--sort", default="name", choices=["name", "state", "memory", "pid"])
    for op in OPERATIONS:
        p = ss.add_parser(op, help=f"{op} a unit (system units ask polkit)")
        p.add_argument("name")
        p.add_argument("--dry-run", action="store_true", help="check the safety rules only")
    sv.add_argument("--user", action="store_true", default=False,
                    help="user units (systemctl --user) instead of system units")
    for p in ss.choices.values():  # SUPPRESS: a child must not reset the parent's flag
        p.add_argument("--user", action="store_true", default=argparse.SUPPRESS,
                       help="user units (systemctl --user) instead of system units")


def _mem(n) -> str:
    if not n:
        return "–"
    return f"{n / 1048576:.1f} MiB"


def run(args) -> int:
    from .backend import make_backend
    sub = args.sub or "list"
    be = make_backend("user" if args.user else "system")
    units = be.list_units(force_files=True)
    if sub == "list":
        rows = sort_units(filter_units(units, getattr(args, "filter", ""),
                                       getattr(args, "state", "all"),
                                       getattr(args, "type", "service")),
                          getattr(args, "sort", "name"),
                          desc=getattr(args, "sort", "name") in ("memory", "pid"))
        if args.json:
            print(json.dumps([u.__dict__ for u in rows], indent=2, default=str))
        else:
            for u in rows:
                print(f"{u.name:<48} {u.state_text():<10} {u.enabled_text():<10} "
                      f"{u.pid or '':>8} {_mem(u.memory):>10}  {u.description}")
        return 0
    unit = next((u for u in units if u.name == args.name), None)
    if unit is None:
        print(f"error: no loaded unit {args.name!r}")
        return 1
    if sub == "show":
        d = {**unit.__dict__, **be.details(unit),
             "allowed": {op: refusal(unit, op) is None for op in OPERATIONS}}
        print(json.dumps(d, indent=2, default=str) if args.json else
              "\n".join(f"{k}: {v}" for k, v in d.items() if k != "extra"))
        return 0
    why = refusal(unit, sub)
    if why:
        print(f"refused: {unit.name}: {why}")
        return 1
    if args.dry_run:
        print(f"would {sub} {unit.name} ({unit.scope})")
        return 0
    print(be.act(unit, sub))
    return 0
