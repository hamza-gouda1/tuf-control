# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Memory detail: in use / reclaimable cache / free over time, composition, swap, pressure."""
from __future__ import annotations

from gi.repository import Gtk

from ...telemetry import fmt
from ..charts import CompositionBar, Line, legend
from .monitor_base import MonitorPage, StatGrid, note, set_text

GIB = 1 << 30


class MemoryPage(MonitorPage):
    key, title = "memory", "Memory"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, _ = self.header("Memory", "")
        gib = lambda v: f"{v:g} GiB"  # noqa: E731
        p = self.panel("Usage", suffix=legend([("in use", "primary"),
                                               ("cache (reclaimable)", "gold")]))
        self.main = self.chart([Line("mem.in_use", "in use", scale=1 / GIB),
                                Line("mem.reclaimable", "cache", "gold", scale=1 / GIB)],
                               stacked=True, floor=0, fmt=gib, height=200)
        p.append(self.main)
        self.bar = CompositionBar(16)
        p.append(self.bar)
        self.bar_caption = Gtk.Box(spacing=18)
        self.bar_labels = {}
        for k in ("in_use", "reclaimable", "free"):
            lab = Gtk.Label(xalign=0)
            lab.add_css_class("numeric")
            lab.add_css_class("stat-sub")
            self.bar_caption.append(lab)
            self.bar_labels[k] = lab
        p.append(self.bar_caption)
        p.append(note("In use = total − available (what `free` calls used). Cache is memory "
                      "the kernel can reclaim on demand (available − free). The three parts "
                      "add up to the total; free is the space above the chart."))
        self.stats = StatGrid(2, 4)
        for key, cap, tip in (
                ("total", "Total", "MemTotal: RAM the kernel manages"),
                ("in_use", "In use", None), ("available", "Available", None),
                ("free", "Free", None),
                ("buff_cache", "Buffers + cache", "Buffers + Cached + SReclaimable, as free -b"),
                ("shmem", "Shared (tmpfs, shm)", None),
                ("slab", "Kernel slab", "reclaimable / unreclaimable"),
                ("dirty", "Dirty · writeback", None),
                ("committed", "Committed", "Committed_AS of CommitLimit"),
                ("dimm", "Module temperature", None)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        sp = self.panel("Swap")
        self.swap_chart = self.chart([Line("mem.swap_used", "swap", "slate", scale=1 / GIB)],
                                     floor=0, min_span=0.5, fmt=gib, height=110)
        sp.append(self.swap_chart)
        self.swap = StatGrid(2, 4)
        for key, cap in (("used", "Swap used"), ("total", "Swap size"),
                         ("cached", "Swap cached"), ("zswap", "zswap")):
            self.swap.add(key, cap)
        sp.append(self.swap)
        pp = self.panel("Pressure")
        self.psi_grid = StatGrid(2, 3)
        for key, cap in (("some", "Some stalled (10 s)"), ("full", "All stalled (10 s)"),
                         ("some60", "Some stalled (60 s)")):
            self.psi_grid.add(key, cap)
        pp.append(self.psi_grid)
        self.psi_note = note("")
        pp.append(self.psi_note)

    def update(self) -> None:
        info = self.hub.info
        st = self.hub.store
        m = info.get("mem") or {}
        if not m or "total" not in m:
            set_text(self.sub, (m or {}).get("error") or "No data yet")
            return
        total = m["total"]
        self.main.y_fixed = (0, total / GIB)
        set_text(self.sub, f"{fmt.bytes_(total)} usable · {fmt.bytes_(m['in_use'])} in use "
                           f"({100 * m['in_use'] / total:.0f} %)")
        self.bar.set_parts([(m["in_use"] / total, "primary"),
                            (m["reclaimable"] / total, "gold"), (m["free"] / total, "muted")])
        for k, name in (("in_use", "In use"), ("reclaimable", "Cache"), ("free", "Free")):
            set_text(self.bar_labels[k], f"{name} {fmt.bytes_(m[k])}")
        g = self.stats
        g.set("total", fmt.bytes_(total))
        g.set("in_use", fmt.bytes_(m["in_use"]))
        g.set("available", fmt.bytes_(m["available"]))
        g.set("free", fmt.bytes_(m["free"]))
        g.set("buff_cache", fmt.bytes_(m["buff_cache"]))
        g.set("shmem", fmt.bytes_(m["shmem"]))
        g.set("slab", f"{fmt.bytes_(m['slab_reclaimable'])} / "
                      f"{fmt.bytes_(m['slab_unreclaimable'])}")
        g.set("dirty", f"{fmt.bytes_(m['dirty'])} · {fmt.bytes_(m['writeback'])}")
        g.set("committed", f"{fmt.bytes_(m['committed'])} of {fmt.bytes_(m['commit_limit'])}")
        d = st.last("temp.dimm0")
        g.set("dimm", fmt.temp(d) if d is not None else "no sensor")
        s = self.swap
        s.set("used", fmt.bytes_(m["swap_used"]))
        s.set("total", fmt.bytes_(m["swap_total"]) if m["swap_total"] else "no swap")
        s.set("cached", fmt.bytes_(m["swap_cached"]))
        zs = self.hub.sampler.mem.zswap_enabled()
        if m.get("zswap_pool") is None:
            s.set("zswap", "not reported")
        elif not zs and not m.get("zswap_stored"):
            s.set("zswap", "off")
        else:
            s.set("zswap", f"{fmt.bytes_(m['zswap_stored'])} in {fmt.bytes_(m['zswap_pool'])}")
        psi = m.get("psi")
        if psi:
            some, full = psi.get("some", {}), psi.get("full", {})
            self.psi_grid.set("some", fmt.pct(some.get("avg10"), 2))
            self.psi_grid.set("full", fmt.pct(full.get("avg10"), 2))
            self.psi_grid.set("some60", fmt.pct(some.get("avg60"), 2))
            set_text(self.psi_note, "Share of time tasks waited for memory (PSI).")
        else:
            for k in ("some", "full", "some60"):
                self.psi_grid.set(k, "–")
            set_text(self.psi_note, "Memory pressure is not measured: this kernel has no "
                                    "/proc/pressure (built without CONFIG_PSI or booted "
                                    "with psi=0).")
        self.redraw()
