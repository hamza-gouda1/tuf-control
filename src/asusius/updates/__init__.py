# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Everything on this laptop that needs an update: system packages (apt), snaps, flatpaks,
firmware (fwupd: BIOS, EC, devices), a newer kernel or NVIDIA driver waiting for a restart,
and optionally the owner's own git checkouts.

Each source is a module with `check() -> SourceResult` of normalised `UpdateItem`s; nothing
in a check needs root or changes the system. `check.run_check()` runs them all and caches
the `Report` in ~/.cache/asusius/updates.json; `scheduler.UpdateScheduler` does that in
asusiusd at most every 6 hours and notifies once per new update; `actions` refreshes and
applies through PackageKit, fwupd, snapd and flatpak, which ask polkit themselves.
"""
from .model import Report, SourceResult, UpdateItem, age_text, human_size  # noqa: F401
