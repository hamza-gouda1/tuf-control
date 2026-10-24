"""System packages (apt), read without root.

Preferred: python-apt (apt_pkg) over the local package cache: an installed package whose
candidate version is newer is an update, exactly like `apt list --upgradable`. Fallback:
`apt-get -s -o Debug::NoLocking=1 dist-upgrade` (a simulation, takes no lock), then
`apt list --upgradable`. Nothing here downloads package lists: the lists are as fresh as
the last `apt update` (PackageKit's RefreshCache or unattended-upgrades), and the result
says when that was.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .model import SourceResult, UpdateItem

TIMEOUT_S = 60
REBOOT_PKGS = ("libc6", "systemd", "dbus", "dbus-broker", "linux-firmware", "intel-microcode",
               "amd64-microcode", "udev")
_KERNEL = re.compile(r"^linux-(image|headers|modules|generic|signed|tools|hwe|lowlatency|"
                     r"oem|liquorix|xanmod)|liquorix|^linux$")


def classify(name: str) -> tuple[str, bool]:
    """(kind, needs_reboot) for a package name."""
    if _KERNEL.search(name):
        return "kernel", True
    if "nvidia" in name:
        return "driver", True
    if name in REBOOT_PKGS or name.startswith("libnvidia"):
        return "system", True
    return "package", False


def _item(name, arch, current, available, *, security=False, size=None, description="",
          held=False, origin="") -> UpdateItem:
    kind, reboot = classify(name)
    extra = {"arch": arch}
    if origin:
        extra["origin"] = origin
    if held:
        extra["held"] = True
    return UpdateItem(id=f"apt:{name}:{arch}", source="apt", name=name, current=current,
                      available=available, size=size, security=security,
                      description=description, needs_reboot=reboot,
                      apply_method="packagekit", kind=kind, extra=extra)


def _is_security(archives) -> bool:
    return any(a and (a.endswith("-security") or "security" in a.lower()) for a in archives)


# -- python-apt --------------------------------------------------------------------------------
def check_python_apt() -> list[UpdateItem]:
    import apt_pkg  # noqa: PLC0415 - optional dependency
    apt_pkg.init_config()
    apt_pkg.config.set("Debug::NoLocking", "1")
    apt_pkg.init_system()
    cache = apt_pkg.Cache(None)  # None: no progress output
    dep = apt_pkg.DepCache(cache)
    try:
        dep.upgrade(True)  # what a dist-upgrade would do; the rest is "held back"
    except SystemError:
        pass
    records = apt_pkg.PackageRecords(cache)
    out = []
    for pkg in cache.packages:
        cur = pkg.current_ver
        if cur is None:
            continue
        cand = dep.get_candidate_ver(pkg)
        if cand is None or apt_pkg.version_compare(cand.ver_str, cur.ver_str) <= 0:
            continue
        archives, origins = [], []
        for pf, _idx in cand.file_list:
            archives.append(pf.archive or "")
            if pf.archive and pf.archive != "now":
                origins.append(f"{pf.origin or pf.label or ''}/{pf.archive}".strip("/"))
        desc = ""
        if cand.file_list and records.lookup(cand.file_list[0]):
            desc = records.short_desc or ""
        held = not (dep.marked_upgrade(pkg) or dep.marked_install(pkg))
        out.append(_item(pkg.name, cand.arch, cur.ver_str, cand.ver_str,
                         security=_is_security(archives), size=cand.size or None,
                         description=desc, held=held, origin=", ".join(origins)))
    return sorted(out, key=lambda i: i.name)


# -- text fallbacks ------------------------------------------------------------------------------
_SIM = re.compile(r"^Inst (?P<name>\S+) (?:\[(?P<cur>[^\]]+)\] )?\((?P<new>\S+) (?P<rest>[^)]*)\)")


def parse_apt_get_sim(text: str) -> list[UpdateItem]:
    """`apt-get -s dist-upgrade` lines: 'Inst pkg [old] (new Origin:rel/suite [arch])'.
    New installs (no [old]) are kept with current=None: usually a new kernel."""
    out = []
    for line in text.splitlines():
        m = _SIM.match(line)
        if not m:
            continue
        rest = m["rest"]
        am = re.search(r"\[([^\]]+)\]\s*$", rest)
        arch = am.group(1) if am else ""
        suites = [s.strip() for s in (rest[:am.start()] if am else rest).split(",")]
        name = m["name"].split(":")[0]
        out.append(_item(name, arch, m["cur"], m["new"], security=_is_security(suites),
                         origin=", ".join(s for s in suites if s)))
    return out


_LIST = re.compile(r"^(?P<name>[^/\s]+)/(?P<suites>\S+) (?P<new>\S+) (?P<arch>\S+)"
                   r"(?: \[upgradable from: (?P<cur>[^\]]+)\])?")


def parse_apt_list(text: str) -> list[UpdateItem]:
    """`apt list --upgradable`: 'name/suite1,suite2 new arch [upgradable from: old]'."""
    out = []
    for line in text.splitlines():
        m = _LIST.match(line.strip())
        if not m or "upgradable from" not in line:
            continue
        suites = m["suites"].split(",")
        out.append(_item(m["name"], m["arch"], m["cur"], m["new"],
                         security=_is_security(suites), origin=", ".join(suites)))
    return out


def _run(cmd: list[str], timeout: float = TIMEOUT_S) -> str:
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1:] or
                           [f"{cmd[0]} exited {r.returncode}"])
    return r.stdout


def lists_refreshed_ts(root: Path = Path("/")) -> float | None:
    """When the package lists were last downloaded (apt update / RefreshCache)."""
    stamp = root / "var/lib/apt/periodic/update-success-stamp"
    best = None
    try:
        best = stamp.stat().st_mtime
    except OSError:
        pass
    lists = root / "var/lib/apt/lists"
    try:
        with os.scandir(lists) as it:
            for e in it:
                if e.name.endswith(("InRelease", "_Release")):
                    t = e.stat().st_mtime
                    best = t if best is None or t > best else best
    except OSError:
        pass
    return best


def available(root: Path = Path("/")) -> tuple[bool, str]:
    if not (root / "var/lib/dpkg/status").exists():
        return False, "not a Debian/Ubuntu system (no dpkg database)"
    return True, ""


def check(root: Path = Path("/"), *, use_python_apt: bool = True) -> SourceResult:
    ok, why = available(root)
    res = SourceResult("apt", available=ok, reason=why)
    if not ok:
        return res
    res.info["lists_refreshed_ts"] = lists_refreshed_ts(root)
    errors = []
    if use_python_apt:
        try:
            res.items = check_python_apt()
            res.info["method"] = "python-apt"
            return res
        except ImportError:
            errors.append("python3-apt not installed")
        except Exception as exc:  # broken cache, etc.: try the tools
            errors.append(f"python-apt: {exc}")
    if shutil.which("apt-get"):
        try:
            items = parse_apt_get_sim(_run(["apt-get", "-s", "-q", "-o", "Debug::NoLocking=1",
                                            "dist-upgrade"]))
            res.items = [i for i in items if i.current]  # upgrades; new installs follow them
            res.info["method"] = "apt-get -s"
            return res
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"apt-get -s: {exc}")
    if shutil.which("apt"):
        try:
            res.items = parse_apt_list(_run(["apt", "list", "--upgradable"]))
            res.info["method"] = "apt list"
            return res
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"apt list: {exc}")
    res.error = "; ".join(errors) or "no apt tools found"
    return res
