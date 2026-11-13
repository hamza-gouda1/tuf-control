# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Snaps via the snapd REST API on /run/snapd.socket.

Reading needs no privileges: GET /v2/snaps (installed) and GET /v2/find?select=refresh
(what the store would refresh; snapd asks the store, so this needs the network). Refresh
is POST /v2/snaps with X-Allow-Interaction, so snapd itself asks polkit.
"""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import time
from pathlib import Path

from .model import SourceResult, UpdateItem

SOCKET = Path("/run/snapd.socket")
TIMEOUT_S = 30
FIND_TRIES = 3        # /v2/find?select=refresh asks the store: one dropped reply must not
FIND_BACKOFF_S = 2.0  # fail the source; try again after 2s, then 4s, before the CLI fallback


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except BaseException:
            s.close()
            raise
        self.sock = s


def snapd(method: str, path: str, body: dict | None = None, *, sock: Path = SOCKET,
          timeout: float = TIMEOUT_S, interactive: bool = False) -> dict:
    conn = _UnixHTTP(str(sock), timeout)
    headers = {"Host": "localhost"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if interactive:
        headers["X-Allow-Interaction"] = "true"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        doc = json.loads(resp.read() or b"{}")
    finally:
        conn.close()
    return doc


def _err(doc: dict) -> str | None:
    if doc.get("type") == "error":
        res = doc.get("result") or {}
        return res.get("message") or res.get("kind") or "snapd error"
    return None


def parse_refresh(installed: list[dict], refresh: list[dict]) -> list[UpdateItem]:
    """Items from /v2/snaps and /v2/find?select=refresh results."""
    cur = {s.get("name"): s for s in installed or []}
    out = []
    for s in refresh or []:
        name = s.get("name") or ""
        old = cur.get(name, {})
        rev = s.get("revision")
        out.append(UpdateItem(
            id=f"snap:{name}", source="snap", name=name,
            current=old.get("version"), available=s.get("version"),
            size=s.get("download-size") or None, description=s.get("summary") or "",
            apply_method="snapd",
            kind="runtime" if s.get("type") in ("base", "core", "snapd", "os") else
            ("kernel" if s.get("type") == "kernel" else "app"),
            needs_reboot=s.get("type") in ("kernel", "gadget"),
            extra={"revision": str(rev) if rev is not None else None,
                   "channel": s.get("channel") or old.get("tracking-channel"),
                   "publisher": (s.get("publisher") or {}).get("display-name")}))
    return sorted(out, key=lambda i: i.name)


def parse_refresh_list(text: str) -> list[UpdateItem]:
    """`snap refresh --list` table (fallback): Name Version Rev Size Publisher Notes."""
    out = []
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("Name"):
        return out
    for line in lines[1:]:
        f = line.split()
        if len(f) < 3:
            continue
        out.append(UpdateItem(id=f"snap:{f[0]}", source="snap", name=f[0], available=f[1],
                              apply_method="snapd", kind="app",
                              extra={"revision": f[2]}))
    return out


def available(sock: Path = SOCKET) -> tuple[bool, str]:
    if not sock.exists():
        return False, "snapd is not installed or not running"
    return True, ""


def _find_refresh(sock: Path) -> dict:
    """/v2/find?select=refresh, tried FIND_TRIES times with a short pause between them:
    snapd asks the store for this one, so a single dropped reply or slow store round-trip
    must not fail the whole source (and the CLI fallback reaches the same store anyway)."""
    last: Exception | None = None
    for attempt in range(FIND_TRIES):
        if attempt:
            time.sleep(FIND_BACKOFF_S * attempt)
        try:
            doc = snapd("GET", "/v2/find?select=refresh", sock=sock)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last = exc
            continue
        e = _err(doc)
        if not e:
            return doc
        if (doc.get("result") or {}).get("kind") == "snap-not-found":
            return {"result": []}  # the store's answer, not a failure: no retry
        last = RuntimeError(e)
    raise last  # FIND_TRIES >= 1, so every failure path here has something to raise


def check(sock: Path = SOCKET) -> SourceResult:
    ok, why = available(sock)
    res = SourceResult("snap", available=ok, reason=why)
    if not ok:
        return res
    try:
        inst = snapd("GET", "/v2/snaps", sock=sock)
        if _err(inst):
            raise RuntimeError(_err(inst))
        ref = _find_refresh(sock)
        res.items = parse_refresh(inst.get("result") or [], ref.get("result") or [])
        res.info["method"] = "snapd REST"
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        res.info["rest_error"] = str(exc)  # kept even when the CLI fallback rescues the check
        try:  # the CLI talks to the same daemon and store; worth one try for its own view
            r = subprocess.run(["snap", "refresh", "--list"], capture_output=True, text=True,
                               timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                res.items = parse_refresh_list(r.stdout)
                res.info["method"] = "snap refresh --list"
                return res
        except (OSError, subprocess.SubprocessError) as cli_exc:
            cli_why = f"{type(cli_exc).__name__}: {cli_exc}"
        else:
            cli_why = (r.stderr or "").strip() or (r.stdout or "").strip() or \
                f"exit code {r.returncode}"
        res.error = f"snapd: {exc}; 'snap refresh --list' also failed ({cli_why})"
    return res


def refresh(names: list[str] | None, *, sock: Path = SOCKET, timeout_s: float = 1800,
            progress=None, watch=None, cancel=None) -> str:
    """Refresh the named snaps (all when None). snapd shows the polkit prompt; the change
    it starts is polled for live progress (per-task done/total) and can be aborted."""
    body = {"action": "refresh"}
    if names:
        body["snaps"] = list(names)
    doc = snapd("POST", "/v2/snaps", body, sock=sock, interactive=True)
    if _err(doc):
        raise RuntimeError(_err(doc))
    change = doc.get("change")
    if watch:
        watch.phase("Starting", package=", ".join(names or [])[:120] or "all snaps",
                    cancellable=True)
        watch.line(f"snapd change {change} started")
    last_task = ""
    deadline = time.monotonic() + timeout_s
    aborted = False
    while change and time.monotonic() < deadline:
        if cancel is not None and cancel.is_set() and not aborted:
            snapd("POST", f"/v2/changes/{change}", {"action": "abort"}, sock=sock)
            aborted = True
        st = snapd("GET", f"/v2/changes/{change}", sock=sock)
        if _err(st):
            raise RuntimeError(_err(st))
        r = st.get("result") or {}
        tasks = r.get("tasks") or []
        if tasks and (watch or progress):
            done = sum(int((t.get("progress") or {}).get("done") or 0) for t in tasks)
            total = sum(int((t.get("progress") or {}).get("total") or 0) for t in tasks)
            run = next((str(t["summary"]) for t in tasks if t.get("status") == "Do"
                        and t.get("summary")), "")
            if run and run != last_task:
                last_task = run
                if watch:
                    watch.line(run)
                if progress:
                    progress(run)
            if watch and total:
                watch.phase("Installing" if not aborted else "Cancelling",
                            percent=100.0 * done / total, package=run[:120])
        if progress and not watch:
            progress(r.get("summary") or "")
        if r.get("ready"):
            if r.get("err") or aborted:
                raise RuntimeError("cancelled" if aborted else r["err"])
            return r.get("summary") or "done"
        time.sleep(1.0)
    if change:
        raise TimeoutError("snapd did not finish before the timeout; check its change log")
    return "nothing to do"
