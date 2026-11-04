# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Audit log: one JSON object per line, ~/.local/state/tuf-control/agent-audit.log.

Every tool call, resource read and prompt request is recorded with the time, the client
name from initialize, the OS-reported launcher (PID and executable), the tool, the
(cleaned, size-capped) parameters, the decision and a short result. Owner actions and
pending/executed writes are also retained in agent-audit-important.log, independently
of routine or refused call traffic. Both logs are created 0600 and rotated at 512 KiB,
keeping three old files. The activity view reserves space for important events.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
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

    @property
    def important_path(self) -> Path:
        p = self.path
        return p.with_name(f"{p.stem}-important{p.suffix}")

    def _rotate(self, p: Path) -> None:
        try:
            if p.lstat().st_size < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.keep - 1, 0, -1):
            older = p.with_name(f"{p.name}.{i}")
            if older.exists():
                os.replace(older, p.with_name(f"{p.name}.{i + 1}"))
        os.replace(p, p.with_name(f"{p.name}.1"))

    def write(self, *, client: str, kind: str, name: str, decision: str, params=None,
              result=None, request_id: str | None = None, process: dict | None = None) -> dict:
        ts = self.clock()
        entry = {"time": datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds"),
                 "ts": round(ts, 3), "order_ns": time.time_ns(), "id": secrets.token_hex(16),
                 "client": clean(client, 80), "kind": kind,
                 "tool": clean(name, 64), "decision": decision}
        if process is not None:
            entry["process"] = _compact(process)
        if params is not None:
            entry["params"] = _compact(params)
        if result is not None:
            entry["result"] = _compact(result, 600)
        if request_id:
            entry["request_id"] = clean(request_id, 32)
        line = (json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        # Classification uses internal call metadata, never a claimed client name.
        # Routine refusals stay in the ordinary class so an unapproved flood cannot
        # displace the owner's decisions or already executed writes.
        if kind == "owner" or (kind == "tool" and decision in ("pending", "executed")):
            self._append(self.important_path, line)
        self._append(self.path, line)
        return entry

    def _append(self, p: Path, line: bytes) -> None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("audit lock is not a regular file")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                if p.is_symlink():
                    raise OSError("audit log must not be a symbolic link")
                self._rotate(p)
                out = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW |
                              os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(out).st_mode):
                        raise OSError("audit log is not a regular file")
                    os.fchmod(out, 0o600)
                    remaining = memoryview(line)
                    while remaining:
                        n = os.write(out, remaining)
                        if n <= 0:
                            raise OSError("audit append failed")
                        remaining = remaining[n:]
                finally:
                    os.close(out)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        except OSError as exc:  # auditing must never take the server down; say so on stderr
            import sys
            print(f"tuf-control: audit log write failed: {exc}", file=sys.stderr)

    def signature(self) -> tuple:
        def sig(p):
            try:
                st = p.stat()
                return (st.st_mtime_ns, st.st_size, st.st_ino)
            except OSError:
                return None
        return sig(self.path), sig(self.important_path)

    @staticmethod
    def _read_tail(path: Path, n: int) -> list[dict]:
        """Bounded read of a single regular file, including old rotation files."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    return []
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                data = fh.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if size > 256 * 1024:
            lines = lines[1:]
        out = []
        for raw in lines[-(n + 1):]:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out[-n:]

    def tail(self, n: int = 50) -> list[dict]:
        """Recent activity with half the slots reserved for retained important events.

        Results remain chronological. A one-entry query returns the newest event.
        Each file read is capped at 256 KiB; protected rotations are read as well.
        """
        n = max(0, min(int(n), 1000))
        if n == 0:
            return []
        important = []
        p = self.important_path
        for i in range(self.keep, -1, -1):
            rotated = p.with_name(f"{p.name}.{i}") if i else p
            important.extend(self._read_tail(rotated, n))
        def key(e):
            ts, order = e.get("ts", 0), e.get("order_ns", 0)
            return (ts if isinstance(ts, (int, float)) else 0,
                    order if isinstance(order, int) else 0)

        def identity(e):
            event_id = e.get("id")
            return event_id if isinstance(event_id, str) else json.dumps(e, sort_keys=True)

        important.sort(key=key)
        all_events = {identity(e): e
                      for e in important + self._read_tail(self.path, n)}
        recent = sorted(all_events.values(), key=key)
        reserved = important[-max(1, n // 2):] if n > 1 else []
        selected = {identity(e): e for e in reserved}
        for e in reversed(recent):
            if len(selected) >= n:
                break
            selected[identity(e)] = e
        return sorted(selected.values(), key=key)
