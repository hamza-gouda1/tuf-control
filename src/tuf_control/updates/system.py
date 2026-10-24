"""Things that are installed but not in use until a restart: a newer kernel of the running
flavour, /run/reboot-required, and an NVIDIA kernel module that no longer matches the
installed driver. All local file reads; nothing needs root."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .model import SourceResult, UpdateItem

_KVER = re.compile(r"^(\d+(?:\.\d+)+)-(\d+(?:\.\d+)*)(?:-(.+))?$")


def parse_kernel(release: str) -> tuple[tuple[int, ...], str] | None:
    """'7.0.13-3-liquorix-amd64' -> ((7, 0, 13, 3), 'liquorix-amd64');
    '6.8.0-147-generic' -> ((6, 8, 0, 147), 'generic')."""
    m = _KVER.match(release.strip())
    if not m:
        return None
    nums = tuple(int(x) for x in (m[1] + "." + m[2]).split("."))
    return nums, m[3] or ""


def installed_kernels(root: Path = Path("/")) -> list[str]:
    try:
        with os.scandir(root / "boot") as it:
            return sorted(e.name[len("vmlinuz-"):] for e in it
                          if e.name.startswith("vmlinuz-") and not e.name.endswith(".old"))
    except OSError:
        return []


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
