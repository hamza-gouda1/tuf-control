# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Did an update really land? Compare installed versions before and after, item by item.

A backend saying "finished" is not proof (PackageKit skips held packages and returns
success). Each item ends as one of:

    installed  the installed version changed (normally to the offered one)
    restart    firmware staged by fwupd; it is written during the next restart
    pending    still at the old version: with the blocker apt reports, or "reported success
               but the version did not change"
    failed     the backend returned an error, and the version did not change
"""
from __future__ import annotations

import os
import subprocess

from .model import Report, UpdateItem

RESULT_TITLES = {"installed": "Installed", "restart": "Installs on restart",
                 "pending": "Still pending", "failed": "Failed"}


def _fake() -> bool:
    from ..controller import FAKE_ROOT_ENV
    return bool(os.environ.get(FAKE_ROOT_ENV))


def installed_versions(items: list[UpdateItem]) -> dict[str, str | None]:
    """Installed version per item id, read from each package system (None: not found).
    In fake mode this is the state after a successful apply (see fake.fake_installed):
    for the state before a run use before_versions()."""
    out: dict[str, str | None] = {i.id: None for i in items}
    if _fake():
        from .fake import fake_installed
        inst = fake_installed()
        for i in items:
            out[i.id] = inst.get(i.id, i.current)
        return out
    by: dict[str, list[UpdateItem]] = {}
    for i in items:
        by.setdefault(i.source, []).append(i)
    readers = {"apt": _apt, "snap": _snap, "flatpak": _flatpak, "firmware": _firmware}
    for src, its in by.items():
        fn = readers.get(src)
        if fn is None:
            continue
        try:
            out.update(fn(its))
        except Exception:
            pass  # unknown stays None: reported as "could not read"
    return out


def before_versions(items: list[UpdateItem]) -> dict[str, str | None]:
    """Versions before a run: the report's 'current' in fake mode (nothing is really
    installed there), the live package databases otherwise."""
    if _fake():
        return {i.id: i.current for i in items}
    return installed_versions(items)


def _apt(items):
    names = sorted({f"{i.name}:{i.extra.get('arch')}" if i.extra.get("arch") else i.name
                    for i in items})
    r = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Architecture}\t${Version}\t"
                        "${db:Status-Abbrev}\n", "--", *names], capture_output=True, text=True,
                       timeout=30, stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"})
    got = {}
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) >= 4 and f[3].strip().startswith(("ii", "hi")):
            got[(f[0], f[1])] = f[2]
    out = {}
    for i in items:
        version = got.get((i.name, i.extra.get("arch"))) or got.get((i.name, "all"))
        if version is None and not i.extra.get("arch"):
            versions = {v for (name, _arch), v in got.items() if name == i.name}
            version = next(iter(versions)) if len(versions) == 1 else None
        out[i.id] = version
    return out


def _snap(items):
    from .snap import snapd
    doc = snapd("GET", "/v2/snaps")
    if doc.get("type") == "error":
        return {i.id: None for i in items}
    cur = {s.get("name"): s for s in doc.get("result") or []}
    out = {}
    for i in items:
        snap = cur.get(i.name) or {}
        version, revision = snap.get("version"), snap.get("revision")
        out[i.id] = (f"{version or 'unversioned'} (revision {revision})"
                     if revision is not None else None)
    return out


def _flatpak(items):
    from .flatpak import LIST_COLS, parse_list
    r = subprocess.run(["flatpak", "list", f"--columns={LIST_COLS}"], capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode:
        return {i.id: None for i in items}
    inst = parse_list(r.stdout)
    out = {}
    for i in items:
        installation = i.extra.get("installation")
        if installation not in ("user", "system"):
            out[i.id] = None
            continue
        ref = i.extra.get("ref") or i.name
        commit = subprocess.run(["flatpak", "info", f"--{installation}", "--show-commit", "--",
                                 ref], capture_output=True, text=True, timeout=15,
                                stdin=subprocess.DEVNULL)
        version = inst.get((i.name, i.extra.get("branch"), installation))
        sha = commit.stdout.strip()
        out[i.id] = (f"{version or 'unversioned'} (commit {sha})"
                     if not commit.returncode and sha else None)
    return out


def _firmware(items):
    from .firmware import FwupdClient
    devs = {d.get("DeviceId"): d.get("Version") for d in FwupdClient().devices()}
    return {i.id: devs.get(i.extra.get("device_id")) for i in items}


def staged_firmware(items: list[UpdateItem]) -> dict[str, str]:
    """fwupd history must prove that the offered release is pending a reboot."""
    its = [i for i in items if i.source == "firmware"]
    if not its or _fake():
        return {}
    from .firmware import FwupdClient
    try:
        records = FwupdClient().history()
    except Exception:
        return {}
    staged = {}
    for i in its:
        record = next((r for r in records if r.get("DeviceId") ==
                       i.extra.get("device_id")), {})
        if record.get("UpdateState") not in (1, 4):  # PENDING, NEEDS_REBOOT
            continue
        versions = [r.get("Version") for r in record.get("Releases") or []]
        if i.available and i.available in versions:
            staged[i.id] = i.available
    return staged


def verify(items: list[UpdateItem], before: dict, after: dict, outcomes: dict,
           report: Report | None, cancelled: bool = False,
           staged: dict | None = None) -> list[dict]:
    """Per-item verified result. `outcomes`: apply method -> backend message ('error: ...'
    for a failure). `report`: the check made after applying (for blocker reasons).
    `cancelled`: the owner stopped the run; items that never started say so."""
    pending = {i.id: i for i in (report.items() if report else [])}
    out = []
    for i in items:
        old, new = before.get(i.id), after.get(i.id)
        msg = str(outcomes.get(_method(i), ""))
        err = msg[7:] if msg.startswith("error: ") else ""
        changed = new is not None and old is not None and new != old
        res = {"id": i.id, "source": i.source, "name": i.name, "old": old or i.current,
               "new": new, "offered": i.available, "result": "", "reason": ""}
        if changed:
            res["result"] = "installed"
            if i.available and new != i.available and i.source not in ("snap", "flatpak"):
                res["reason"] = f"installed {new}; {i.available} was offered"
        elif (i.apply_method == "fwupd" and msg and not err and not cancelled
              and (staged or {}).get(i.id) == i.available and i.available is not None):
            res["result"] = "restart"
            res["reason"] = "fwupd confirms this release is staged for the next restart"
        elif err:
            res["result"] = "failed" if "cancel" not in err.lower() else "pending"
            res["reason"] = err
        else:
            res["result"] = "pending"
            still = pending.get(i.id)
            bl = still.blocker if still is not None else None
            if bl:
                res["reason"] = f"{bl['title']}: {bl['reason']}"
                res["command"] = bl.get("command")
            elif cancelled and not msg:
                res["reason"] = "cancelled before it was installed"
            elif new is None or old is None:
                res["reason"] = "could not verify the installed version before and after the update"
            else:
                res["reason"] = (f"{_backend(i)} reported success but the installed version "
                                 f"did not change (still {new or old})")
        out.append(res)
    return out


def _method(i: UpdateItem) -> str:
    return "packagekit-deps" if i.extra.get("with_deps") else i.apply_method


def _backend(i: UpdateItem) -> str:
    return {"packagekit": "PackageKit", "snapd": "snapd", "fwupd": "fwupd",
            "flatpak-user": "flatpak", "flatpak-system": "flatpak"}.get(i.apply_method,
                                                                        "The installer")


def summary(results: list[dict]) -> str:
    """'2 of 3 installed, 1 still pending' (+ ', 1 failed', ', 1 on restart')."""
    n = len(results)
    if not n:
        return "Nothing was installed"
    c = {k: sum(1 for r in results if r["result"] == k) for k in RESULT_TITLES}
    text = f"{c['installed']} of {n} installed"
    if c["restart"]:
        text += f", {c['restart']} on restart"
    if c["pending"]:
        text += f", {c['pending']} still pending"
    if c["failed"]:
        text += f", {c['failed']} failed"
    return text


def all_installed(results: list[dict]) -> bool:
    return bool(results) and all(r["result"] in ("installed", "restart") for r in results)
