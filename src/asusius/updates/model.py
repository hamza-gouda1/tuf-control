# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Normalised update items, per-source results and the report the UI, CLI and daemon share."""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field

# display order and titles of the sources
SOURCE_TITLES = {
    "apt": "System packages",
    "snap": "Snaps",
    "flatpak": "Flatpak apps",
    "firmware": "Firmware",
    "system": "Restart needed",
    "repos": "Asusius & Bordeaux GNOME",
}
SOURCE_ORDER = list(SOURCE_TITLES)
# how an item is applied; the UI maps these to buttons
APPLY_METHODS = ("packagekit", "snapd", "flatpak-user", "flatpak-system", "fwupd", "reboot",
                 "git", "software", "none")


@dataclass
class UpdateItem:
    id: str                      # unique within the report: "<source>:<name>[:<arch/branch>]"
    source: str
    name: str
    current: str | None = None
    available: str | None = None
    size: int | None = None      # bytes to download, when known
    security: bool = False
    description: str = ""
    needs_reboot: bool = False
    apply_method: str = "none"
    kind: str = ""               # finer tag: kernel, driver, app, runtime, bios, device...
    extra: dict = field(default_factory=dict)  # source-specific (device id, origin...)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> UpdateItem:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    @property
    def blocker(self) -> dict | None:
        return self.extra.get("blocker") or None

    @property
    def waiting(self) -> bool:
        """Held by the owner or in a phased rollout: shown, not counted, never applied."""
        from .blockers import WAITING
        return bool(self.blocker and self.blocker.get("blocker") in WAITING)

    @property
    def counts(self) -> bool:
        """Pure hints (reboot pending) and waiting items are shown but are not 'updates'."""
        return self.apply_method != "reboot" and not self.waiting


@dataclass
class SourceResult:
    source: str
    available: bool = True       # the source exists on this machine
    reason: str = ""             # why not available, or why the check failed
    error: str = ""              # the check ran but failed (offline, timeout...)
    items: list[UpdateItem] = field(default_factory=list)
    info: dict = field(default_factory=dict)  # e.g. lists_refreshed_ts, metadata_ts
    duration_s: float = 0.0

    @property
    def title(self) -> str:
        return SOURCE_TITLES.get(self.source, self.source)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [i.to_dict() for i in self.items]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SourceResult:
        r = cls(source=d["source"], available=d.get("available", True),
                reason=d.get("reason", ""), error=d.get("error", ""),
                info=dict(d.get("info") or {}), duration_s=d.get("duration_s", 0.0))
        r.items = [UpdateItem.from_dict(i) for i in d.get("items") or []]
        return r


@dataclass
class Report:
    checked_ts: float = 0.0
    sources: list[SourceResult] = field(default_factory=list)
    reboot_required: bool = False
    reboot_reasons: list[str] = field(default_factory=list)
    fake: bool = False

    # -- views -----------------------------------------------------------------------
    def items(self) -> list[UpdateItem]:
        return [i for s in self.sources for i in s.items]

    def updates(self) -> list[UpdateItem]:
        return [i for i in self.items() if i.counts]

    def waiting(self) -> list[UpdateItem]:
        return [i for i in self.items() if i.waiting]

    def by_source(self) -> dict[str, list[UpdateItem]]:
        out: dict[str, list[UpdateItem]] = {}
        for i in self.updates():
            out.setdefault(i.source, []).append(i)
        return out

    def source(self, name: str) -> SourceResult | None:
        return next((s for s in self.sources if s.source == name), None)

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in sorted(self.by_source().items(),
                                              key=lambda kv: SOURCE_ORDER.index(kv[0])
                                              if kv[0] in SOURCE_ORDER else 99)}

    @property
    def total(self) -> int:
        return len(self.updates())

    @property
    def security_count(self) -> int:
        return sum(1 for i in self.updates() if i.security)

    def summary(self) -> str:
        """'14 updates available (apt 12, firmware 1, snap 1)' or 'Everything is up to date'."""
        n = self.total
        held = len(self.waiting())
        tail = f" · {held} held back" if held else ""
        if not n:
            return "Everything is up to date" + tail
        parts = ", ".join(f"{k} {v}" for k, v in self.counts().items())
        return f"{n} update{'s' if n != 1 else ''} available ({parts})" + tail

    def signature(self) -> str:
        """Stable digest of what is offered: (id, available version) pairs."""
        h = hashlib.sha256()
        for key in self.keys():
            h.update(key.encode() + b"\0")
        return h.hexdigest()[:16]

    def keys(self) -> list[str]:
        return sorted(f"{i.id}={i.available or ''}" for i in self.updates())

    # -- (de)serialisation -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"checked_ts": self.checked_ts, "reboot_required": self.reboot_required,
                "reboot_reasons": self.reboot_reasons, "fake": self.fake,
                "summary": self.summary(), "total": self.total,
                "security": self.security_count, "counts": self.counts(),
                "waiting": len(self.waiting()),
                "signature": self.signature(),
                "sources": [s.to_dict() for s in self.sources]}

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        return cls(checked_ts=float(d.get("checked_ts") or 0.0),
                   sources=[SourceResult.from_dict(s) for s in d.get("sources") or []],
                   reboot_required=bool(d.get("reboot_required")),
                   reboot_reasons=list(d.get("reboot_reasons") or []),
                   fake=bool(d.get("fake")))


def age_text(ts: float | None, now: float | None = None) -> str:
    """'just now', '5 minutes ago', '3 hours ago', '2 days ago'; 'never' without a time."""
    if not ts:
        return "never"
    s = max(0.0, (now if now is not None else time.time()) - ts)
    if s < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = int(s // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"  # pragma: no cover


def human_size(n: int | None) -> str:
    if not n:
        return ""
    v = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if v < 1000 or unit == "GB":
            return f"{v:.0f} {unit}" if unit in ("B", "kB") else f"{v:.1f} {unit}"
        v /= 1000
    return ""  # pragma: no cover
