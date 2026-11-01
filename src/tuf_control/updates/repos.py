# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Optional (off by default): is a local checkout of TUF Control / Bordeaux GNOME behind its
upstream branch? Compares HEAD with the remote-tracking branch as of the last fetch; it does
not fetch (no network, no credentials)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .model import SourceResult, UpdateItem

DEFAULT_REPOS = ["~/projects/tuf-control", "~/projects/bordeaux-gnome"]


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                       timeout=10, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def repo_item(path: Path, git=_git) -> UpdateItem | None:
    upstream = git(path, "rev-parse", "--abbrev-ref", "@{u}")
    behind = int(git(path, "rev-list", "--count", "HEAD..@{u}") or 0)
    if behind <= 0:
        return None
    head = git(path, "rev-parse", "--short", "HEAD")
    new = git(path, "rev-parse", "--short", "@{u}")
    return UpdateItem(id=f"repos:{path.name}", source="repos", name=path.name, current=head,
                      available=new, apply_method="git", kind="repo",
                      description=f"{behind} commit{'s' if behind != 1 else ''} behind "
                                  f"{upstream} (as of the last fetch). Pull it yourself: "
                                  "`git pull` in the checkout.",
                      extra={"path": str(path), "behind": behind, "upstream": upstream})


def check(paths: list[str], git=_git) -> SourceResult:
    res = SourceResult("repos")
    errors = []
    for p in paths:
        path = Path(p).expanduser()
        if not (path / ".git").exists():
            continue
        try:
            it = repo_item(path, git)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if it:
            res.items.append(it)
    if errors:
        res.error = "; ".join(errors)
    return res
