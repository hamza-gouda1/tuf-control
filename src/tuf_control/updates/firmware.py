"""Firmware (BIOS/UEFI capsules, EC, Thunderbolt, webcams, SSDs...) through fwupd's D-Bus API.

Reads only: GetDevices, GetUpgrades(device) for updatable devices, GetRemotes for the age of
the LVFS metadata. Nothing here installs. GetUpgrades works from the downloaded metadata
(no network); `fwupdmgr refresh` renews it.
"""
from __future__ import annotations

from .model import SourceResult, UpdateItem

BUS = "org.freedesktop.fwupd"
PATH = "/"
IFACE = "org.freedesktop.fwupd"
FLAG_INTERNAL = 1 << 0
FLAG_UPDATABLE = 1 << 1
FLAG_REQUIRE_AC = 1 << 3
FLAG_NEEDS_REBOOT = 1 << 8
UNSET_TIME = 2**64 - 1
URGENCY = {0: "", 1: "low", 2: "medium", 3: "high", 4: "critical"}


def _kind(dev: dict) -> str:
    name = (dev.get("Name") or "").lower()
    plugin = dev.get("Plugin") or ""
    if name == "system firmware" or "(bios)" in name:
        return "bios"
    if plugin.startswith("uefi_db") or plugin in ("uefi_dbx", "uefi_kek", "uefi_pk"):
        return "secure-boot"
    if plugin.startswith("uefi"):
        return "uefi"
    if "embedded controller" in name or plugin in ("ec", "superio"):
        return "ec"
    return "device"


def parse_release(dev: dict, rel: dict) -> UpdateItem:
    flags = int(dev.get("Flags") or 0)
    issues = list(rel.get("Issues") or [])
    urgency = URGENCY.get(int(rel.get("Urgency") or 0), "")
    locs = list(rel.get("Locations") or ([rel["Uri"]] if rel.get("Uri") else []))
    checks = rel.get("Checksum") or []
    if isinstance(checks, str):
        checks = [checks]
    desc = (rel.get("Summary") or "").strip()
    did = dev.get("DeviceId") or ""
    return UpdateItem(
        id=f"firmware:{did[:12]}", source="firmware",
        name=dev.get("Name") or "Device", current=dev.get("Version"),
        available=rel.get("Version"), size=int(rel.get("Size") or 0) or None,
        security=bool(issues) or urgency in ("high", "critical"), description=desc,
        needs_reboot=bool(flags & FLAG_NEEDS_REBOOT), apply_method="fwupd", kind=_kind(dev),
        extra={"device_id": did, "vendor": dev.get("Vendor"), "plugin": dev.get("Plugin"),
               "require_ac": bool(flags & FLAG_REQUIRE_AC), "urgency": urgency,
               "issues": issues[:10], "locations": locs[:3], "checksums": list(checks)[:4],
               "remote": rel.get("RemoteId"),
               "details": _strip_markup(rel.get("Description") or "")[:1200]})


def _strip_markup(text: str) -> str:
    import re
    t = re.sub(r"<li>", "• ", text)
    t = re.sub(r"</(p|li)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def parse(devices: list[dict], upgrades: dict[str, list[dict]],
          remotes: list[dict] | None = None) -> tuple[list[UpdateItem], dict]:
    """devices: GetDevices; upgrades: device id -> GetUpgrades list (newest first)."""
    items = []
    for dev in devices:
        rels = upgrades.get(dev.get("DeviceId") or "")
        if rels:
            items.append(parse_release(dev, rels[0]))
    info: dict = {"devices": len(devices),
                  "updatable": sum(1 for d in devices if int(d.get("Flags") or 0)
                                   & FLAG_UPDATABLE)}
    newest = None
    for r in remotes or []:
        if not r.get("Enabled"):
            continue
        t = r.get("ModificationTime") or r.get("Mtime")
        if t and int(t) != UNSET_TIME and int(r.get("Type") or 1) == 1:  # download remotes
            newest = max(newest or 0, int(t))
    info["metadata_ts"] = newest
    return sorted(items, key=lambda i: i.name), info


class FwupdClient:
    """Thin read-only client; `call` can be replaced in tests."""

    def __init__(self, call=None) -> None:
        if call is None:
            from .. import dbusutil

            def call(method, args=None, reply="(aa{sv})"):
                return dbusutil.call("system", BUS, PATH, IFACE, method, args, reply, 15000)
        self.call = call

    def devices(self) -> list[dict]:
        return list(self.call("GetDevices")[0])

    def remotes(self) -> list[dict]:
        return list(self.call("GetRemotes")[0])

    def upgrades(self, device_id: str) -> list[dict]:
        from gi.repository import GLib

        from .. import dbusutil
        try:
            return list(self.call("GetUpgrades", GLib.Variant("(s)", (device_id,)))[0])
        except GLib.Error as exc:
            name = dbusutil.error_name(exc)
            if name.endswith((".NothingToDo", ".NotSupported", ".NotFound", ".Internal")):
                return []
            raise


def available() -> tuple[bool, str]:
    try:
        from .. import dbusutil
        if not dbusutil.has_owner("system", BUS):
            return False, "fwupd is not installed"
    except Exception as exc:
        return False, f"no system bus ({exc})"
    return True, ""


def check(client: FwupdClient | None = None) -> SourceResult:
    if client is None:
        ok, why = available()
        if not ok:
            return SourceResult("firmware", available=False, reason=why)
    res = SourceResult("firmware")
    try:
        client = client or FwupdClient()
        devices = client.devices()
        ups = {}
        for d in devices:
            if int(d.get("Flags") or 0) & FLAG_UPDATABLE:
                ups[d["DeviceId"]] = client.upgrades(d["DeviceId"])
        try:
            remotes = client.remotes()
        except Exception:
            remotes = []
        res.items, res.info = parse(devices, ups, remotes)
        res.info["method"] = "fwupd D-Bus"
    except Exception as exc:
        from .. import dbusutil
        res.error = f"fwupd: {dbusutil.error_text(exc)}"
    return res
