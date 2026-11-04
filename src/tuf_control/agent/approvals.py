# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Per-call approval queue (~/.local/state/tuf-control/agent-approvals.json).

The MCP server records a request (tool, validated parameters, the agent's stated
reason); the owner approves once or denies it in the app, the CLI or a notification; the
agent polls get_approval_status and then repeats the exact same call with approval_id.
An approval is bound to the tool, a hash of the normalised parameters and the server
session. The claimed client name is informational, not authentication. An approval is
used at most once and expires if it is not used soon.

States: pending -> approved -> used | pending -> denied | pending/approved -> expired |
any open state -> cancelled (kill switch).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from ..core import ValidationError
from .store import approvals_path, locked, read_json, write_json

PENDING_TTL_S = 600      # undecided requests expire after 10 minutes
APPROVED_TTL_S = 300     # an approval must be used within 5 minutes
MAX_PENDING = 5          # open requests across all clients
KEEP_CLOSED = 50         # closed entries kept for the record
ID_RE = r"^[0-9a-f]{16}$"
OPEN = ("pending", "approved")


class ApprovalError(ValidationError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def params_hash(tool: str, params: dict) -> str:
    blob = json.dumps({"tool": tool, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class ApprovalQueue:
    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self._path = path
        self.clock = clock

    @property
    def path(self) -> Path:
        return self._path or approvals_path()

    # -- storage -----------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            data = read_json(self.path, 1 << 20)
        except (OSError, ValueError):
            return []  # a broken queue holds no approvals (fail closed)
        items = data.get("requests") if isinstance(data, dict) else None
        return [x for x in items if isinstance(x, dict) and "id" in x] if isinstance(
            items, list) else []

    def _save(self, items: list[dict]) -> None:
        closed = [x for x in items if x["status"] not in OPEN]
        open_ = [x for x in items if x["status"] in OPEN]
        write_json(self.path, {"version": 1, "requests": open_ + closed[-KEEP_CLOSED:]})

    def _expire(self, items: list[dict], now: float) -> bool:
        changed = False
        for x in items:
            if x["status"] == "pending" and now >= x["expires_at"]:
                x.update(status="expired", decided_at=now, decided_by="timeout")
                changed = True
            elif x["status"] == "approved" and now >= (x.get("use_by") or 0):
                x.update(status="expired")
                changed = True
        return changed

    def _rmw(self, fn):
        with locked(self.path):
            items = self._load()
            now = self.clock()
            changed = self._expire(items, now)
            result, dirty = fn(items, now)
            if changed or dirty:
                self._save(items)
            return result

    # -- queries -------------------------------------------------------------------
    def list(self, status: str | None = None) -> list[dict]:
        def fn(items, _now):
            return [dict(x) for x in items if status is None or x["status"] == status], False
        return self._rmw(fn)

    def pending(self) -> list[dict]:
        return self.list("pending")

    def get(self, rid: str) -> dict | None:
        def fn(items, _now):
            for x in items:
                if x["id"] == rid:
                    return dict(x), False
            return None, False
        return self._rmw(fn)

    def signature(self) -> tuple | None:
        """Cheap change check for the GUI (stat only)."""
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    # -- changes ----------------------------------------------------------------------
    def request(self, *, tool: str, tier: str, params: dict, summary: str, reason: str,
                client: str, session: str, process: dict) -> tuple[dict, bool]:
        """(entry, created). An identical open request from the same client is reused."""
        h = params_hash(tool, params)

        def fn(items, now):
            for x in items:
                if (x["status"] in OPEN and x["params_hash"] == h and
                        x["client"] == client and x.get("session") == session):
                    return (dict(x), False), False
            if sum(1 for x in items if x["status"] == "pending") >= MAX_PENDING:
                raise ApprovalError("queue_full", f"{MAX_PENDING} requests are already "
                                    "waiting for the owner; wait for a decision")
            entry = {"id": secrets.token_hex(8), "tool": tool, "tier": tier,
                     "params": params, "params_hash": h, "summary": summary,
                     "reason": reason, "client": client, "session": session,
                     "process": process, "created_at": now,
                     "expires_at": now + PENDING_TTL_S, "status": "pending",
                     "decided_at": None, "decided_by": None, "use_by": None,
                     "used_at": None}
            items.append(entry)
            return (dict(entry), True), True
        return self._rmw(fn)

    def decide(self, rid: str, approve: bool, by: str) -> dict:
        def fn(items, now):
            for x in items:
                if x["id"] == rid:
                    if x["status"] != "pending":
                        raise ApprovalError("not_pending", f"request {rid} is {x['status']}")
                    x.update(status="approved" if approve else "denied", decided_at=now,
                             decided_by=by, use_by=now + APPROVED_TTL_S if approve else None)
                    return dict(x), True
            raise ApprovalError("unknown_request", f"no request with id {rid}")
        return self._rmw(fn)

    def consume(self, rid: str, *, tool: str, params: dict, client: str, session: str) -> dict:
        """Mark an approval used, after checking it covers exactly this call."""
        h = params_hash(tool, params)

        def fn(items, now):
            for x in items:
                if x["id"] != rid:
                    continue
                st = x["status"]
                if st == "pending":
                    raise ApprovalError("still_pending", "the owner has not decided yet; "
                                        "poll get_approval_status")
                if st != "approved":
                    raise ApprovalError(f"approval_{st}", f"request {rid} is {st}")
                if x["tool"] != tool or x["params_hash"] != h:
                    raise ApprovalError("approval_mismatch", "this approval was given for "
                                        "a different tool or different parameters")
                if x["client"] != client or x.get("session") != session:
                    raise ApprovalError("approval_mismatch", "this approval belongs to "
                                        "another client or server session")
                x.update(status="used", used_at=now)
                return dict(x), True
            raise ApprovalError("unknown_request", f"no request with id {rid}")
        return self._rmw(fn)

    def cancel_all(self, by: str = "owner") -> int:
        def fn(items, now):
            n = 0
            for x in items:
                if x["status"] in OPEN:
                    x.update(status="cancelled", decided_at=now, decided_by=by)
                    n += 1
            return n, n > 0
        return self._rmw(fn)
