# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Read-only system package update detection for non-APT distributions (dnf, pacman, zypper)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import SourceResult, UpdateItem

TIMEOUT_S = 60
_REBOOT_PFX = ("kernel", "linux", "systemd", "glibc", "dbus", "udev", "nvidia")


def detect_manager(root: Path = Path("/")) -> str | None:
    """Return the system package manager identifier ('apt', 'pacman', 'dnf', 'zypper') or None."""
    if (root / "var/lib/dpkg/status").exists():
        return "apt"
    at_root = root == Path("/")
    if (root / "var/lib/pacman").is_dir() or (at_root and shutil.which("pacman")):
        return "pacman"
    if (
        (root / "var/lib/dnf").is_dir()
        or (root / "etc/dnf").is_dir()
        or (root / "etc/fedora-release").exists()
        or (at_root and (shutil.which("dnf5") or shutil.which("dnf")))
    ):
        return "dnf"
    if (
        (root / "var/lib/zypp").is_dir()
        or (root / "etc/zypp").is_dir()
        or (at_root and shutil.which("zypper"))
    ):
        return "zypper"
    return None


def _classify(name: str) -> tuple[str, bool]:
    low = name.lower()
    if low == "kernel" or low.startswith(("kernel-", "linux", "vmlinuz")):
        if not low.startswith("linux-firmware"):
            return "kernel", True
    if "nvidia" in low:
        return "driver", True
    if any(low == p or low.startswith(p + "-") for p in _REBOOT_PFX):
        return "system", True
    return "package", False


def _run(cmd: list[str], timeout: float = TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)


def _err_line(r: subprocess.CompletedProcess[str], fallback: str) -> str:
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[-1] if lines else fallback


def parse_pacman(text: str) -> list[UpdateItem]:
    """Parse `checkupdates` / `pacman -Qu` lines: 'pkg oldver -> newver'."""
    out: list[UpdateItem] = []
    for line in text.splitlines():
        m = re.match(r"^(\S+)\s+(\S+)\s+->\s+(\S+)", line.strip())
        if not m:
            continue
        name, cur, new = m.groups()
        kind, reboot = _classify(name)
        out.append(UpdateItem(
            id=f"pacman:{name}", source="pacman", name=name, current=cur, available=new,
            needs_reboot=reboot, apply_method="none", kind=kind,
            description="Run sudo pacman -Syu to update safely."))
    return sorted(out, key=lambda i: i.name)


def _rpm_installed() -> dict[tuple[str, str], str]:
    if not shutil.which("rpm"):
        return {}
    try:
        r = _run(["rpm", "-qa", "--qf", "%{NAME}\t%{ARCH}\t%{EVR}\n"], timeout=20)
        if r.returncode != 0:
            return {}
        out: dict[tuple[str, str], str] = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[(parts[0], parts[1])] = parts[2]
        return out
    except (OSError, subprocess.SubprocessError):
        return {}


def parse_dnf(text: str, installed: dict[tuple[str, str], str] | None = None) -> list[UpdateItem]:
    """Parse `dnf check-update -q` / `dnf5 check-upgrade -q`: 'name.arch  ver  repo'."""
    inst = installed or {}
    out: list[UpdateItem] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("obsoleting packages", "security:")):
            break
        parts = line.split()
        if len(parts) < 3 or "." not in parts[0]:
            continue
        name, arch = parts[0].rsplit(".", 1)
        new, repo = parts[1], parts[2]
        kind, reboot = _classify(name)
        sec = "security" in repo.lower()
        out.append(UpdateItem(
            id=f"dnf:{name}:{arch}", source="dnf", name=name,
            current=inst.get((name, arch)), available=new, security=sec,
            needs_reboot=reboot, apply_method="packagekit", kind=kind,
            extra={"arch": arch, "origin": repo}))
    return sorted(out, key=lambda i: i.name)


def parse_zypper(text: str) -> list[UpdateItem]:
    """Parse `zypper --xmlout list-updates` (or pipe table fallback)."""
    out: list[UpdateItem] = []
    stripped = text.lstrip()
    if stripped.startswith("<"):
        try:
            root = ET.fromstring(stripped)
            for node in root.findall(".//update"):
                name = (node.attrib.get("name") or "").strip()
                if not name:
                    continue
                arch = (node.attrib.get("arch") or "").strip()
                cur = (node.attrib.get("edition-old") or "").strip() or None
                new = (node.attrib.get("edition") or "").strip() or None
                cat = (node.attrib.get("category") or "").lower()
                kind, reboot = _classify(name)
                uid = f"zypper:{name}:{arch}" if arch else f"zypper:{name}"
                out.append(UpdateItem(
                    id=uid, source="zypper", name=name, current=cur, available=new,
                    security=(cat == "security"), needs_reboot=reboot,
                    apply_method="packagekit", kind=kind,
                    extra={k: v for k, v in (("arch", arch), ("category", cat)) if v}))
            return sorted(out, key=lambda i: i.name)
        except ET.ParseError:
            pass
    for raw in text.splitlines():
        if "|" not in raw:
            continue
        cols = [c.strip() for c in raw.split("|")]
        if len(cols) < 6 or cols[0].lower() in ("s", "status") or set(cols[0]) <= {"-"}:
            continue
        name, cur, new, arch = cols[2], cols[3] or None, cols[4] or None, cols[5]
        if not name or name.lower() == "name":
            continue
        kind, reboot = _classify(name)
        uid = f"zypper:{name}:{arch}" if arch else f"zypper:{name}"
        out.append(UpdateItem(
            id=uid, source="zypper", name=name, current=cur, available=new,
            needs_reboot=reboot, apply_method="packagekit", kind=kind,
            extra={"arch": arch} if arch else {}))
    return sorted(out, key=lambda i: i.name)


def _check_pacman() -> SourceResult:
    cu = shutil.which("checkupdates")
    if cu:
        res = SourceResult("pacman", info={"method": "checkupdates"})
        r = _run([cu])
        if r.returncode == 2:
            return res
        if r.returncode != 0:
            res.error = _err_line(r, f"checkupdates exited {r.returncode}")
            return res
        res.items = parse_pacman(r.stdout)
        return res
    pm = shutil.which("pacman")
    if not pm:
        return SourceResult("pacman", available=False, reason="pacman is not installed")
    res = SourceResult("pacman", info={"method": "pacman -Qu"})
    r = _run([pm, "-Qu"])
    if r.returncode == 1 and not r.stdout.strip() and not r.stderr.strip():
        return res
    if r.returncode not in (0, 1):
        res.error = _err_line(r, f"pacman -Qu exited {r.returncode}")
        return res
    res.items = parse_pacman(r.stdout)
    return res


def _check_dnf() -> SourceResult:
    cmd = shutil.which("dnf5") or shutil.which("dnf")
    if not cmd:
        return SourceResult("dnf", available=False, reason="dnf is not installed")
    sub = "check-upgrade" if os.path.basename(cmd) == "dnf5" else "check-update"
    res = SourceResult("dnf", info={"method": f"{os.path.basename(cmd)} {sub}"})
    r = _run([cmd, sub, "-q"])
    if r.returncode == 0:
        return res
    if r.returncode != 100:
        res.error = _err_line(r, f"{os.path.basename(cmd)} exited {r.returncode}")
        return res
    res.items = parse_dnf(r.stdout, _rpm_installed())
    return res


def _check_zypper() -> SourceResult:
    zp = shutil.which("zypper")
    if not zp:
        return SourceResult("zypper", available=False, reason="zypper is not installed")
    res = SourceResult("zypper", info={"method": "zypper list-updates"})
    r = _run([zp, "--non-interactive", "--no-refresh", "--xmlout", "list-updates"])
    if r.returncode != 0:
        res.error = _err_line(r, f"zypper exited {r.returncode}")
        return res
    res.items = parse_zypper(r.stdout)
    return res


def check(manager: str) -> SourceResult:
    try:
        if manager == "pacman":
            return _check_pacman()
        if manager == "dnf":
            return _check_dnf()
        if manager == "zypper":
            return _check_zypper()
    except (OSError, subprocess.SubprocessError) as exc:
        return SourceResult(manager, error=str(exc))
    return SourceResult(manager, available=False, reason=f"unknown package manager {manager}")
