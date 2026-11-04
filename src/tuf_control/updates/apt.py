# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
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
import time
from pathlib import Path

from . import blockers
from .model import SourceResult, UpdateItem

TIMEOUT_S = 60
DIAGNOSTIC_BUDGET_S = 15
DIAGNOSTIC_COMMAND_TIMEOUT_S = 3
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
          held=False, origin="", facts: dict | None = None) -> UpdateItem:
    kind, reboot = classify(name)
    extra = {"arch": arch}
    if origin:
        extra["origin"] = origin
    if held:
        extra["held"] = True
    if facts is not None:
        facts = {**facts, "kept_back": bool(facts.get("kept_back", held))}
        extra["facts"] = facts
        bl = blockers.classify(name, facts)
        if bl:
            extra["blocker"] = bl
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
        dep.upgrade(False)  # safe upgrade: no new dependencies or package removals
    except SystemError:
        pass
    records = apt_pkg.PackageRecords(cache)
    holds = {p.name for p in cache.packages if p.selected_state == apt_pkg.SELSTATE_HOLD}
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
        desc, phased = "", None
        if cand.file_list and records.lookup(cand.file_list[0]):
            desc = records.short_desc or ""
            m = re.search(r"^Phased-Update-Percentage:\s*(\d+)", records.record or "", re.M)
            phased = int(m.group(1)) if m else None
        held = pkg.name in holds or not (dep.marked_upgrade(pkg) or dep.marked_install(pkg))
        facts = {"kept_back": held, "dpkg_hold": pkg.name in holds, "phased_pct": phased}
        out.append((pkg, dict(name=pkg.name, arch=cand.arch, current=cur.ver_str,
                              available=cand.ver_str, security=_is_security(archives),
                              size=cand.size or None, description=desc, held=held,
                              origin=", ".join(origins), facts=facts)))
    # after the scan: the simulations reset the (shared) dependency cache
    for pkg, kw in out:
        if kw["held"]:
            kw["facts"].update(simulate_install(apt_pkg, cache, pkg, holds))
    items = [_item(kw.pop("name"), kw.pop("arch"), kw.pop("current"), kw.pop("available"),
                   **kw) for _p, kw in out]
    return sorted(items, key=lambda i: i.name)


def simulate_install(apt_pkg, cache, pkg, holds: set[str]) -> dict:
    """What installing the candidate of a kept-back package would change, on a scratch
    DepCache (nothing is written): new packages, other upgrades, removals, held ones."""
    dep = apt_pkg.DepCache(cache)
    dep.init()  # one dependency cache per apt cache: start from nothing marked
    try:
        dep.mark_install(pkg, True)
    except SystemError:
        return {"broken": True}
    new, upg, rem = [], [], []
    for q in cache.packages:
        if q.name == pkg.name:
            continue
        if dep.marked_delete(q):
            rem.append(q.name)
        elif dep.marked_install(q) and q.current_ver is None:
            new.append(q.name)
        elif dep.marked_upgrade(q) and q.current_ver is not None:
            upg.append(q.name)
    return {"new_packages": sorted(new), "upgrades": sorted(upg), "removals": sorted(rem),
            "held_involved": sorted(n for n in set(upg) | set(rem) if n in holds),
            "broken": dep.broken_count > 0}


def dpkg_holds(root: Path = Path("/")) -> set[str]:
    try:
        return blockers.parse_dpkg_holds((root / "var/lib/dpkg/status").read_text())
    except OSError:
        return set()


def selection_holds(text: str) -> set[str]:
    return {parts[0].split(":")[0] for line in text.splitlines()
            if len(parts := line.split()) == 2 and parts[1] == "hold"}


def simulation_facts(text: str, name: str, holds: set[str]) -> dict:
    items = parse_apt_get_sim(text)
    removals = re.findall(r"^Remv (\S+)", text, re.M)
    new = sorted({i.name for i in items if not i.current and i.name != name})
    upgraded = sorted({i.name for i in items if i.current and i.name != name})
    changed = set(removals) | set(upgraded)
    return {"new_packages": new, "upgrades": upgraded, "removals": removals,
            "held_involved": sorted(changed & holds), "dpkg_hold": name in holds,
            "broken": bool(re.search(r"unmet dependencies|Unable to correct problems|"
                                     r"held broken packages", text, re.I))}


def text_blockers(items: list[UpdateItem], holds: set[str], safe: str,
                  full: str, debug: str = "") -> None:
    # Targeted diagnostics must not consume the check child's entire 240 s lifetime.
    # Every command shares this deadline; queued items remain conservatively held
    # when it expires, with no invented dependency-install plan.
    deadline = time.monotonic() + DIAGNOSTIC_BUDGET_S

    def diagnostic(cmd: list[str]) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return _run(cmd, timeout=min(DIAGNOSTIC_COMMAND_TIMEOUT_S, remaining))

    safe_names = {i.name for i in parse_apt_get_sim(safe)}
    for item in items:
        kept = item.name in holds or item.name not in safe_names
        if not kept:
            continue
        facts = {"kept_back": True, "dpkg_hold": item.name in holds}
        # Whole-upgrade diagnostics are useful evidence, but are never a safe
        # per-item installation plan. A targeted simulation supplies that list.
        facts["dist_upgrade"] = full[-20000:]
        facts["resolver"] = debug[-20000:]
        try:
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", item.name):
                raise ValueError("invalid package name")
            targeted = diagnostic(["apt-get", "-s", "-o", "Debug::NoLocking=1", "install", "--",
                                   item.name + "=" + item.available])
            if targeted is None:
                facts["diagnostics_deferred"] = True
            else:
                facts.update(simulation_facts(targeted, item.name, holds))
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
            facts["broken"] = True
            involved = {n for n in holds if re.search(r"(?<![\w+.-])" + re.escape(n) +
                                                       r"(?![\w+.-])", debug)}
            facts["held_involved"] = sorted(involved)
        try:
            policy = diagnostic(["apt-cache", "policy", "--", item.name])
            if policy is None:
                facts["diagnostics_deferred"] = True
            else:
                match = re.search(r"\(phased (\d+)%\)", policy)
                if match:
                    facts["phased_pct"] = int(match.group(1))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
        item.extra.update(held=True, facts=facts, blocker=blockers.classify(item.name, facts))


def apply_holds(items: list[UpdateItem], holds: set[str]) -> None:
    """Text fallbacks know only holds: mark those items as held by the owner."""
    for i in items:
        if i.name in holds:
            i.extra["held"] = True
            i.extra["facts"] = {"kept_back": True, "dpkg_hold": True}
            i.extra["blocker"] = blockers.classify(i.name, i.extra["facts"])


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
            full = _run(["apt-get", "-s", "-q", "-o", "Debug::NoLocking=1", "dist-upgrade"])
            try:
                safe = _run(["apt-get", "-s", "-q", "-o", "Debug::NoLocking=1", "upgrade"])
            except (RuntimeError, OSError, subprocess.SubprocessError):
                safe = ""
            try:
                debug = _run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "-o",
                              "Debug::pkgProblemResolver=yes", "dist-upgrade"])
            except (RuntimeError, OSError, subprocess.SubprocessError):
                debug = ""
            # apt list retains held/phased candidates omitted by dist-upgrade.
            items = {i.id: i for i in parse_apt_get_sim(full) if i.current}
            if shutil.which("apt"):
                try:
                    items.update({i.id: i for i in parse_apt_list(_run(["apt", "list", "--upgradable"]))})
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    pass
            holds = dpkg_holds(root)
            if root == Path("/") and shutil.which("dpkg"):
                try:
                    holds |= selection_holds(_run(["dpkg", "--get-selections"]))
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    pass
            res.items = list(items.values())
            text_blockers(res.items, holds, safe, full, debug)
            res.info["method"] = "apt-get simulations + apt list"
            return res
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"apt-get -s: {exc}")
    if shutil.which("apt"):
        try:
            res.items = parse_apt_list(_run(["apt", "list", "--upgradable"]))
            res.info["method"] = "apt list"
            apply_holds(res.items, dpkg_holds(root))
            return res
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"apt list: {exc}")
    res.error = "; ".join(errors) or "no apt tools found"
    return res
