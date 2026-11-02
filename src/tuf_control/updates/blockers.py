# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Why apt keeps a package back, in plain language, with the command that would take it.

`classify(name, facts)` is pure; the facts come from apt itself in the check (apt.py):

    kept_back       not part of what a full upgrade would do right now
    dpkg_hold       the package's own dpkg selection is "hold" (apt-mark hold)
    held_involved   other held packages the update would have to change
    removals        packages installing it would remove
    phased_pct      Phased-Update-Percentage of the candidate (Ubuntu staged rollout)
    new_packages    packages it needs that are not installed yet (a new kernel ABI...)
    broken          apt could not find a consistent solution

Held and phased items wait by design (the owner's choice, or Ubuntu's rollout): they are
listed but not counted as updates and never part of "Update all".
"""
from __future__ import annotations

WAITING = ("held", "held-dependency", "phased")   # not counted, not in "Update all"
FIXABLE = ("needs-new",)                          # "Install with dependencies"
TITLES = {
    "held": "Held by you",
    "held-dependency": "Blocked by a hold",
    "phased": "Phased rollout",
    "needs-new": "Needs new packages",
    "conflict": "Would remove packages",
    "unknown": "Kept back by apt",
}
NVIDIA_NOTE = ("NVIDIA driver packages: updating them while the old kernel module is loaded "
               "breaks CUDA and nvidia-smi until you restart, which is a common reason to "
               "hold them.")


def _names(xs, n: int = 6) -> str:
    xs = list(xs)
    return ", ".join(xs[:n]) + (f" and {len(xs) - n} more" if len(xs) > n else "")


def classify(name: str, facts: dict | None) -> dict | None:
    """{"blocker", "title", "reason", "command", "fixable"} or None for a normal update."""
    f = facts or {}
    if not f.get("kept_back"):
        return None
    new = list(f.get("new_packages") or [])
    held = list(f.get("held_involved") or [])
    removals = list(f.get("removals") or [])
    pct = f.get("phased_pct")
    nvidia = "nvidia" in name
    if f.get("dpkg_hold"):
        reason = (f"You put {name} on hold (apt-mark hold), so apt, PackageKit and GNOME "
                  "Software leave it at the installed version.")
        if new:
            reason += f" Updating it would also install {_names(new)}."
        if held:
            reason += f" It also needs held {_names(held)}."
        if nvidia:
            reason += " " + NVIDIA_NOTE
        unhold = " ".join([name, *held])
        return {"blocker": "held", "title": TITLES["held"], "reason": reason,
                "command": f"sudo apt-mark unhold {unhold} && sudo apt full-upgrade",
                "fixable": False}
    if held:
        reason = (f"It needs a newer {_names(held)}, which you hold (apt-mark hold); apt "
                  "will not change a held package.")
        if any("nvidia" in h for h in held) or nvidia:
            reason += " " + NVIDIA_NOTE
        return {"blocker": "held-dependency", "title": TITLES["held-dependency"],
                "reason": reason,
                "command": f"sudo apt-mark unhold {' '.join(held)} && sudo apt full-upgrade",
                "fixable": False}
    if removals:
        return {"blocker": "conflict", "title": TITLES["conflict"],
                "reason": f"Installing it would remove {_names(removals)}. TUF Control never "
                          "removes packages; review this in a terminal.",
                "command": "sudo apt full-upgrade   # read the list of removals before "
                           "answering", "fixable": False}
    if pct is not None and int(pct) < 100:
        return {"blocker": "phased", "title": TITLES["phased"],
                "reason": f"Ubuntu is releasing this version gradually ({int(pct)} % of "
                          "machines so far). It installs by itself when this laptop's turn "
                          "comes; nothing is wrong.",
                "command": "sudo apt -o APT::Get::Always-Include-Phased-Updates=true "
                           "full-upgrade", "fixable": False}
    if f.get("broken"):
        return {"blocker": "conflict", "title": TITLES["conflict"],
                "reason": "apt cannot find a consistent way to install it (a dependency "
                          "conflict).",
                "command": "sudo apt full-upgrade", "fixable": False}
    if new:
        return {"blocker": "needs-new", "title": TITLES["needs-new"],
                "reason": f"It needs packages that are not installed yet: {_names(new)}. "
                          "An upgrade without new dependencies (apt-get upgrade or PackageKit's Update) only upgrades "
                          "installed packages, so it stays behind until those are installed.",
                "command": "sudo apt full-upgrade", "fixable": True}
    return {"blocker": "unknown", "title": TITLES["unknown"],
            "reason": "apt keeps it back without a reason it reports here.",
            "command": "sudo apt full-upgrade", "fixable": False}


def parse_dpkg_holds(status_text: str) -> set[str]:
    """Package names whose dpkg selection is hold, from /var/lib/dpkg/status."""
    held, name = set(), None
    for line in status_text.splitlines():
        if line.startswith("Package: "):
            name = line[9:].strip()
        elif line.startswith("Status: ") and name and line.split()[1] == "hold":
            held.add(name)
    return held
