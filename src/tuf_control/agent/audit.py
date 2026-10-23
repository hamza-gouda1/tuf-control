"""Audit log: one JSON object per line, ~/.local/state/tuf-control/agent-audit.log.

Every tool call, resource read and prompt request is recorded with the time, the client
name from initialize, the tool, the (cleaned, size-capped) parameters, the decision and a
short result. Owner actions (grant, revoke, approve, deny) are recorded too. The file is
created 0600 and rotated at 512 KiB, keeping three old files.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path

from .sanitize import clean, clean_tree
from .store import audit_path

MAX_BYTES = 512 * 1024
KEEP = 3
FIELD_LIMIT = 1024


def _compact(obj, limit: int = FIELD_LIMIT):
    obj = clean_tree(obj, max_len=200, max_items=40)
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if len(text) <= limit:
        return obj
    return clean(text, limit)


class AuditLog:
    def __init__(self, path: Path | None = None, *, max_bytes: int = MAX_BYTES,
                 keep: int = KEEP, clock=time.time) -> None:
        self._path = path
        self.max_bytes = max_bytes
        self.keep = keep
        self.clock = clock

    @property
    def path(self) -> Path:
        return self._path or audit_path()

    def _rotate(self) -> None:
        p = self.path
        try:
            if p.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.keep - 1, 0, -1):
            older = p.with_name(f"{p.name}.{i}")
            if older.exists():
                os.replace(older, p.with_name(f"{p.name}.{i + 1}"))
        os.replace(p, p.with_name(f"{p.name}.1"))

    def write(self, *, client: str, kind: str, name: str, decision: str, params=None,
              result=None, request_id: str | None = None) -> dict:
        ts = self.clock()
        entry = {"time": datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds"),
                 "ts": round(ts, 3), "client": clean(client, 80), "kind": kind,
                 "tool": clean(name, 64), "decision": decision}
        if params is not None:
            entry["params"] = _compact(params)
        if result is not None:
            entry["result"] = _compact(result, 600)
        if request_id:
            entry["request_id"] = clean(request_id, 32)
        line = (json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        p = self.path
        try:
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._rotate()
                out = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(out, line)
                finally:
                    os.close(out)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        except OSError as exc:  # auditing must never take the server down; say so on stderr
            import sys
            print(f"tuf-control: audit log write failed: {exc}", file=sys.stderr)
        return entry

    def signature(self) -> tuple | None:
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def tail(self, n: int = 50) -> list[dict]:
        """The last n entries, oldest first. Reads at most the last 256 KiB."""
        n = max(0, min(int(n), 1000))
        if n == 0:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                data = fh.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if size > 256 * 1024:
            lines = lines[1:]  # first line may be cut
        out = []
        for raw in lines[-(n + 1):]:
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out[-n:]
