# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Flatpak apps and runtimes, system and per-user installations.

Check: `flatpak remote-ls --updates --cached` (the remotes' cached summaries; no network,
so it is safe offline and as fresh as the last flatpak operation) per installation, plus
`flatpak list` for the installed versions. Apply: `flatpak update --user` needs nothing;
`flatpak update --system` goes through flatpak's own system helper, which asks polkit.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from .model import SourceResult, UpdateItem

TIMEOUT_S = 60
COLS = "application,version,branch,arch,origin,download-size"
LIST_COLS = "application,version,branch,arch,installation"


def _units(text: str) -> int | None:
    """'77.6 MB' / '1,2 kB' / '512 bytes' -> bytes."""
    t = text.strip().replace("\xa0", " ").replace(",", ".")
    if not t:
        return None
    parts = t.split()
    try:
        v = float(parts[0])
    except ValueError:
        return None
    mult = {"bytes": 1, "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024**2,
            "gib": 1024**3}.get(parts[1].lower() if len(parts) > 1 else "b", 1)
    return int(v * mult)


def parse_list(text: str) -> dict[tuple[str, str, str], str]:
    """`flatpak list --columns=application,version,branch,arch,installation` ->
    {(app, branch, installation): version}."""
    out = {}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) >= 5:
            out[(f[0], f[2], f[4])] = f[1]
    return out


def parse_remote_ls(text: str, installation: str,
                    installed: dict | None = None) -> list[UpdateItem]:
    """`flatpak remote-ls --updates --columns=application,version,branch,arch,origin,
    download-size` (tab separated) -> items."""
    installed = installed or {}
    out = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 5 or not f[0] or f[0].lower() == "application id":
            continue
        app, ver, branch, arch, origin = f[:5]
        size = _units(f[5]) if len(f) > 5 else None
        runtime = (".Platform" in app or ".Sdk" in app or app.count(".") >= 3
                   and any(x in app for x in (".GL.", ".VAAPI.", ".Locale", ".Debug",
                                              ".codecs", ".Extension")))
        out.append(UpdateItem(
            id=f"flatpak:{installation}:{app}/{arch}/{branch}", source="flatpak", name=app,
            current=installed.get((app, branch, installation)) or None,
            available=ver or None, size=size, kind="runtime" if runtime else "app",
            apply_method=f"flatpak-{installation}",
            description=f"{origin} · {branch}" + (" · per-user" if installation == "user"
                                                   else ""),
            extra={"installation": installation, "branch": branch, "arch": arch,
                   "origin": origin, "ref": f"{app}/{arch}/{branch}"}))
    return out


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": "C.UTF-8"}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, env=env,
                          stdin=subprocess.DEVNULL)


def available() -> tuple[bool, str]:
    if not shutil.which("flatpak"):
        return False, "flatpak is not installed"
    return True, ""


def check() -> SourceResult:
    ok, why = available()
    res = SourceResult("flatpak", available=ok, reason=why)
    if not ok:
        return res
    installed: dict = {}
    r = _run(["flatpak", "list", f"--columns={LIST_COLS}"])
    if r.returncode == 0:
        installed = parse_list(r.stdout)
    errors = []
    for inst in ("system", "user"):
        if inst == "user" and not any(k[2] == "user" for k in installed):
            continue  # no per-user installation in use
        r = _run(["flatpak", "remote-ls", f"--{inst}", "--updates", "--cached",
                  f"--columns={COLS}"])
        if r.returncode != 0:
            errors.append(f"{inst}: {(r.stderr or '').strip()[:160]}")
            continue
        res.items += parse_remote_ls(r.stdout, inst, installed)
    res.info["installed"] = len(installed)
    res.info["method"] = "flatpak remote-ls --cached"
    if errors and not res.items:
        res.error = "; ".join(errors)
    return res


def update_cmd(installation: str, refs: list[str] | None = None) -> list[str]:
    if installation not in ("user", "system"):
        raise ValueError("unknown Flatpak installation")
    for ref in refs or []:
        if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
                or any(part in ("", ".", "..") for part in ref.split("/"))):
            raise ValueError("invalid Flatpak reference")
    return ["flatpak", "update", f"--{installation}", "-y", "--noninteractive", "--", *(refs or [])]
