"""Refresh and apply updates through each system's own service; never sudo or pkexec.

    apt       PackageKit (org.freedesktop.PackageKit): RefreshCache, GetUpdates, UpdatePackages
    firmware  `fwupdmgr refresh` for metadata; fwupd Install(device, cabinet) to apply
    snap      snapd POST /v2/snaps {"action": "refresh"} with X-Allow-Interaction
    flatpak   `flatpak update --user` (no prompt) / `--system` (flatpak's system helper)
    restart   org.gnome.SessionManager.Reboot (GNOME's own end-session dialog)

Each service decides whether to ask polkit, which shows the desktop's authentication
dialog. The UI puts every call behind a confirmation. With TUF_CONTROL_FAKE_ROOT set,
nothing real is called: the request is appended to fake-cache/actions.log.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .model import UpdateItem

PK = "org.freedesktop.PackageKit"
PK_PATH = "/org/freedesktop/PackageKit"
PK_TX = "org.freedesktop.PackageKit.Transaction"
PK_FILTER_NONE = 1 << 1             # pk_bitfield_value(PK_FILTER_ENUM_NONE)
PK_TX_FLAG_ONLY_TRUSTED = 1 << 1    # pk_bitfield_value(PK_TRANSACTION_FLAG_ENUM_ONLY_TRUSTED)
PK_EXIT_SUCCESS = 1
PK_INFO_SECURITY = 8
FIRMWARE_HOSTS = ("fwupd.org", "cdn.fwupd.org")
FIRMWARE_WARNING = (
    "Firmware updates write to the laptop's own chips. Keep the charger connected and the "
    "battery above 30 %, do not close the lid, suspend or switch off until it says it is "
    "done; a BIOS update finishes during the next restart and the screen may stay dark "
    "for a few minutes. An interrupted update can leave the device unusable.")

Progress = Callable[[str], None]


class ActionError(RuntimeError):
    pass


def _fake() -> bool:
    from ..controller import FAKE_ROOT_ENV
    return bool(os.environ.get(FAKE_ROOT_ENV))


def _record(what: str) -> str:
    from .check import cache_dir
    p = cache_dir() / "actions.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(what + "\n")
    return f"{what} (fake: nothing changed)"


# -- PackageKit ----------------------------------------------------------------------------
class PkTransaction:
    """One PackageKit transaction, run to completion in the calling (worker) thread."""

    def __init__(self, progress: Progress | None = None, timeout_s: float = 3600) -> None:
        from gi.repository import Gio, GLib

        from .. import dbusutil
        self.progress, self.timeout_s = progress, timeout_s
        self.ctx = GLib.MainContext.new()
        self.ctx.push_thread_default()
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            (self.path,) = dbusutil.call("system", PK, PK_PATH, PK, "CreateTransaction", None,
                                         "(o)", 10000, conn=self.conn)
        finally:
            self.ctx.pop_thread_default()
        self.packages: list[tuple[int, str, str]] = []
        self.error: str | None = None
        self.exit: int | None = None

    def _signal(self, _c, _s, _p, _i, name, params):
        v = params.unpack()
        if name == "Package":
            self.packages.append(v)
        elif name == "ErrorCode":
            self.error = v[1]
        elif name == "Finished":
            self.exit = v[0]
        elif name == "ItemProgress" and self.progress:
            self.progress(f"{v[0].split(';')[0]} {v[2]}%")

    def run(self, method: str, args, sig: str | None = None) -> list[tuple[int, str, str]]:
        from gi.repository import Gio, GLib

        from .. import dbusutil
        self.ctx.push_thread_default()
        try:
            sub = self.conn.signal_subscribe(PK, PK_TX, None, self.path, None,
                                             Gio.DBusSignalFlags.NONE, self._signal)
            try:
                dbusutil.call("system", PK, self.path, PK_TX, "SetHints",
                              GLib.Variant("(as)", (["interactive=true"],)), None, 10000,
                              conn=self.conn)
                dbusutil.call("system", PK, self.path, PK_TX, method, args, sig, 30000,
                              interactive=True, conn=self.conn)
                deadline = time.monotonic() + self.timeout_s
                while self.exit is None and time.monotonic() < deadline:
                    self.ctx.iteration(False) or time.sleep(0.05)
            finally:
                self.conn.signal_unsubscribe(sub)
        finally:
            self.ctx.pop_thread_default()
        if self.exit is None:
            raise ActionError(f"PackageKit {method} timed out")
        if self.exit != PK_EXIT_SUCCESS:
            raise ActionError(self.error or f"PackageKit {method} failed (exit {self.exit})")
        return self.packages


def pk_refresh(progress: Progress | None = None) -> str:
    from gi.repository import GLib
    PkTransaction(progress, 900).run("RefreshCache", GLib.Variant("(b)", (False,)))
    return "package lists refreshed"


def pk_update(names: list[str] | None, progress: Progress | None = None) -> str:
    """Update the named packages (all offered updates when None) via PackageKit."""
    from gi.repository import GLib
    offered = PkTransaction(progress, 300).run("GetUpdates",
                                               GLib.Variant("(t)", (PK_FILTER_NONE,)))
    ids = [pid for _info, pid, _s in offered
           if names is None or pid.split(";")[0] in set(names)]
    if not ids:
        return "nothing to update"
    PkTransaction(progress).run("UpdatePackages",
                                GLib.Variant("(tas)", (PK_TX_FLAG_ONLY_TRUSTED, ids)))
    return f"updated {len(ids)} package{'s' if len(ids) != 1 else ''}"


# -- fwupd ------------------------------------------------------------------------------------
def _checksum_ok(path: Path, checksums: list[str]) -> bool:
    if not checksums:
        return False
    data = path.read_bytes()
    have = {hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest()}
    return any(c.lower() in have for c in checksums)


def firmware_url_ok(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    return u.scheme == "https" and (u.hostname or "") in FIRMWARE_HOSTS


def fwupd_install(item: UpdateItem, progress: Progress | None = None) -> str:
    """Download the release's cabinet from the LVFS (https, checksum from the metadata)
    and hand it to fwupd, which verifies its signature and asks polkit."""
    from gi.repository import Gio, GLib

    from .. import dbusutil
    url = next((u for u in item.extra.get("locations") or [] if firmware_url_ok(u)), None)
    if not url:
        raise ActionError("no LVFS download location for this release; use GNOME Software "
                          "or `fwupdmgr update`")
    did = item.extra.get("device_id")
    with tempfile.TemporaryDirectory(prefix="tuf-fw-") as tmp:
        cab = Path(tmp) / "firmware.cab"
        if progress:
            progress("downloading firmware")
        with urllib.request.urlopen(url, timeout=60) as resp, open(cab, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 16)
        if not _checksum_ok(cab, item.extra.get("checksums") or []):
            raise ActionError("downloaded firmware does not match the LVFS checksum")
        fd = os.open(cab, os.O_RDONLY)
        try:
            fds = Gio.UnixFDList.new_from_array([fd])
            if progress:
                progress("installing (do not interrupt)")
            dbusutil.call("system", "org.freedesktop.fwupd", "/", "org.freedesktop.fwupd",
                          "Install", GLib.Variant("(sha{sv})", (did, 0, {})), None, 3600_000,
                          interactive=True, fd_list=fds)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
    return "firmware installed" + (" — restart to finish" if item.needs_reboot else "")


def fwupd_refresh() -> str:
    r = subprocess.run(["fwupdmgr", "refresh", "--force"], capture_output=True, text=True,
                       timeout=180, stdin=subprocess.DEVNULL)
    if r.returncode not in (0, 2):  # 2: nothing to do / already fresh
        raise ActionError((r.stderr or r.stdout).strip().splitlines()[-1:] or
                          ["fwupdmgr refresh failed"])
    return "firmware metadata refreshed"


# -- flatpak / snap / session ------------------------------------------------------------------
def flatpak_update(installation: str, refs: list[str] | None) -> str:
    from .flatpak import update_cmd
    r = subprocess.run(update_cmd(installation, refs), capture_output=True, text=True,
                       timeout=3600, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ActionError((r.stderr or r.stdout).strip().splitlines()[-1:] or
                          ["flatpak update failed"])
    return f"flatpak ({installation}) updated"


def flatpak_refresh() -> str:
    for inst in ("system", "user"):  # without --cached: downloads the remotes' summaries
        subprocess.run(["flatpak", "remote-ls", f"--{inst}", "--updates",
                        "--columns=application"], capture_output=True, timeout=120,
                       stdin=subprocess.DEVNULL)
    return "flatpak remotes refreshed"


def request_reboot() -> str:
    """GNOME's own restart dialog (with its countdown and Cancel)."""
    from .. import dbusutil
    dbusutil.call("session", "org.gnome.SessionManager", "/org/gnome/SessionManager",
                  "org.gnome.SessionManager", "Reboot", None, None, 10000)
    return "restart requested"


SOFTWARE_DESKTOP = "org.gnome.Software.desktop"


def software_available() -> bool:
    return bool(shutil.which("gnome-software"))


def open_software() -> str:
    if _fake():
        return _record("open gnome-software --mode=updates")
    if not software_available():
        raise ActionError("GNOME Software is not installed")
    subprocess.Popen(["gnome-software", "--mode=updates"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return "opened Software"


# -- entry points ----------------------------------------------------------------------------------
REFRESHABLE = ("apt", "firmware", "flatpak")


def refresh_sources(sources: list[str], progress: Progress | None = None) -> dict[str, str]:
    """Renew the metadata each source checks against. Errors are per source."""
    out = {}
    for s in sources:
        try:
            if _fake():
                out[s] = _record(f"refresh {s}")
            elif s == "apt":
                out[s] = pk_refresh(progress)
            elif s == "firmware" and shutil.which("fwupdmgr"):
                out[s] = fwupd_refresh()
            elif s == "flatpak" and shutil.which("flatpak"):
                out[s] = flatpak_refresh()
        except Exception as exc:
            from .. import dbusutil
            out[s] = f"error: {dbusutil.error_text(exc)}"
    return out


def plan(items: list[UpdateItem]) -> dict[str, list[UpdateItem]]:
    """Group items by how they are applied; reboot hints and git items are not applied."""
    out: dict[str, list[UpdateItem]] = {}
    for i in items:
        if i.apply_method in ("reboot", "git", "none"):
            continue
        out.setdefault(i.apply_method, []).append(i)
    return out


def describe(items: list[UpdateItem]) -> str:
    """Confirmation text listing what will happen."""
    lines = []
    for method, its in plan(items).items():
        names = ", ".join(i.name for i in its[:6]) + (f" and {len(its) - 6} more"
                                                       if len(its) > 6 else "")
        lines.append({"packagekit": "System packages", "snapd": "Snaps",
                      "flatpak-user": "Flatpak (yours)", "flatpak-system": "Flatpak (system)",
                      "fwupd": "Firmware"}.get(method, method) + f": {names}")
    return "\n".join(lines)


def apply(items: list[UpdateItem], progress: Progress | None = None,
          cancel: threading.Event | None = None) -> dict[str, str]:
    """Apply the given items, one system service at a time. Returns method -> outcome."""
    out = {}
    for method, its in plan(items).items():
        if cancel is not None and cancel.is_set():
            break
        try:
            if _fake():
                out[method] = _record(f"apply {method}: " + " ".join(i.id for i in its))
            elif method == "packagekit":
                out[method] = pk_update([i.name for i in its], progress)
            elif method == "snapd":
                from .snap import refresh
                out[method] = refresh([i.name for i in its], progress=progress)
            elif method.startswith("flatpak-"):
                out[method] = flatpak_update(method.split("-", 1)[1],
                                             [i.extra.get("ref") or i.name for i in its])
            elif method == "fwupd":
                out[method] = "; ".join(fwupd_install(i, progress) for i in its)
        except Exception as exc:
            from .. import dbusutil
            out[method] = f"error: {dbusutil.error_text(exc)}"
    return out


def restart() -> str:
    if _fake():
        return _record("restart")
    return request_reboot()
