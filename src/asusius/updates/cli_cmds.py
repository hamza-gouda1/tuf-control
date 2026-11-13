# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""`asusius updates ...`: what needs an update, from the cache or a fresh check."""
from __future__ import annotations

import json
import time

from .model import SOURCE_ORDER, Report, age_text, human_size


def add_parsers(sub) -> None:
    up = sub.add_parser("updates", help="software, firmware and driver updates")
    us = up.add_subparsers(dest="sub")
    ls = us.add_parser("list", help="the last check (default); --check runs one now")
    ls.add_argument("--check", action="store_true", help="check now instead of the cache")
    us.add_parser("check", help="check every source now and cache the result")
    rf = us.add_parser("refresh", help="renew package lists / firmware metadata through "
                                       "PackageKit and fwupd (they may ask for your password)")
    rf.add_argument("--source", action="append", choices=["apt", "firmware", "flatpak"])
    ap = us.add_parser("apply", help="install updates through PackageKit, snapd, flatpak or "
                                     "fwupd, then re-check and report what really "
                                     "installed (prints the plan unless --yes)")
    ap.add_argument("--source", action="append", choices=SOURCE_ORDER)
    ap.add_argument("--id", action="append", help="item id from `updates list --json`")
    ap.add_argument("--yes", action="store_true", help="really apply (asks polkit)")
    hs = us.add_parser("history", help="past update runs from the persistent history")
    hs.add_argument("--since", help="e.g. 7d, 24h, 30m, 2w, today, yesterday (default: all)")
    hs.add_argument("--source", choices=["apt", "snap", "flatpak", "firmware", "system"])
    hs.add_argument("--limit", type=int, default=50)
    hs.add_argument("--json", action="store_true")


def _text(rep: Report | None) -> str:
    if rep is None:
        return "No update check yet. Run `asusius updates check`."
    lines = [f"{rep.summary()} — checked {age_text(rep.checked_ts)}"]
    if rep.reboot_required:
        lines.append("Restart needed: " + "; ".join(rep.reboot_reasons))
    for s in sorted(rep.sources, key=lambda s: SOURCE_ORDER.index(s.source)
                    if s.source in SOURCE_ORDER else 99):
        head = f"\n[{s.source}] {s.title}"
        if not s.available:
            lines.append(f"{head}: not available ({s.reason})")
            continue
        if s.error:
            head += f" — error: {s.error}"
        ts = s.info.get("lists_refreshed_ts") or s.info.get("metadata_ts")
        if ts:
            head += f" (metadata from {age_text(ts)})"
        lines.append(head + ("" if s.items else ": nothing"))
        for i in s.items:
            flags = " ".join(x for x in ("SECURITY" if i.security else "",
                                          "restart" if i.needs_reboot else "",
                                          i.blocker["title"] if i.blocker else "",
                                          "held back" if i.extra.get("held")
                                          and not i.blocker else "") if x)
            ver = f"{i.current or '?'} -> {i.available or '?'}"
            size = human_size(i.size)
            lines.append(f"  {i.name:<34} {ver}" + (f"  {size}" if size else "") +
                         (f"  [{flags}]" if flags else ""))
    return "\n".join(lines)


_RESULT_FLAGS = {"installed": "installed", "restart": "installs on restart",
                 "pending": "still pending", "failed": "FAILED"}


def _history_text(runs: list[dict]) -> str:
    from .verify import RESULT_TITLES
    if not runs:
        return "No update runs recorded yet."
    out = [f"{len(runs)} run{'s' if len(runs) != 1 else ''}, newest first"]
    for r in runs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("end") or r.get("start")
                                                              or 0))
        dur = f" · {r['duration_s']:g} s" if r.get("duration_s") else ""
        trig = {"manual": "run from Asusius", "automatic": "automatic",
                "detected": "installed outside Asusius"}.get(r.get("trigger"),
                                                                 r.get("trigger") or "?")
        head = f"\n{when} · {trig}{dur}"
        if r.get("cancelled"):
            head += " · cancelled"
        if r.get("reboot"):
            head += " · restart needed"
        out.append(head)
        out.append("  " + (r.get("summary") or ""))
        for it in r.get("items") or []:
            ver = f"{it.get('old') or '?'} -> {it.get('new') or '?'}"
            line = f"  {it.get('source', '?'):<8} {str(it.get('name')):<32} {ver}"
            res = it.get("result")
            if res in RESULT_TITLES:
                line += f"  [{_RESULT_FLAGS.get(res, res)}]"
            if it.get("reason"):
                line += "\n      " + str(it["reason"])
            if it.get("command"):
                line += "\n      $ " + str(it["command"])
            out.append(line)
    return "\n".join(out)


def run(args) -> int:
    from . import actions
    from .check import load_report, run_check
    sub = args.sub or "list"
    if sub == "history":
        from . import history
        try:
            since = history.parse_since(getattr(args, "since", None))
        except ValueError as exc:
            print(str(exc))
            return 2
        runs = history.read(since_ts=since, source=getattr(args, "source", None),
                            limit=args.limit)
        print(json.dumps(runs, indent=2) if args.json else _history_text(runs))
        return 0
    if sub == "check" or (sub == "list" and getattr(args, "check", False)):
        rep = run_check()
    else:
        rep = load_report()
    if sub in ("list", "check"):
        print(json.dumps(rep.to_dict() if rep else None, indent=2) if args.json else _text(rep))
        return 0
    if sub == "refresh":
        out = actions.refresh_sources(args.source or list(actions.REFRESHABLE))
        rep = run_check()
        out["check"] = rep.summary()
        print(json.dumps(out, indent=2) if args.json else
              "\n".join(f"{k}: {v}" for k, v in out.items()))
        return 0
    if sub == "apply":
        from .progress import RunState
        from . import runner
        rep = rep or run_check()
        items = [i for i in rep.updates()
                 if (not args.source or i.source in args.source)
                 and (not args.id or i.id in args.id)]
        planned = actions.plan(items)
        if not planned:
            print("nothing to apply")
            return 0
        if not args.yes:
            print(actions.describe(items) + "\n\nRe-run with --yes to apply.")
            if any(m == "fwupd" for m in planned):
                print("\n" + actions.FIRMWARE_WARNING)
            return 0
        state = RunState()
        print(actions.describe(items), flush=True)
        res = runner.apply_run(items, state, trigger="manual")
        if not args.json:
            for k, v in res["outcomes"].items():
                print(f"{k}: {v}")
            print()
            print(res["summary"] + ":")
            print(_history_text([res["run"]]).split("\n", 1)[1])
        else:
            print(json.dumps(res, indent=2))
        return 1 if any(r["result"] == "failed" for r in res["results"]) else 0
    return 2
