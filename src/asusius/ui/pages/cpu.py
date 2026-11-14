# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Processor detail: utilisation, per-core grid, frequency, temperatures, policy."""
from __future__ import annotations

from ...telemetry import fmt
from ..charts import CoreGrid, Line, legend
from ..viewmodels import cpu_vendor_info, fmt_temp, temp_unit_label, temp_value
from .monitor_base import WINDOW_S, MonitorPage, StatGrid, note, set_text


class CpuPage(MonitorPage):
    key, title = "cpu", "Processor"

    def __init__(self, win) -> None:
        super().__init__(win)
        s = self.hub.sampler
        st = s.cpu.static
        self.static = st
        vinfo = cpu_vendor_info(st, {})
        sub = f"{st['count']} threads · {st['cores']} cores"
        if st["hybrid"]:
            sub += f" ({st['p_cores']} performance + {st['e_cores']} efficient)"
        self.name, self.sub, self.pills = self.header(st.get("model") or "Processor", sub)
        pct = lambda v: f"{v:.0f}%"  # noqa: E731
        p = self.panel("Utilisation", suffix=legend([("user", "primary"), ("system", "gold"),
                                                     ("I/O wait", "slate")]))
        p.append(self.chart([Line("cpu.user", "user"), Line("cpu.system", "system", "gold"),
                             Line("cpu.iowait", "I/O wait", "slate")], stacked=True,
                            y_fixed=(0, 100), fmt=pct, height=190, window_s=WINDOW_S))
        self.stats = StatGrid(2, 5)
        for key, cap, tip in (
                ("util", "Utilisation", "Busy time across all logical CPUs (/proc/stat)"),
                ("freq", "Average clock", "Mean of scaling_cur_freq over all CPUs"),
                ("temp", "Package", vinfo["temp_tip"]),
                ("power", "Package power", None),
                ("load", "Load average", "1, 5 and 15 minutes (runnable + uninterruptible)"),
                ("uptime", "Uptime", None),
                ("threads", "Runnable · threads", "Runnable now / all threads (/proc/loadavg)"),
                ("ctxt", "Context switches", "per second"),
                ("intr", "Interrupts", "per second"),
                ("governor", "Governor · EPP", None)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        self.rapl_note = note("")
        p.append(self.rapl_note)
        cores = self.panel("Logical CPUs", suffix=legend(
            [("performance core", "primary"), ("efficient core", "gold")]
            if st["hybrid"] else []))
        cores.append(note("Each tile: usage over the last minute, current clock and the "
                          "temperature of its physical core."))
        self.grid = CoreGrid(self.hub, st["cpus"], st["topology"])
        self.charts.append(self.grid)
        cores.append(self.grid)
        fp = self.panel("Clock and temperature")
        fp.append(legend([("average clock (GHz)", "primary")]))
        fp.append(self.chart([Line("cpu.freq.avg", "avg", scale=1 / 1000)], floor=0,
                             min_span=5, fmt=lambda v: f"{v:g} GHz", height=120))
        self.temp_legend = legend([(f"package {temp_unit_label()}", "rose")])
        fp.append(self.temp_legend)
        self.temp_chart = self.chart([Line("temp.cpu", "pkg", "rose")], y_fixed=(20, 100),
                                     fmt=lambda v: f"{temp_value(v)}°", height=120)
        fp.append(self.temp_chart)
        self.policy_grid = StatGrid(2, 4)
        for key, cap in (("driver", "Scaling driver"), ("pstate", vinfo["pstate_title"]),
                         ("turbo", "Turbo"), ("range", "Clock range")):
            self.policy_grid.add(key, cap)
        fp.append(self.policy_grid)
        self._policy_t = -1e9
        self._pol: dict | None = None

    def refresh_temperature_unit(self) -> None:
        from gi.repository import Gtk
        stack = [self.temp_legend]
        unit = temp_unit_label()
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label):
                w.set_label(f"package {unit}")
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        self.temp_chart.queue_draw()
        self.stats.set("temp", fmt_temp(self.hub.store.last("temp.cpu")))

    def update(self) -> None:
        st, info = self.hub.store, self.hub.info
        cpu = info.get("cpu") or {}
        g = self.stats
        g.set("util", fmt.pct(st.last("cpu.total"), 1))
        f = st.last("cpu.freq.avg")
        g.set("freq", fmt.mhz(f))
        g.set("temp", fmt_temp(st.last("temp.cpu")))
        w = st.last("power.cpu")
        g.set("power", fmt.watts(w) if w is not None else "needs root")
        load = cpu.get("load")
        g.set("load", f"{load[0]:.2f} · {load[1]:.2f} · {load[2]:.2f}" if load else "–")
        g.set("uptime", fmt.duration(cpu.get("uptime")))
        g.set("threads", f"{load[3]} · {load[4]}" if load else "–")
        g.set("ctxt", fmt.count(st.last("cpu.ctxt")) + " /s")
        g.set("intr", fmt.count(st.last("cpu.intr")) + " /s")
        now = st.last_t or 0
        if now - self._policy_t > 5:
            self._policy_t = now
            pol = self._pol = self.hub.sampler.cpu.policy()
            g.set("governor", f"{pol.get('governor') or '–'} · {pol.get('epp') or '–'}")
            pg = self.policy_grid
            pg.set("driver", pol.get("driver") or "–")
            vinfo = cpu_vendor_info(self.static, pol)
            if "pstate" in getattr(pg, "captions", ()):
                pg.captions["pstate"].set_label(vinfo["pstate_title"])
            if "temp" in getattr(g, "boxes", ()):
                g.boxes["temp"].set_tooltip_text(vinfo["temp_tip"])
            pg.set("pstate", pol.get("pstate_status") or "not in use")
            nt = pol.get("no_turbo")
            pg.set("turbo", {"0": "enabled", "1": "disabled"}.get(nt or "", "–"))
            topo = self.static["topology"]
            mins = [t["min_mhz"] for t in topo.values() if t["min_mhz"]]
            maxs = [t["max_mhz"] for t in topo.values() if t["max_mhz"]]
            pg.set("range", f"{fmt.mhz(min(mins))} – {fmt.mhz(max(maxs))}" if mins and maxs
                   else "–")
        vinfo = cpu_vendor_info(self.static, self._pol)
        set_text(self.rapl_note, "" if w is not None else vinfo["rapl_note"])
        self.rapl_note.set_visible(w is None)
        self.redraw()
