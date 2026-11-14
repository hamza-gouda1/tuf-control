# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Things that are installed but not in use until a restart: a newer kernel of the running
flavour, /run/reboot-required, and an NVIDIA kernel module that no longer matches the
installed driver. All local file reads; nothing needs root."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .model import SourceResult, UpdateItem

_VER_HEAD = re.compile(r"^(\d+(?:\.\d+)+)(?:-(.+))?$")
_FEDORA_TAIL = re.compile(r"^(\d+(?:\.\d+)*)\.(fc\d+|el\d+(?:_\d+)?)\.(.+)$")
_NUM_TAIL = re.compile(r"^(\d+(?:\.\d+)*)(?:-(.+))?$")
_KOTD_TAIL = re.compile(r"^(\d+(?:\.\d+)*)\.[A-Za-z0-9_.]+-(.+)$")
_NAMED_TAIL = re.compile(r"^([A-Za-z]+)(\d+(?:\.\d+)*)(?:-(\d+))?(?:-(.+))?$")
_UKI_RE = re.compile(r"(\d+\.\d+\.\d+(?:-[A-Za-z0-9_+.-]+)?)$")


def parse_kernel(release: str) -> tuple[tuple[int, ...], str] | None:
    """Parse Linux uname/kernel versions across Ubuntu/Debian, Fedora/EL, Arch, openSUSE."""
    s = release.strip()
    m = _VER_HEAD.match(s)
    if not m:
        return None
    base = tuple(int(x) for x in m[1].split("."))
    if not base or base[0] <= 0:
        return None
    rest = m[2]
    if not rest:
        return base, ""
    if fm := _FEDORA_TAIL.match(rest):
        rel = tuple(int(x) for x in fm[1].split("."))
        tag = tuple(int(x) for x in re.findall(r"\d+", fm[2]))
        return base + rel + tag, fm[3]
    if nm := _NUM_TAIL.match(rest):
        rel = tuple(int(x) for x in nm[1].split("."))
        return base + rel, nm[2] or ""
    if km := _KOTD_TAIL.match(rest):
        rel = tuple(int(x) for x in km[1].split("."))
        return base + rel, km[2]
    if am := _NAMED_TAIL.match(rest):
        prefix, num_s, pkgrel, suffix = am.groups()
        rel = tuple(int(x) for x in num_s.split(".")) + ((int(pkgrel),) if pkgrel else ())
        flavour = suffix or prefix
        return base + rel, flavour
    return base, rest


def _bzimage_version(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            hdr = f.read(65536)
        if len(hdr) < 0x220 or hdr[0x202:0x206] != b"HdrS":
            return None
        off = int.from_bytes(hdr[0x20E:0x210], "little") + 0x200
        if not 0x200 < off < len(hdr):
            return None
        raw = hdr[off:off + 128].split(b"\x00", 1)[0].decode("ascii", "ignore")
        tok = raw.strip().split()[0] if raw.strip() else ""
        return tok if tok and parse_kernel(tok) is not None else None
    except OSError:
        return None


def installed_kernels(root: Path = Path("/")) -> list[str]:
    found: set[str] = set()
    unversioned_vmlinuz = False
    try:
        with os.scandir(root / "boot") as it:
            for e in it:
                if not e.name.startswith("vmlinuz-") or e.name.endswith((".old", ".kdump")):
                    continue
                suffix = e.name[len("vmlinuz-"):]
                if suffix.startswith("0-rescue"):
                    continue
                if parse_kernel(suffix) is not None:
                    found.add(suffix)
                else:
                    unversioned_vmlinuz = True
                    ver = _bzimage_version(Path(e.path))
                    if ver:
                        found.add(ver)
    except OSError:
        pass
    for rel_dir in ("boot/loader/entries", "efi/loader/entries", "boot/efi/loader/entries"):
        try:
            with os.scandir(root / rel_dir) as it:
                for e in it:
                    if not e.name.endswith(".conf"):
                        continue
                    try:
                        text = Path(e.path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for line in text.splitlines():
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2 and parts[0] == "version":
                            if parse_kernel(parts[1]) is not None:
                                found.add(parts[1].strip())
        except OSError:
            pass
    for rel_dir in ("boot/efi/EFI/Linux", "efi/EFI/Linux", "boot/EFI/Linux"):
        try:
            with os.scandir(root / rel_dir) as it:
                for e in it:
                    if not e.name.endswith(".efi"):
                        continue
                    stem = e.name[:-4]
                    if stem.endswith("-signed"):
                        stem = stem[:-7]
                    um = _UKI_RE.search(stem)
                    if um and parse_kernel(um.group(1)) is not None:
                        found.add(um.group(1))
        except OSError:
            pass
    for rel_dir in ("usr/lib/modules", "lib/modules"):
        mod_root = root / rel_dir
        try:
            with os.scandir(mod_root) as it:
                for e in it:
                    if not e.is_dir() or parse_kernel(e.name) is None:
                        continue
                    p = Path(e.path)
                    if (p / "vmlinuz").exists() or (
                        unversioned_vmlinuz
                        and ((p / "pkgbase").exists() or (p / "modules.dep").exists())
                    ):
                        found.add(e.name)
        except OSError:
            pass
    return sorted(found)


def kernel_item(running: str, installed: list[str]) -> UpdateItem | None:
    cur = parse_kernel(running)
    if cur is None:
        return None
    same = [(parse_kernel(k), k) for k in installed]
    same = [(p[0], k) for p, k in same if p is not None and p[1] == cur[1]]
    if not same:
        return None
    best, name = max(same)
    if best <= cur[0]:
        return None
    return UpdateItem(id="system:kernel", source="system", name="Linux kernel",
                      current=running, available=name, needs_reboot=True,
                      apply_method="reboot", kind="kernel",
                      description=f"A newer {cur[1] or 'kernel'} kernel is installed; it "
                                  "is used after a restart.")


def reboot_required(root: Path = Path("/")) -> tuple[bool, list[str]]:
    flag = root / "run/reboot-required"
    if not flag.exists():
        return False, []
    try:
        pkgs = (root / "run/reboot-required.pkgs").read_text().split()
    except OSError:
        pkgs = []
    return True, sorted(set(pkgs))


def nvidia_versions(root: Path = Path("/"), modinfo=None) -> dict:
    """{'loaded': ..., 'module': ..., 'userspace': ...}; missing values are None."""
    out = {"loaded": None, "module": None, "userspace": None}
    try:
        out["loaded"] = (root / "sys/module/nvidia/version").read_text().strip() or None
    except OSError:
        try:
            m = re.search(r"Kernel Module(?: for \S+)?\s+(\d+\.\d+(?:\.\d+)?)",
                          (root / "proc/driver/nvidia/version").read_text())
            out["loaded"] = m.group(1) if m else None
        except OSError:
            pass
    if out["loaded"] is None:
        return out  # no NVIDIA driver loaded: nothing to compare
    if modinfo is None:
        def modinfo():
            r = subprocess.run(["modinfo", "-F", "version", "nvidia"], capture_output=True,
                               text=True, timeout=10, stdin=subprocess.DEVNULL)
            return r.stdout.strip() if r.returncode == 0 else None
    try:
        out["module"] = modinfo() or None
    except (OSError, subprocess.SubprocessError):
        pass
    for d in ("usr/lib/x86_64-linux-gnu", "usr/lib64", "usr/lib"):
        try:
            with os.scandir(root / d) as it:
                for e in it:
                    m = re.fullmatch(r"libnvidia-ml\.so\.(\d+\.\d+(?:\.\d+)?)", e.name)
                    if m:
                        out["userspace"] = m.group(1)
                        break
        except OSError:
            continue
        if out["userspace"]:
            break
    return out


def nvidia_item(v: dict) -> UpdateItem | None:
    loaded = v.get("loaded")
    if not loaded:
        return None
    newer = [x for x in (v.get("module"), v.get("userspace")) if x and x != loaded]
    if not newer:
        return None
    return UpdateItem(id="system:nvidia", source="system", name="NVIDIA driver",
                      current=loaded, available=newer[0], needs_reboot=True,
                      apply_method="reboot", kind="driver",
                      description="The loaded NVIDIA kernel module is older than the "
                                  "installed driver; restart to load the new one (until then "
                                  "CUDA and nvidia-smi may fail with a version mismatch).",
                      extra=dict(v))


def check(root: Path = Path("/"), running: str | None = None, modinfo=None) -> SourceResult:
    res = SourceResult("system")
    running = running or os.uname().release
    k = kernel_item(running, installed_kernels(root))
    if k:
        res.items.append(k)
    need, pkgs = reboot_required(root)
    if need:
        res.items.append(UpdateItem(
            id="system:reboot-required", source="system", name="Restart required",
            needs_reboot=True, apply_method="reboot", kind="reboot",
            description="Installed updates ask for a restart" +
                        (f": {', '.join(pkgs[:8])}" if pkgs else "."),
            extra={"packages": pkgs}))
    nv = nvidia_item(nvidia_versions(root, modinfo))
    if nv:
        res.items.append(nv)
    res.info["running_kernel"] = running
    return res
