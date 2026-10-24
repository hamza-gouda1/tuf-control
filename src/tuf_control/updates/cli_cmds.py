"""`tuf-control updates ...`: what needs an update, from the cache or a fresh check."""
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
                                     "fwupd (prints the plan unless --yes)")
    ap.add_argument("--source", action="append", choices=SOURCE_ORDER)
    ap.add_argument("--id", action="append", help="item id from `updates list --json`")
    ap.add_argument("--yes", action="store_true", help="really apply (asks polkit)")


def _text(rep: Report | None) -> str:
    if rep is None:
        return "No update check yet. Run `tuf-control updates check`."
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
                                          "held back" if i.extra.get("held") else "") if x)
            ver = f"{i.current or '?'} -> {i.available or '?'}"
            size = human_size(i.size)
            lines.append(f"  {i.name:<34} {ver}" + (f"  {size}" if size else "") +
                         (f"  [{flags}]" if flags else ""))
    return "\n".join(lines)


def run(args) -> int:
    from . import actions
    from .check import load_report, run_check
    sub = args.sub or "list"
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
        t0 = time.monotonic()
        out = actions.apply(items, progress=lambda s: None)
        out["took_s"] = round(time.monotonic() - t0, 1)
        run_check()
        print(json.dumps(out, indent=2) if args.json else
              "\n".join(f"{k}: {v}" for k, v in out.items()))
        return 1 if any(str(v).startswith("error") for v in out.values()) else 0
    return 2
