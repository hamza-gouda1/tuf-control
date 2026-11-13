# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""A fixed, believable update set for the fake hardware mode (screenshots, tests, demos).
Used whenever ASUSIUS_FAKE_ROOT is set; the real sources are never touched then."""
from __future__ import annotations

import os

from . import blockers
from .model import SourceResult, UpdateItem

FAKE_UPDATES_ENV = "ASUSIUS_FAKE_UPDATES"  # "none": an up-to-date machine
FAKE_PENDING_ENV = "ASUSIUS_FAKE_PENDING"  # item ids that do NOT install (comma list)
FAKE_SKIP_ENV = "ASUSIUS_FAKE_SKIP"        # set by a cancelled fake run (same format)


def _blocked(name: str, cur: str, new: str, facts: dict) -> dict:
    """Blocker facts like python-apt's simulation would report them."""
    return {"kept_back": True, **facts, "dpkg_hold": bool(facts.get("dpkg_hold")),
            "phased_pct": facts.get("phased_pct")}


def fake_sources(now: float) -> list[SourceResult]:
    if os.environ.get(FAKE_UPDATES_ENV) == "none":
        return [SourceResult("apt", info={"lists_refreshed_ts": now - 3 * 3600}),
                SourceResult("snap"), SourceResult("flatpak"),
                SourceResult("firmware", info={"metadata_ts": now - 86400}),
                SourceResult("system")]
    apt = SourceResult("apt", info={"lists_refreshed_ts": now - 5 * 3600,
                                    "method": "python-apt"})

    def pkg(name, cur, new, size, desc, sec=False, kind="package", reboot=False,
            facts=None):
        extra: dict = {"arch": "amd64", "origin": "Ubuntu/noble-" +
                       ("security" if sec else "updates")}
        if facts is not None:
            facts = _blocked(name, cur, new, facts)
            extra["facts"] = facts
            extra["held"] = True
            extra["blocker"] = blockers.classify(name, facts)
        apt.items.append(UpdateItem(id=f"apt:{name}:amd64", source="apt", name=name,
                                    current=cur, available=new, size=size, security=sec,
                                    description=desc, needs_reboot=reboot,
                                    apply_method="packagekit", kind=kind, extra=extra))
    # a common real-world case: a new kernel ABI leaves the meta packages held back on a
    # plain upgrade (needs-new, fixable in the app); the NVIDIA libraries are on a dpkg
    # hold (listed, never applied)
    kernel_new = ["linux-image-6.8.0-147-generic", "linux-modules-6.8.0-147-generic",
                  "linux-modules-nvidia-580-6.8.0-147-generic"]
    hdr_new = ["linux-headers-6.8.0-147-generic", "linux-headers-6.8.0-147-generic-common"]
    pkg("linux-image-generic", "6.8.0-146.146", "6.8.0-147.147", 10_820,
        "Generic Linux kernel image", True, "kernel", True,
        facts={"new_packages": kernel_new, "upgrades": [], "removals": [],
               "held_involved": [], "phased_pct": None, "dpkg_hold": False})
    pkg("linux-headers-generic", "6.8.0-146.146", "6.8.0-147.147", 10_784,
        "Generic Linux kernel headers", True, "kernel", True,
        facts={"new_packages": hdr_new, "upgrades": [], "removals": [],
               "held_involved": [], "phased_pct": None, "dpkg_hold": False})
    pkg("openssl", "3.0.13-0ubuntu3.6", "3.0.13-0ubuntu3.7", 1_003_000,
        "Secure Sockets Layer toolkit - cryptographic utility", True)
    pkg("libnvidia-gl-580", "580.178.04-0ubuntu1", "580.182.01-0ubuntu1", 182_000_000,
        "NVIDIA OpenGL/GLX/EGL/GLES GLVND libraries and Vulkan ICD", False, "driver", True,
        facts={"new_packages": [], "upgrades": [], "removals": [], "held_involved": [],
               "phased_pct": None, "dpkg_hold": True})
    pkg("containerd", "2.2.1-0ubuntu1~24.04.3", "2.2.1-0ubuntu1~24.04.4", 34_100_000,
        "daemon to control runC")
    pkg("firefox", "1:1snap1-0ubuntu5", "1:1snap1-0ubuntu6", 79_000,
        "Transitional package - firefox -> firefox snap")
    snap = SourceResult("snap", info={"method": "snapd REST"}, items=[
        UpdateItem(id="snap:core22", source="snap", name="core22", current="20260801",
                   available="20260909", size=77_600_000, apply_method="snapd",
                   kind="runtime", description="Runtime environment based on Ubuntu 22.04",
                   extra={"revision": "2987", "channel": "latest/stable"})])
    flat = SourceResult("flatpak", items=[
        UpdateItem(id="flatpak:system:io.missioncenter.MissionCenter/x86_64/stable",
                   source="flatpak", name="io.missioncenter.MissionCenter", current="1.1.0",
                   available="1.2.0", size=4_200_000, apply_method="flatpak-system",
                   kind="app", description="flathub · stable",
                   extra={"installation": "system", "ref":
                          "io.missioncenter.MissionCenter/x86_64/stable"})])
    fw = SourceResult("firmware", info={"metadata_ts": now - 2 * 86400, "devices": 31,
                                        "updatable": 12}, items=[
        UpdateItem(id="firmware:3b27a1b5c0de", source="firmware", name="System Firmware",
                   current="309", available="310", size=13_900_000, security=True,
                   description="ASUS FX608JHR BIOS 310: security fixes and improved "
                               "stability",
                   needs_reboot=True, apply_method="fwupd", kind="bios",
                   extra={"device_id": "3b27a1b5c0de" + "0" * 28, "vendor": "ASUSTeK",
                          "require_ac": True, "urgency": "high",
                          "issues": ["CVE-2026-1234"],
                          "details": "• Updates the Intel microcode\n• Fixes CVE-2026-1234"})])
    system = SourceResult("system", info={"running_kernel": "6.8.0-146-generic"},
                          items=[UpdateItem(
                              id="system:kernel", source="system", name="Linux kernel",
                              current="6.8.0-146-generic",
                              available="6.8.0-147-generic", needs_reboot=True,
                              apply_method="reboot", kind="kernel",
                              description="A newer kernel is installed; it is used after a "
                                          "restart.")])
    return [apt, snap, flat, fw, system,
            SourceResult("repos", available=False, reason='off ("check_repos" in the '
                                                          'updates settings)')]


def _ids(env: str) -> set[str]:
    return {x.strip() for x in os.environ.get(env, "").split(",") if x.strip()}


def fake_installed(now: float | None = None) -> dict[str, str]:
    """Installed versions in fake mode: everything lands on its offered version except
    the ids in ASUSIUS_FAKE_PENDING (the "still pending" case) and those a cancelled
    fake run never installed (ASUSIUS_FAKE_SKIP, set by actions)."""
    import time as _time
    pending = _ids(FAKE_PENDING_ENV) | _ids(FAKE_SKIP_ENV)
    out = {}
    for s in fake_sources(now if now is not None else _time.time()):
        for i in s.items:
            if i.apply_method == "reboot":
                continue
            out[i.id] = i.current if i.id in pending else (i.available or i.current)
    return out
