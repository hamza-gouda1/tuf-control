"""`tuf-control agent ...` and `tuf-control mcp` (registered from cli.py)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from . import policy
from .approvals import ApprovalQueue
from .audit import AuditLog
from .store import access_path, approvals_path, audit_path


def add_parsers(sub) -> None:
    a = sub.add_parser("agent", help="agent access: levels, grants, approvals, audit log")
    s = a.add_subparsers(dest="sub", required=True)
    s.add_parser("status", help="access level, standing grant, pending requests")
    g = s.add_parser("grant", help="set the access level (with --for: a standing grant)")
    g.add_argument("level", choices=policy.LEVELS)
    g.add_argument("--for", dest="duration", metavar="DURATION",
                   help="standing grant for 15m, 1h, 1h30m ... (max 24h): control-basic "
                        "writes need no per-call approval until it expires")
    g.add_argument("--enable-high-risk", action="store_true",
                   help="also enable the high-risk tools (they still need per-call approval)")
    g.add_argument("--disable-high-risk", action="store_true")
    s.add_parser("revoke", help="kill switch: access off, grants and pending requests cancelled")
    s.add_parser("pending", help="requests waiting for your approval")
    for name in ("approve", "deny"):
        x = s.add_parser(name, help=f"{name} a pending request" + (" once" if name ==
                                                                   "approve" else ""))
        x.add_argument("id")
    au = s.add_parser("audit", help="the agent audit log")
    au.add_argument("--tail", type=int, default=20, metavar="N")
    s.add_parser("schema", help="the full tool manifest as JSON")

    m = sub.add_parser("mcp", help="run the MCP server on stdio (for MCP clients)")
    m.add_argument("--print-config", action="store_true",
                   help="print a client configuration snippet and exit")


def _emit(args, data, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if args.json else text)


def _when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"


def status_data(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    acc = policy.load_access()
    q = ApprovalQueue()
    return {**acc.public(now), "pending": len(q.pending()),
            "files": {"access": str(access_path()), "approvals": str(approvals_path()),
                      "audit": str(audit_path())}}


def _owner_audit(name: str, decision: str, params=None, rid=None) -> None:
    AuditLog().write(client="owner:cli", kind="owner", name=name, decision=decision,
                     params=params, request_id=rid)


def run(args, ctl=None) -> int:
    if args.cmd == "mcp":
        return mcp_main(["--print-config"] if args.print_config else [], json_out=True)
    sub = args.sub
    if sub == "status":
        d = status_data()
        g = d["standing_grant"]
        lines = [f"agent access: {'on' if d['enabled'] else 'OFF'}",
                 f"level: {d['level']} (effective {d['effective_level']})",
                 "standing grant: " + (f"{g['level']} for "
                                       f"{policy.format_remaining(g['remaining_s'])}"
                                       if g else "none"),
                 f"high-risk tools: {'enabled' if d['high_risk_tools'] else 'disabled'}",
                 f"pending requests: {d['pending']}"]
        if d["error"]:
            lines.insert(0, f"agent.json error (all access off): {d['error']}")
        _emit(args, d, "\n".join(lines))
    elif sub == "grant":
        secs = policy.parse_duration(args.duration) if args.duration else None
        hr = True if args.enable_high_risk else False if args.disable_high_risk else None
        acc = policy.grant(args.level, secs, high_risk=hr)
        _owner_audit("grant", "grant", {"level": args.level, "for_s": secs, "high_risk": hr})
        d = acc.public()
        msg = f"level {d['level']}, effective {d['effective_level']}"
        if d["standing_grant"]:
            msg += (f"; {d['standing_grant']['level']} writes pre-approved for "
                    f"{policy.format_remaining(d['standing_grant']['remaining_s'])}")
        if d["high_risk_tools"]:
            msg += "; high-risk tools enabled (per-call approval)"
        _emit(args, d, msg)
    elif sub == "revoke":
        policy.revoke()
        n = ApprovalQueue().cancel_all(by="owner:cli")
        _owner_audit("revoke", "revoke", {"cancelled": n})
        _emit(args, {"revoked": True, "cancelled_requests": n},
              f"agent access off; {n} open request{'s' if n != 1 else ''} cancelled. "
              "`tuf-control agent grant read` turns read-only access back on.")
    elif sub == "pending":
        items = ApprovalQueue().pending()
        text = "\n".join(f"{e['id']}  {e['tier']:<5} {e['summary']}\n"
                         f"    from {e['client']} at {_when(e['created_at'])}, expires "
                         f"{_when(e['expires_at'])}" + (f"\n    stated reason: “{e['reason']}”"
                                                        if e.get("reason") else "")
                         for e in items) or "no pending requests"
        _emit(args, {"pending": items}, text)
    elif sub in ("approve", "deny"):
        e = ApprovalQueue().decide(args.id, sub == "approve", by="owner:cli")
        _owner_audit(e["tool"], sub, e["params"], e["id"])
        _emit(args, e, f"{sub}d {e['id']}: {e['summary']}" +
              (" (the client can now repeat the call once)" if sub == "approve" else ""))
    elif sub == "audit":
        rows = AuditLog().tail(args.tail)
        text = "\n".join(f"{r.get('time', '')[11:19]} {r.get('client', ''):<18} "
                         f"{r.get('decision', ''):<12} {r.get('tool', '')}"
                         + (f"  {json.dumps(r['params'])}" if r.get("params") else "")
                         for r in rows) or "audit log is empty"
        _emit(args, {"entries": rows}, text)
    elif sub == "schema":
        from .tools import AgentService
        print(json.dumps(AgentService(ctl=ctl).manifest(), indent=2))
    return 0


# -- mcp ---------------------------------------------------------------------------------

def launcher() -> tuple[str, list[str], dict]:
    """(command, args, env) for a client config: the installed launcher when there is one."""
    found = shutil.which("tuf-control")
    if not found:
        cand = Path.home() / ".local/bin/tuf-control"
        found = str(cand) if os.access(cand, os.X_OK) else None
    if found:
        return found, ["mcp"], {}
    src = Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(src)} if (src / "tuf_control").is_dir() else {}
    return sys.executable, ["-m", "tuf_control", "mcp"], env


def client_config() -> dict:
    cmd, args, env = launcher()
    entry = {"command": cmd, "args": args}
    if env:
        entry["env"] = env
    return {"mcpServers": {"tuf-control": entry}}


def mcp_main(argv: list[str] | None = None, json_out: bool = True) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--print-config" in argv:
        print(json.dumps(client_config(), indent=2))
        return 0
    if argv and argv != ["mcp"]:
        print("usage: tuf-control-mcp [--print-config]", file=sys.stderr)
        return 2
    from .mcp import serve
    return serve()


__all__ = ["add_parsers", "client_config", "mcp_main", "run"]
