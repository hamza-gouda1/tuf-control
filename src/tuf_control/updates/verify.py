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
    """Installed version per item id, read from each package system (None: not found)."""
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


def _apt(items):
    names = sorted({f"{i.name}:{i.extra.get('arch')}" if i.extra.get("arch") else i.name
                    for i in items})
    r = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Architecture}\t${Version}\t"
                        "${db:Status-Abbrev}\n", *names], capture_output=True, text=True,
                       timeout=30, stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"})
    got = {}
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) >= 4 and f[3].strip().startswith(("ii", "hi")):
            got[(f[0], f[1])] = f[2]
    return {i.id: got.get((i.name, i.extra.get("arch"))) or got.get((i.name, "all"))
            for i in items}


def _snap(items):
    from .snap import snapd
    doc = snapd("GET", "/v2/snaps")
    cur = {s.get("name"): s.get("version") for s in doc.get("result") or []}
    return {i.id: cur.get(i.name) for i in items}


def _flatpak(items):
    from .flatpak import LIST_COLS, parse_list
    r = subprocess.run(["flatpak", "list", f"--columns={LIST_COLS}"], capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    inst = parse_list(r.stdout)
    return {i.id: inst.get((i.name, i.extra.get("branch"), i.extra.get("installation")))
            for i in items}


def _firmware(items):
    from .firmware import FwupdClient
    devs = {d.get("DeviceId"): d.get("Version") for d in FwupdClient().devices()}
    return {i.id: devs.get(i.extra.get("device_id")) for i in items}


def verify(items: list[UpdateItem], before: dict, after: dict, outcomes: dict,
           report: Report | None) -> list[dict]:
    """Per-item verified result. `outcomes`: apply method -> backend message ('error: ...'
    for a failure). `report`: the check made after applying (for blocker reasons)."""
    pending = {i.id: i for i in (report.items() if report else [])}
    out = []
    for i in items:
        old, new = before.get(i.id), after.get(i.id)
        msg = str(outcomes.get(_method(i), ""))
        err = msg[7:] if msg.startswith("error: ") else ""
        changed = new is not None and new != old and new != i.current
        res = {"id": i.id, "source": i.source, "name": i.name, "old": old or i.current,
               "new": new, "offered": i.available, "result": "", "reason": ""}
        if changed:
            res["result"] = "installed"
            if i.available and new != i.available:
                res["reason"] = f"installed {new}; {i.available} was offered"
        elif i.apply_method == "fwupd" and not err and i.needs_reboot:
            res["result"] = "restart"
            res["reason"] = "fwupd staged the firmware; it is written during the next restart"
        elif err:
            res["result"] = "failed"
            res["reason"] = err
        else:
            res["result"] = "pending"
            still = pending.get(i.id)
            bl = still.blocker if still is not None else None
            if bl:
                res["reason"] = f"{bl['title']}: {bl['reason']}"
                res["command"] = bl.get("command")
            elif new is None and old is None:
                res["reason"] = "could not read the installed version"
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
