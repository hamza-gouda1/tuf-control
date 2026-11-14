# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Refresh and apply updates through each system's own service; never sudo or pkexec.

    apt       PackageKit (org.freedesktop.PackageKit): RefreshCache, GetUpdates, UpdatePackages
    firmware  `fwupdmgr refresh` for metadata; fwupd Install(device, cabinet) to apply
    snap      snapd POST /v2/snaps {"action": "refresh"} with X-Allow-Interaction
    flatpak   `flatpak update --user` (no prompt) / `--system` (flatpak's system helper)
    restart   org.gnome.SessionManager.Reboot (GNOME's own end-session dialog)

Each service decides whether to ask polkit, which shows the desktop's authentication
dialog. The UI puts every call behind a confirmation. With ASUSIUS_FAKE_ROOT set,
nothing real is called: the request is appended to fake-cache/actions.log.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import ipaddress
import os
import re
import selectors
import signal
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable

from .model import UpdateItem

PK = "org.freedesktop.PackageKit"
PK_PATH = "/org/freedesktop/PackageKit"
PK_TX = "org.freedesktop.PackageKit.Transaction"
PK_FILTER_NONE = 1 << 1             # pk_bitfield_value(PK_FILTER_ENUM_NONE)
PK_FILTER_NOT_INSTALLED = 1 << 3    # pk_bitfield_value(PK_FILTER_ENUM_NOT_INSTALLED)
PK_TX_FLAG_SIMULATE = 1 << 2
PK_TX_FLAG_ONLY_TRUSTED = 1 << 1    # pk_bitfield_value(PK_TRANSACTION_FLAG_ENUM_ONLY_TRUSTED)
PK_EXIT_SUCCESS = 1
PK_EXIT_CANCELLED = 3
PK_INFO_SECURITY = 8
# PackageKit Status enum -> the phase shown; values verified against PackageKitGlib
PK_STATUS = {7: "Refreshing", 8: "Downloading", 13: "Resolving dependencies", 14: "Checking",
             9: "Installing", 10: "Installing", 11: "Cleaning up", 15: "Preparing",
             16: "Committing", 20: "Downloading", 21: "Downloading", 22: "Downloading",
             23: "Downloading", 24: "Downloading", 25: "Downloading", 27: "Reading cache",
             30: "Waiting", 31: "Waiting for password", 36: "Running hooks"}
FIRMWARE_HOSTS = ("fwupd.org", "cdn.fwupd.org")
MAX_FIRMWARE_BYTES = 256 << 20
LVFS_CONNECT_TIMEOUT_S = 60
LVFS_TRANSFER_TIMEOUT_S = 180
FIRMWARE_WARNING = (
    "Firmware updates write to the laptop's own chips. Keep the charger connected and the "
    "battery above 30 %, do not close the lid, suspend or switch off until it says it is "
    "done; a BIOS update finishes during the next restart and the screen may stay dark "
    "for a few minutes. An interrupted update can leave the device unusable.")

Progress = Callable[[str], None]
# per-source live handles while a run goes (see progress.SourceWatch); keys are methods
Watch = dict


METHOD_TITLES = {"packagekit": "System packages", "snapd": "Snaps",
                 "flatpak-user": "Flatpak (yours)", "flatpak-system": "Flatpak (system)",
                 "fwupd": "Firmware", "packagekit-deps": "System packages"}


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
    """One PackageKit transaction, run to completion in the calling (worker) thread.

    Transaction signals become live state on the watch (phase, percentage, the current
    package, speed and time remaining) and plain log lines; `cancel` is honoured while
    PackageKit reports AllowCancel."""

    def __init__(self, progress: Progress | None = None, timeout_s: float = 3600,
                 watch: object | None = None, cancel: threading.Event | None = None) -> None:
        from gi.repository import Gio, GLib

        from .. import dbusutil
        self.progress, self.timeout_s = progress, timeout_s
        self.watch, self.cancel = watch, cancel
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
        self._percent: float | None = None
        self._speed: float | None = None
        self._remaining_bytes: int | None = None
        self._allow_cancel = False
        self._cancel_sent = False

    def _report(self) -> None:
        """Push the accumulated state (percent, speed, remaining) to the watch."""
        if self.watch is None:
            return
        self.watch.phase(percent=self._percent, speed=self._speed,
                         remaining_s=(self._remaining_bytes / self._speed
                                      if self._remaining_bytes is not None and self._speed
                                      else None), cancellable=self._allow_cancel)

    def _properties_changed(self, _c, _s, _p, _i, _name, params):
        iface, changed, _invalidated = params.unpack()
        if iface != PK_TX:
            return
        for name, value in changed.items():
            value = value.unpack() if hasattr(value, "unpack") else value
            if name == "Status":
                phase = PK_STATUS.get(int(value), "Working")
                if self.watch:
                    self.watch.phase(phase, auth=int(value) == 31)
                    self.watch.line(phase.lower())
            elif name == "Percentage":
                self._percent = float(value) if 0 <= int(value) <= 100 else None
            elif name == "Speed":
                self._speed = float(value) or None
            elif name == "DownloadSizeRemaining":
                self._remaining_bytes = int(value)
            elif name == "AllowCancel":
                self._allow_cancel = bool(value)
        self._report()

    def _signal(self, _c, _s, _p, _i, name, params):
        v = params.unpack()
        if name == "Package":
            self.packages.append(v)
            if self.watch and len(v) > 1:
                self.watch.phase(package=v[1].split(";")[0])
        elif name == "ErrorCode":
            self.error = v[1]
        elif name == "Finished":
            self.exit = v[0]
        elif name == "Status":
            phase = PK_STATUS.get(int(v[0]))
            if phase and self.watch:
                self.watch.phase(phase, auth=int(v[0]) == 31)
                self.watch.line(phase.lower())
        elif name == "Percentage":
            p = int(v[0])
            self._percent = None if p > 100 else float(p)
            self._report()
        elif name == "ItemProgress":
            pid, _status, percentage = v[0], v[1], v[2]
            if self.progress:
                self.progress(f"{pid.split(';')[0]} {percentage}%")
            if self.watch and pid:
                self.watch.phase(package=pid.split(";")[0])
        elif name == "Speed":
            self._speed = float(v[0]) or None
            self._report()
        elif name == "DownloadSizeRemaining":
            self._remaining_bytes = int(v[0])
            self._report()
        elif name == "AllowCancel":
            self._allow_cancel = bool(v[0])
            self._report()

    def run(self, method: str, args, sig: str | None = None) -> list[tuple[int, str, str]]:
        from gi.repository import Gio, GLib

        from .. import dbusutil
        _check_cancelled(self.cancel)
        self.ctx.push_thread_default()
        try:
            sub = self.conn.signal_subscribe(PK, PK_TX, None, self.path, None,
                                             Gio.DBusSignalFlags.NONE, self._signal)
            props_sub = self.conn.signal_subscribe(
                PK, "org.freedesktop.DBus.Properties", "PropertiesChanged", self.path,
                PK_TX, Gio.DBusSignalFlags.NONE, self._properties_changed)
            try:
                dbusutil.call("system", PK, self.path, PK_TX, "SetHints",
                              GLib.Variant("(as)", (["interactive=true"],)), None, 10000,
                              conn=self.conn)
                _check_cancelled(self.cancel)
                dbusutil.call("system", PK, self.path, PK_TX, method, args, sig, 30000,
                              interactive=True, conn=self.conn)
                deadline = time.monotonic() + self.timeout_s
                while self.exit is None and time.monotonic() < deadline:
                    if (self.cancel is not None and self.cancel.is_set()
                            and self._allow_cancel and not self._cancel_sent):
                        with contextlib.suppress(Exception):
                            dbusutil.call("system", PK, self.path, PK_TX, "Cancel", None,
                                          None, 10000, conn=self.conn)
                        self._cancel_sent = True
                    self.ctx.iteration(False) or time.sleep(0.05)
            finally:
                self.conn.signal_unsubscribe(sub)
                self.conn.signal_unsubscribe(props_sub)
                if self.exit is None and self._allow_cancel:
                    with contextlib.suppress(Exception):
                        dbusutil.call("system", PK, self.path, PK_TX, "Cancel", None,
                                      None, 10000, conn=self.conn)
        finally:
            self.ctx.pop_thread_default()
        if self.exit is None:
            raise ActionError(f"PackageKit {method} timed out")
        if self.exit == PK_EXIT_CANCELLED:
            raise _Cancelled(f"PackageKit {method} was cancelled")
        if self.exit != PK_EXIT_SUCCESS:
            raise ActionError(self.error or f"PackageKit {method} failed (exit {self.exit})")
        return self.packages


class _Cancelled(ActionError):
    """The owner cancelled; not an error to toast in red, but the run's honest outcome."""


def pk_refresh(progress: Progress | None = None, watch: object | None = None) -> str:
    from gi.repository import GLib
    PkTransaction(progress, 900, watch=watch).run("RefreshCache",
                                                  GLib.Variant("(b)", (False,)))
    return "package lists refreshed"


def _split_pkg_arch(spec: str) -> tuple[str, str | None]:
    if ":" in spec:
        name, arch = spec.split(":", 1)
        return name, arch or None
    return spec, None


def _pid_matches_specs(pid: str, specs: set[tuple[str, str | None]]) -> bool:
    parts = pid.split(";")
    pname, narch = _split_pkg_arch(parts[0])
    parch = narch or (parts[2] if len(parts) > 2 and parts[2] else None)
    return any(
        pname == req_name and (req_arch is None or parch is None or parch == req_arch)
        for req_name, req_arch in specs
    )


def pk_update(names: list[str] | None, progress: Progress | None = None,
              watch: object | None = None, cancel: threading.Event | None = None) -> str:
    """Update the named packages (all offered updates when None) via PackageKit."""
    from gi.repository import GLib
    from .apt import dpkg_holds
    held = dpkg_holds()
    wanted = {_split_pkg_arch(n) for n in names} if names is not None else None
    if wanted and held.intersection(base for base, _ in wanted):
        raise ActionError("A selected package is now held by you. Refresh the Updates page")
    offered = PkTransaction(progress, 300, watch=watch).run(
        "GetUpdates", GLib.Variant("(t)", (PK_FILTER_NONE,)))
    ids = [pid for _info, pid, _s in offered
           if pid.split(";")[0].split(":")[0] not in held
           and (wanted is None or _pid_matches_specs(pid, wanted))]
    if not ids:
        return "nothing to update"
    _check_cancelled(cancel)
    PkTransaction(progress, watch=watch, cancel=cancel).run(
        "UpdatePackages", GLib.Variant("(tas)", (PK_TX_FLAG_ONLY_TRUSTED, ids)))
    return f"updated {len(ids)} package{'s' if len(ids) != 1 else ''}"


def pk_install(names: list[str], progress: Progress | None = None,
               watch: object | None = None, cancel: threading.Event | None = None,
               expected_upgrades: list[str] | None = None) -> str:
    """Install the named packages at their candidate version (PackageKit InstallPackages):
    this is what a kept-back meta package needs — the new packages it depends on."""
    from gi.repository import GLib
    from .apt import dpkg_holds
    wanted = {_split_pkg_arch(n) for n in names}
    exp_specs = {_split_pkg_arch(n) for n in (expected_upgrades or [])}
    if dpkg_holds().intersection({b for b, _ in wanted | exp_specs}):
        raise ActionError("A dependency is now held by you. Nothing was installed")
    resolve_names = list(dict.fromkeys(_split_pkg_arch(n)[0] for n in names))
    resolved = PkTransaction(progress, 120, watch=watch).run(
        "Resolve", GLib.Variant("(tas)", (PK_FILTER_NOT_INSTALLED, resolve_names)))
    ids = sorted({pid for _info, pid, _s in resolved if _pid_matches_specs(pid, wanted)})
    if not all(any(_pid_matches_specs(pid, {spec}) for pid in ids) for spec in wanted):
        raise ActionError("PackageKit could not resolve every confirmed package; refresh "
                          "the Updates page before trying again")
    simulated = PkTransaction(progress, 120, watch=watch).run(
        "InstallPackages", GLib.Variant("(tas)", (
            PK_TX_FLAG_ONLY_TRUSTED | PK_TX_FLAG_SIMULATE, ids)))
    # PackageKit's InfoEnum INSTALLING/UPDATING, never removal/downgrade/obsoletion.
    allowed = wanted | exp_specs
    if not simulated:
        raise ActionError("PackageKit did not provide a dependency simulation; nothing was installed")
    for info, pid, _summary in simulated:
        if info not in (12, 11) or not _pid_matches_specs(pid, allowed):
            raise ActionError("PackageKit's plan differs from the confirmed package list; "
                              "refresh the Updates page. Nothing was installed")
    if cancel is not None and cancel.is_set():
        raise _Cancelled("Dependency installation was cancelled")
    _check_cancelled(cancel)
    PkTransaction(progress, watch=watch, cancel=cancel).run(
        "InstallPackages", GLib.Variant("(tas)", (PK_TX_FLAG_ONLY_TRUSTED, ids)))
    return f"installed {len(ids)} package{'s' if len(ids) != 1 else ''} with dependencies"


def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Cancelled("cancelled by you before installation started")


# -- fwupd ------------------------------------------------------------------------------------
def _checksum_ok(data: bytes, checksums: list[str]) -> bool:
    """SHA-256 only: every current LVFS release carries one, and fwupd verifies the
    cabinet's signature on top when it is installed."""
    if not checksums:
        return False
    have = hashlib.sha256(data).hexdigest()
    return any(c.lower() == have for c in checksums)


def firmware_url_ok(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    return (u.scheme == "https" and (u.hostname or "") in FIRMWARE_HOSTS
            and not u.username and not u.password and u.port is None and not u.fragment)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect only to vetted sockaddr values, while verifying the original hostname.

    http.client does not consult proxy environment variables. Bypassing its ordinary
    connect() also avoids a second DNS lookup between validation and the TLS handshake.
    """

    def __init__(self, host: str, addresses: list, *, timeout: float = 60,
                 connect_deadline: float | None = None) -> None:
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.addresses = addresses
        self.connect_deadline = (connect_deadline if connect_deadline is not None
                                 else time.monotonic() + timeout)

    def connect(self) -> None:
        last_error = None
        for family, socktype, proto, _canonname, address in self.addresses:
            raw = None
            try:
                remaining = self.connect_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("LVFS connection deadline exceeded")
                raw = socket.socket(family, socktype, proto)
                raw.settimeout(min(self.timeout, remaining))
                raw.connect(address)
                remaining = self.connect_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("LVFS connection deadline exceeded before TLS")
                raw.settimeout(min(self.timeout, remaining))
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as exc:
                if raw is not None:
                    raw.close()
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ActionError("the LVFS hostname has no usable public address")


class _DeadlineReader(io.RawIOBase):
    """Bound every raw TLS read, including HTTP headers and chunk framing."""

    def __init__(self, stream, transport, deadline: float) -> None:
        super().__init__()
        self.stream, self.transport, self.deadline = stream, transport, deadline

    def readable(self) -> bool:
        return True

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ActionError("LVFS download exceeded the 180 second time limit")
        return remaining

    def readinto(self, buffer) -> int | None:
        self.transport.settimeout(min(LVFS_CONNECT_TIMEOUT_S, self._remaining()))
        try:
            size = self.stream.readinto(buffer)
        except TimeoutError:
            self._remaining()  # report the total deadline when it caused the timeout
            raise
        self._remaining()
        return size

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            super().close()


class _DeadlineResponseSocket:
    """Adapt only HTTPResponse's file reads; connection ownership stays unchanged."""

    def __init__(self, transport, deadline: float) -> None:
        self.transport, self.deadline = transport, deadline

    def makefile(self, mode: str):
        stream = self.transport.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(stream, self.transport, self.deadline))


def _fetch_lvfs(url: str, watch: object | None = None) -> bytes:
    """Download an LVFS file and return it. The URL is validated here, next to the request:
    https, only the two LVFS hostnames, no credentials, port or fragment, the resolved
    addresses must be public (no loopback, private or link-local range). The TLS socket
    connects directly to a vetted address with hostname/SNI verification; proxies and
    redirects are refused. Integrity is checked by the caller against the signed
    metadata checksum."""
    u = urllib.parse.urlparse(url)
    if (u.scheme != "https" or (u.hostname or "") not in FIRMWARE_HOSTS or u.username
            or u.password or u.port is not None or u.fragment):
        raise ActionError("not a valid LVFS download location")
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM,
                                   proto=socket.IPPROTO_TCP)
    if not addresses:
        raise ActionError("the LVFS hostname has no usable public address")
    for family, _socktype, _proto, _canonname, address in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6) or not ipaddress.ip_address(
                address[0]).is_global:
            raise ActionError("the LVFS hostname resolves to a non-public address")
    t0 = time.monotonic()
    transfer_deadline = t0 + LVFS_TRANSFER_TIMEOUT_S
    conn = _PinnedHTTPSConnection(u.hostname, addresses, timeout=LVFS_CONNECT_TIMEOUT_S,
                                  connect_deadline=t0 + LVFS_CONNECT_TIMEOUT_S)
    conn.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
        _DeadlineResponseSocket(sock, transfer_deadline), **kwargs)
    target = urllib.parse.urlunparse(("", "", u.path or "/", u.params, u.query, ""))
    buf = bytearray()
    try:
        conn.request("GET", target, headers={"User-Agent": "asusius"})
        # Keep our own reference: HTTPResponse may clear conn.sock on close.
        transport = conn.sock
        with conn.getresponse() as resp:
            if 300 <= resp.status < 400:
                raise ActionError("the LVFS tried to redirect the download; refused")
            if resp.status != 200:
                raise ActionError(f"LVFS download failed (HTTP {resp.status})")
            total = int(resp.headers.get("Content-Length") or 0)
            if total > MAX_FIRMWARE_BYTES:
                raise ActionError("Firmware cabinet exceeds the 256 MiB download limit")
            while True:
                remaining = transfer_deadline - time.monotonic()
                if remaining <= 0:
                    raise ActionError("LVFS download exceeded the 180 second time limit")
                if transport is not None:
                    transport.settimeout(min(LVFS_CONNECT_TIMEOUT_S, remaining))
                try:
                    chunk = resp.read1(1 << 16)
                except TimeoutError as exc:
                    if time.monotonic() >= transfer_deadline:
                        raise ActionError(
                            "LVFS download exceeded the 180 second time limit") from exc
                    raise
                if time.monotonic() >= transfer_deadline:
                    raise ActionError("LVFS download exceeded the 180 second time limit")
                if not chunk:
                    break
                if len(buf) + len(chunk) > MAX_FIRMWARE_BYTES:
                    raise ActionError("Firmware cabinet exceeds the 256 MiB download limit")
                buf += chunk
                if watch and total:
                    elapsed = max(0.001, time.monotonic() - t0)
                    watch.phase("Downloading", percent=100.0 * len(buf) / total,
                                speed=len(buf) / elapsed,
                                remaining_s=(total - len(buf)) * elapsed / len(buf))
    finally:
        conn.close()
    return bytes(buf)


def fwupd_install(item: UpdateItem, progress: Progress | None = None,
                  watch: object | None = None,
                  cancel: threading.Event | None = None) -> str:
    """Download the release's cabinet from the LVFS (https, checksum from the metadata)
    and hand it to fwupd, which verifies its signature and asks polkit."""
    from gi.repository import Gio, GLib

    from .. import dbusutil
    _check_cancelled(cancel)
    url = next((u for u in item.extra.get("locations") or [] if firmware_url_ok(u)), None)
    if not url:
        raise ActionError("no LVFS download location for this release; use GNOME Software "
                          "or `fwupdmgr update`")
    did = item.extra.get("device_id")
    if progress:
        progress("downloading firmware")
    data = _fetch_lvfs(url, watch)
    _check_cancelled(cancel)
    if not _checksum_ok(data, item.extra.get("checksums") or []):
        raise ActionError("downloaded firmware does not match the LVFS checksum")
    if watch:
        watch.line(f"downloaded {len(data)} bytes; the checksum matches")
        watch.phase("Installing", percent=None, cancellable=False)
    fd, cab = tempfile.mkstemp(prefix="asusius-fw-", suffix=".cab")
    try:
        os.write(fd, data)
        os.fsync(fd)
        fds = Gio.UnixFDList.new_from_array([fd])
        if progress:
            progress("installing (do not interrupt)")
        _check_cancelled(cancel)
        dbusutil.call("system", "org.freedesktop.fwupd", "/", "org.freedesktop.fwupd",
                      "Install", GLib.Variant("(sha{sv})", (did, 0, {})), None, 3600_000,
                      interactive=True, fd_list=fds)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(cab)
    return "firmware installed" + (" — restart to finish" if item.needs_reboot else "")


def fwupd_refresh() -> str:
    r = subprocess.run(["fwupdmgr", "refresh", "--force"], capture_output=True, text=True,
                       timeout=180, stdin=subprocess.DEVNULL)
    if r.returncode not in (0, 2):  # 2: nothing to do / already fresh
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise ActionError(lines[-1] if lines else "fwupdmgr refresh failed")
    return "firmware metadata refreshed"


# -- flatpak / snap / session ------------------------------------------------------------------
def flatpak_update(installation: str, refs: list[str] | None, progress=None,
                   watch=None, cancel=None) -> str:
    """Stream Flatpak's output on the worker thread; cancellation targets this child only."""
    from .flatpak import update_cmd
    cmd = update_cmd(installation, refs)
    if cancel is not None and cancel.is_set():
        raise _Cancelled("Flatpak update was cancelled")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            env={**os.environ, "LC_ALL": "C.UTF-8"})
    selector = selectors.DefaultSelector()
    pending = b""
    last_line = ""
    cancelled_at = None
    deadline = time.monotonic() + 3600
    try:
        selector.register(proc.stdout, selectors.EVENT_READ)
        if watch:
            watch.phase("Installing", cancellable=True)
        eof = False
        while not eof or proc.poll() is None:
            now = time.monotonic()
            if cancel is not None and cancel.is_set() and cancelled_at is None:
                cancelled_at = now
                proc.send_signal(signal.SIGINT)
                if watch:
                    watch.phase("Cancelling", cancellable=False)
            if cancelled_at is not None and now - cancelled_at > 5 and proc.poll() is None:
                proc.kill()
            if now > deadline:
                raise ActionError("Flatpak update timed out")
            for key, _events in selector.select(0.2):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    eof = True
                    selector.unregister(key.fileobj)
                    continue
                pending += data
                lines = re.split(rb"[\r\n]", pending)
                pending = lines.pop()[-8192:]
                for raw in lines:
                    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "",
                                  raw.decode(errors="replace")).strip()[:2000]
                    if not line:
                        continue
                    last_line = line
                    if progress:
                        progress(line)
                    if watch:
                        watch.line(line)
                        match = re.search(r"(?<!\d)(\d{1,3})%", line)
                        watch.phase(package=line[:120],
                                    percent=int(match.group(1)) if match else None)
            if eof and proc.poll() is None:
                time.sleep(0.05)
        if cancelled_at is not None:
            raise _Cancelled("Flatpak update was cancelled")
        if proc.returncode:
            if pending.strip():
                last_line = pending.decode(errors="replace")[-2000:]
            raise ActionError(last_line or "Flatpak update failed")
        return f"flatpak ({installation}) updated"
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()


def flatpak_refresh() -> str:
    for inst in ("system", "user"):  # without --cached: downloads the remotes' summaries
        r = subprocess.run(["flatpak", "remote-ls", f"--{inst}", "--updates",
                            "--columns=application"], capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            lines = (r.stderr or r.stdout).strip().splitlines()
            raise ActionError(lines[-1] if lines else f"flatpak remote-ls --{inst} failed")
    return "flatpak remotes refreshed"


def request_reboot() -> str:
    """GNOME's own restart dialog (with its countdown and Cancel)."""
    from .. import dbusutil
    dbusutil.call("session", "org.gnome.SessionManager", "/org/gnome/SessionManager",
                  "org.gnome.SessionManager", "Reboot", None, None, 10000)
    return "restart requested"


SOFTWARE_DESKTOP = "org.gnome.Software.desktop"


def _software_target() -> tuple[list[str], str] | None:
    if shutil.which("gnome-software"):
        return ["gnome-software", "--mode=updates"], "opened Software"
    if shutil.which("plasma-discover"):
        return ["plasma-discover", "--mode", "Update"], "opened Discover"
    if shutil.which("xdg-open"):
        return ["xdg-open", "https://fwupd.org/"], "opened LVFS in browser"
    return None


def software_available() -> bool:
    return _software_target() is not None


def open_software() -> str:
    if _fake():
        return _record("open software updates")
    target = _software_target()
    if target is None:
        raise ActionError("No software center (GNOME Software, KDE Discover) or xdg-open found")
    cmd, label = target
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return label


# -- entry points ----------------------------------------------------------------------------------
REFRESHABLE = ("apt", "firmware", "flatpak")


def refresh_sources(sources: list[str], progress: Progress | None = None) -> dict[str, str]:
    """Renew the metadata each source checks against. Errors are per source."""
    out = {}
    for s in sources:
        try:
            if _fake():
                out[s] = _record(f"refresh {s}")
            elif s in ("apt", "dnf", "zypper"):
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
        if i.waiting or i.apply_method in ("reboot", "git", "none"):
            continue
        out.setdefault(i.apply_method, []).append(i)
    return out


def describe(items: list[UpdateItem]) -> str:
    """Confirmation text listing what will happen."""
    lines = []
    for method, its in plan(items).items():
        names = ", ".join(i.name for i in its[:6]) + (f" and {len(its) - 6} more"
                                                       if len(its) > 6 else "")
        lines.append(METHOD_TITLES.get(method, method) + f": {names}")
    return "\n".join(lines)


def _fake_skip(ids: list[str]) -> None:
    """Fake mode: remember what a cancelled run did not install, so the verifier and the
    screenshots see honest versions."""
    from .fake import FAKE_SKIP_ENV
    have = {x.strip() for x in os.environ.get(FAKE_SKIP_ENV, "").split(",") if x.strip()}
    os.environ[FAKE_SKIP_ENV] = ",".join(sorted(have | set(ids)))


def _fake_apply(method: str, its: list[UpdateItem], watch: object | None,
                cancel: threading.Event | None) -> str:
    """Fake mode: nothing is installed, but the run looks real (phases, log, ~1 s)."""
    import random
    w = watch
    names = [i.name for i in its]
    if w:
        w.phase("Waiting", cancellable=True)
    for pct in (8, 26, 51, 78):
        if cancel is not None and cancel.is_set():
            break
        time.sleep(0.08)
        pkg = names[min(int(pct / 26), max(0, len(names) - 1))]
        if w:
            w.phase("Downloading", percent=pct, package=pkg,
                    speed=random.uniform(1.5e6, 9e6), remaining_s=14.0 - pct / 8)
            w.line(f"Get:{pct // 12} https://example.org/ubuntu noble/main amd64 {pkg} "
                   f"[{random.randint(120, 900)} kB]")
    for pct in (30, 62, 90):
        if cancel is not None and cancel.is_set():
            break
        time.sleep(0.07)
        if w:
            w.phase("Installing", percent=pct, package=names[min(int(pct / 34),
                                                                 max(0, len(names) - 1))])
            w.line(f"Unpacking {names[min(int(pct / 34), max(0, len(names) - 1))]}")
    if cancel is not None and cancel.is_set():
        _fake_skip([i.id for i in its])
        if w:
            w.line("cancelled")
            w.phase("Cancelled", done=True)
        return _record(f"apply {method}: cancelled")
    if w:
        w.phase("Cleaning up", percent=100)
        w.line(f"Setting up {', '.join(names[:3])}")
    return _record(f"apply {method}: " + " ".join(i.id for i in its))


def apply(items: list[UpdateItem], progress: Progress | None = None,
          cancel: threading.Event | None = None,
          watch: Watch | None = None) -> dict[str, str]:
    """Apply the given items, one system service at a time. Returns method -> outcome.
    `watch` maps an apply method to a live handle (progress.SourceWatch); `cancel` is a
    request each backend honours when it can."""
    out: dict[str, str] = {}
    for method, its in plan(items).items():
        if cancel is not None and cancel.is_set():
            if _fake():
                _fake_skip([i.id for i in its])
            out[method] = "error: cancelled by you"
            continue
        w = (watch or {}).get(method)
        try:
            if _fake():
                out[method] = _fake_apply(method, its, w, cancel)
            elif method == "packagekit":
                out[method] = pk_update([i.name for i in its], progress, watch=w, cancel=cancel)
            elif method == "snapd":
                from .snap import refresh
                out[method] = refresh([i.name for i in its], progress=progress, watch=w,
                                      cancel=cancel)
            elif method.startswith("flatpak-"):
                if w:
                    w.phase("Installing", package=", ".join(
                        i.extra.get("ref") or i.name for i in its)[:120])
                out[method] = flatpak_update(method.split("-", 1)[1],
                                             [i.extra.get("ref") or i.name for i in its],
                                             progress=progress, watch=w, cancel=cancel)
            elif method == "fwupd":
                out[method] = "; ".join(fwupd_install(i, progress, watch=w, cancel=cancel) for i in its)
            if w:
                w.phase("Done", percent=100, done=True, error="")
        except _Cancelled as exc:
            out[method] = f"error: {exc}"
            if w:
                w.phase("Cancelled", done=True)
        except Exception as exc:
            from .. import dbusutil
            out[method] = f"error: {dbusutil.error_text(exc)}"
            if w:
                w.phase("Failed", done=True, error=dbusutil.error_text(exc))
    return out


def deps_names(item: UpdateItem) -> list[str]:
    """The packages an "Install with dependencies" action would install: the item itself
    at its offered version plus the new packages apt's simulation found (a new kernel ABI
    that a plain upgrade never pulls in)."""
    facts = (item.blocker or {}).get("facts") or item.extra.get("facts") or {}
    return sorted({item.name, *(facts.get("new_packages") or [])})


def describe_deps(item: UpdateItem) -> str:
    facts = (item.blocker or {}).get("facts") or item.extra.get("facts") or {}
    new = facts.get("new_packages") or []
    upg = facts.get("upgrades") or []
    rem = facts.get("removals") or []
    lines = [f"{item.name} {item.current} → {item.available}"]
    if new:
        lines.append("Newly installed: " + ", ".join(new))
    if upg:
        lines.append("Also upgraded: " + ", ".join(upg))
    lines.append("Removed: none" if not rem else
                 "REMOVED (this will not run): " + ", ".join(rem))
    lines.append("Held packages are never touched; removing a hold is a terminal command.")
    return "\n".join(lines)


def _deps_plan(item: UpdateItem) -> tuple:
    facts = item.extra.get("facts") or {}
    bl = item.blocker or {}
    if (item.source != "apt" or item.apply_method != "packagekit"
            or bl.get("blocker") != "needs-new" or not bl.get("fixable")
            or facts.get("dpkg_hold") or facts.get("held_involved")
            or facts.get("removals") or facts.get("broken")
            or not facts.get("new_packages")):
        raise ActionError("This package has no safe dependency-install plan. Refresh "
                          "the Updates page and review its details")
    groups = [sorted(set(facts.get(key) or [])) for key in
              ("new_packages", "upgrades", "removals")]
    if any(not isinstance(n, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?", n)
           for names in [[item.name], *groups] for n in names):
        raise ActionError("Invalid package name in dependency-install plan")
    return item.name, item.current, item.available, *[tuple(x) for x in groups]


def apply_deps(item: UpdateItem, progress: Progress | None = None,
               cancel: threading.Event | None = None,
               watch: Watch | None = None) -> dict[str, str]:
    """Install a kept-back item with the new packages it needs (PackageKit
    InstallPackages). The outcome lands under "packagekit-deps" so the verifier can tell
    it apart from a plain UpdatePackages."""
    w = (watch or {}).get("packagekit-deps")
    item.extra["with_deps"] = True
    try:
        confirmed = _deps_plan(item)
        if cancel is not None and cancel.is_set():
            raise _Cancelled("Dependency installation was cancelled")
        if not _fake():
            from . import apt
            fresh = apt.check()
            latest = next((i for i in fresh.items if i.id == item.id), None)
            if not fresh.available or fresh.error or latest is None or _deps_plan(latest) != confirmed:
                raise ActionError("The dependency plan changed since confirmation. Refresh "
                                  "the Updates page and confirm the new list")
        if _fake():
            out = _fake_apply("packagekit-deps", [item], w, cancel)
        else:
            out = pk_install(deps_names(item), progress, watch=w, cancel=cancel,
                             expected_upgrades=(item.extra.get("facts") or {}).get("upgrades"))
    except _Cancelled as exc:
        out = f"error: {exc}"
        if w:
            w.phase("Cancelled", done=True)
    except Exception as exc:
        from .. import dbusutil
        out = f"error: {dbusutil.error_text(exc)}"
        if w:
            w.phase("Failed", done=True, error=dbusutil.error_text(exc))
    else:
        if w:
            w.phase("Done", percent=100, done=True)
    return {"packagekit-deps": out}


def restart() -> str:
    if _fake():
        return _record("restart")
    return request_reboot()
