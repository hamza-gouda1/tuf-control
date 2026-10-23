"""tuf-control command line. Reads are always safe; writes validate and honour --dry-run."""
from __future__ import annotations

import argparse
import json
import sys

from .controller import Controller
from .core import Capability, Profile, TufControlError, jsonable


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tuf-control", description="ASUS TUF/ROG control")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="summary of everything readable")
    sub.add_parser("telemetry", help="temperatures, fans, GPU, battery power")
    sub.add_parser("limits", help="power limits with firmware bounds")
    sub.add_parser("capabilities", help="what each backend can do and why not")

    prof = sub.add_parser("profile", help="platform profile")
    ps = prof.add_subparsers(dest="sub", required=True)
    ps.add_parser("get")
    pset = ps.add_parser("set")
    pset.add_argument("name", help="quiet | balanced | performance")
    pset.add_argument("--dry-run", action="store_true")

    lim = sub.add_parser("limit", help="power limits (write)")
    ls = lim.add_subparsers(dest="sub", required=True)
    lset = ls.add_parser("set")
    lset.add_argument("name")
    lset.add_argument("value", type=int)
    lset.add_argument("--clamp", action="store_true", help="clamp to bounds instead of refusing")
    lset.add_argument("--dry-run", action="store_true")

    bat = sub.add_parser("battery", help="battery charge limit (write)")
    bs = bat.add_subparsers(dest="sub", required=True)
    blim = bs.add_parser("limit")
    blim.add_argument("percent", type=int, help="20-100")
    blim.add_argument("--dry-run", action="store_true")
    return p


def _emit(args, data, text: str) -> None:
    print(json.dumps(jsonable(data), indent=2) if args.json else text)


def _safe(fn):
    try:
        value, backend = fn()
        return {"value": jsonable(value), "backend": backend}
    except TufControlError as exc:
        return {"error": str(exc)}


def _fmt_limits(limits) -> str:
    rows = []
    for x in limits:
        rng = f"[{x.min}..{x.max}]" if x.bounded else "[bounds unknown]"
        rows.append(f"{x.name:<20} current={x.current} default={x.default} {rng}")
    return "\n".join(rows) or "no power limits"


def run(args, ctl: Controller) -> int:
    cmd = args.cmd
    if cmd == "capabilities":
        caps = ctl.capabilities()
        lines = []
        for k, v in caps.items():
            head = f"{k:<20} {v['backend'] or 'UNAVAILABLE'}"
            if v["fallbacks"]:
                head += f"  (fallbacks: {', '.join(v['fallbacks'])})"
            lines.append(head)
            for b, why in v["unavailable"].items():
                lines.append(f"    {b}: {why}")
        _emit(args, caps, "\n".join(lines))
    elif cmd == "status":
        reads = {
            "profile": ctl.get_profile,
            "battery_threshold": ctl.get_battery_threshold,
            "battery_health": lambda: ctl.read(Capability.BATTERY_HEALTH),
            "gpu_mode": lambda: ctl.read(Capability.GPU_MODE),
            "kbd_brightness": lambda: ctl.read(Capability.KBD_BRIGHTNESS),
            "power_limits": ctl.get_limits,
            "fan_curves": lambda: ctl.read(Capability.FAN_CURVES),
        }
        data = {k: _safe(fn) for k, fn in reads.items()}
        data["cross_check"] = ctl.cross_check()
        text = []
        for k, v in data.items():
            if k == "cross_check":
                for row in v:
                    text.append(f"cross-check {row['capability']}: "
                                f"{'agree' if row['agree'] else 'DIFFER'} {row['values']}")
            elif "error" in v:
                text.append(f"{k:<18} unavailable ({v['error']})")
            else:
                text.append(f"{k:<18} [{v['backend']}] {json.dumps(v['value'])}")
        _emit(args, data, "\n".join(text))
    elif cmd == "telemetry":
        t, backend = ctl.read(Capability.TELEMETRY)
        _emit(args, {"backend": backend, **jsonable(t)},
              "\n".join(f"{k}: {v}" for k, v in jsonable(t).items()))
    elif cmd == "limits":
        limits, backend = ctl.get_limits()
        _emit(args, {"backend": backend, "limits": limits}, _fmt_limits(limits))
    elif cmd == "profile" and args.sub == "get":
        prof, backend = ctl.get_profile()
        _emit(args, {"profile": prof, "backend": backend}, f"{prof.value} ({backend})")
    elif cmd == "profile":
        res = ctl.set_profile(Profile.parse(args.name), dry_run=args.dry_run)
        _emit(args, res.__dict__, f"{res.detail} via {res.backend}")
    elif cmd == "limit":
        res = ctl.set_limit(args.name, args.value, dry_run=args.dry_run, clamp=args.clamp)
        _emit(args, res.__dict__, f"{res.detail} via {res.backend}")
    elif cmd == "battery":
        res = ctl.set_battery_threshold(args.percent, dry_run=args.dry_run)
        _emit(args, res.__dict__, f"{res.detail} via {res.backend}")
    return 0


def main(argv: list[str] | None = None, controller: Controller | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args, controller or Controller())
    except TufControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
