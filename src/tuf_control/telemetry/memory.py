"""Memory: /proc/meminfo, swap, zswap and pressure stall information."""
from __future__ import annotations

from .util import join, read_bytes, read_str

PSI_MISSING = ("not available: this kernel has no /proc/pressure (built without "
               "CONFIG_PSI or booted with psi=0)")


def parse_meminfo(data: bytes) -> dict[str, int]:
    """{field: bytes}; values in /proc/meminfo are kB (KiB) unless unitless (HugePages_*)."""
    out: dict[str, int] = {}
    for line in data.split(b"\n"):
        if not line:
            continue
        key, _, rest = line.partition(b":")
        parts = rest.split()
        if not parts:
            continue
        v = int(parts[0])
        out[key.decode()] = v * 1024 if len(parts) > 1 and parts[1] == b"kB" else v
    return out


def derive(mi: dict[str, int]) -> dict[str, float]:
    """Partition of MemTotal used by the charts, plus the figures `free` prints.

    in_use + reclaimable + free == total, exactly:
      in_use      = MemTotal - MemAvailable  (what procps >= 4 calls "used")
      reclaimable = MemAvailable - MemFree   (cache the kernel can drop)
    """
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    free = mi.get("MemFree", 0)
    out = {
        "total": total, "available": avail, "free": free,
        "in_use": max(0, total - avail),
        "reclaimable": max(0, avail - free),
        "buffers": mi.get("Buffers", 0), "cached": mi.get("Cached", 0),
        "buff_cache": mi.get("Buffers", 0) + mi.get("Cached", 0) + mi.get("SReclaimable", 0),
        "shmem": mi.get("Shmem", 0), "slab_reclaimable": mi.get("SReclaimable", 0),
        "slab_unreclaimable": mi.get("SUnreclaim", 0), "dirty": mi.get("Dirty", 0),
        "writeback": mi.get("Writeback", 0), "committed": mi.get("Committed_AS", 0),
        "commit_limit": mi.get("CommitLimit", 0),
        "swap_total": mi.get("SwapTotal", 0),
        "swap_used": max(0, mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)),
        "swap_cached": mi.get("SwapCached", 0),
        "zswap_pool": mi.get("Zswap"), "zswap_stored": mi.get("Zswapped"),
        "anon": mi.get("AnonPages", 0), "mapped": mi.get("Mapped", 0),
    }
    return out


def parse_psi(text: str | None) -> dict[str, dict[str, float]] | None:
    """{'some': {'avg10': .., 'avg60': .., 'avg300': .., 'total': us}, 'full': {...}}."""
    if not text:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        out[parts[0]] = {k: float(v) for k, v in (p.split("=") for p in parts[1:])}
    return out


class MemoryReader:
    def __init__(self, root: str = "/") -> None:
        self.root = root

    def zswap_enabled(self) -> bool | None:
        v = read_str(join(self.root, "sys/module/zswap/parameters/enabled"))
        return None if v is None else v in ("Y", "1")

    def pressure(self, name: str) -> tuple[dict | None, str | None]:
        """PSI for cpu|memory|io, or (None, reason)."""
        text = read_str(join(self.root, f"proc/pressure/{name}"))
        if text is None:
            return None, PSI_MISSING
        return parse_psi(text), None

    def sample(self, now: float, vals: dict, info: dict) -> None:
        data = read_bytes(join(self.root, "proc/meminfo"))
        if data is None:
            info["mem"] = {"error": "/proc/meminfo unreadable"}
            return
        d = derive(parse_meminfo(data))
        vals["mem.in_use"] = d["in_use"]
        vals["mem.reclaimable"] = d["reclaimable"]
        vals["mem.free"] = d["free"]
        vals["mem.swap_used"] = d["swap_used"]
        if d["zswap_pool"] is not None:
            vals["mem.zswap_pool"] = d["zswap_pool"]
        psi, why = self.pressure("memory")
        if psi:
            vals["mem.psi_some"] = psi.get("some", {}).get("avg10")
        d["psi"], d["psi_reason"] = psi, why
        info["mem"] = d
