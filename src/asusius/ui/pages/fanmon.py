# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Fans and temperatures: both fans live, linked to the fan-curve editor."""
from __future__ import annotations

from gi.repository import Gtk

from ...telemetry import fmt
from ..charts import Line, legend
from ..viewmodels import fmt_temp, gpu_vendor_title, temp_unit_label, temp_value
from ..widgets import label
from .base import button
from .monitor_base import WINDOW_S, MonitorPage, Panel, StatGrid, note, set_text

rpm_fmt = lambda v: f"{v / 1000:g}k" if v else "0"  # noqa: E731


class FanMonitorPage(MonitorPage):
    key, title = "fanmon", "Fans & temperatures"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, pills = self.header("Fans and temperatures", "")
        pills.append(button("Edit fan curves", "pill-button", icon="document-edit-symbolic",
                            on_click=lambda: self.win.show_page("fans")))
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                          min_children_per_line=1, max_children_per_line=2,
                          column_spacing=14, row_spacing=14)
        self.fans = {}
        for key, title, colour, temp_key, temp_label in (
                ("cpu", "CPU fan", "primary", "temp.cpu", "CPU package"),
                ("gpu", "GPU fan", "gold", "dgpu.temp", "GPU")):
            p = Panel(title)
            big = Gtk.Box(spacing=6)
            val = label("–", ("stat-value",))
            big.append(val)
            big.append(label("rpm", ("stat-unit",), valign=Gtk.Align.END))
            p.append(big)
            p.append(self.chart([Line(f"fan.{key}", "rpm", colour)], y_fixed=(0, 7000),
                                fmt=rpm_fmt, height=150, window_s=WINDOW_S))
            g = StatGrid(2, 3)
            g.add("peak", "Peak (2 min)")
            g.add("share", "Of session peak",
                  "The firmware does not report a maximum speed, so this is relative to the "
                  "highest speed seen since the app started")
            g.add("temp", temp_label)
            p.append(g)
            row.append(p)
            p.get_parent().set_focusable(False)
            self.fans[key] = (val, g, temp_key)
        self.body.append(row)
        self.session_peak = {"cpu": 0.0, "gpu": 0.0}
        self.missing = note("")
        self.body.append(self.missing)
        tp = self.panel("Temperatures", suffix=legend([("CPU package", "rose"),
                                                       ("GPU", "gold"), ("NVMe", "slate"),
                                                       ("memory", "sage")]))
        self.temp_lines = [Line("temp.cpu", "CPU", "rose", fill=False),
                           Line("dgpu.temp", "GPU", "gold", fill=False),
                           Line("temp.disk.nvme0n1.0", "NVMe", "slate", fill=False),
                           Line("temp.dimm0", "memory", "sage", fill=False)]
        self.temp_chart = self.chart(self.temp_lines, y_fixed=(20, 100),
                                     fmt=lambda v: f"{temp_value(v)}{temp_unit_label()}",
                                     height=170)
        tp.append(self.temp_chart)
        self.temps = StatGrid(2, 4)
        tp.append(self.temps)
        self._temp_keys: list[str] = []
        tp.append(note("The dGPU temperature is only read while it is awake. The fans are "
                       "set by the firmware from its curve; edit the curves in Fan curves."))

    def refresh_temperature_unit(self) -> None:
        self.temp_chart.queue_draw()
        self.update()

    def _temps(self) -> list[tuple[str, str]]:
        info = self.hub.info.get("sensors") or {}
        out = [("temp.cpu", "CPU package")]
        out.append(("dgpu.temp", gpu_vendor_title(self.hub.info.get("dgpu"), "dgpu")))
        for disk, labels in (info.get("nvme") or {}).items():
            for i, lab in enumerate(labels):
                out.append((f"temp.disk.{disk}.{i}", f"{disk} {lab}"))
        for key, lab in info.get("extra") or []:
            out.append((f"temp.{key}", lab))
        return out

    def update(self) -> None:
        st = self.hub.store
        now = st.last_t or 0
        info = self.hub.info.get("sensors") or {}
        fans = dict(info.get("fans") or [])
        for key, (val, g, temp_key) in self.fans.items():
            v = st.last(f"fan.{key}")
            set_text(val, f"{v:.0f}" if v is not None else "–")
            s = st.stats(f"fan.{key}", now - WINDOW_S)
            whole = st.stats(f"fan.{key}")  # everything still in the ring buffer
            if whole:
                self.session_peak[key] = max(self.session_peak[key], whole[1])
            g.set("peak", fmt.rpm(s[1]) if s else "–")
            peak = self.session_peak[key]
            g.set("share", fmt.pct(100 * v / peak) if v is not None and peak else "–")
            t = st.last(temp_key)
            g.set("temp", fmt_temp(t) if t is not None else
                  ("asleep" if temp_key == "dgpu.temp" else "–"))
        missing = [k for k in ("cpu", "gpu") if k not in fans]
        set_text(self.missing, ("No sensor for: " + ", ".join(missing) + " fan.")
                 if missing else "")
        self.missing.set_visible(bool(missing))
        keys = self._temps()
        if [k for k, _ in keys] != self._temp_keys:
            self._temp_keys = [k for k, _ in keys]
            for k, lab in keys:
                if k not in self.temps.cells:
                    self.temps.add(k, lab)
            nv = next((k for k, _ in keys if k.startswith("temp.disk.")), None)
            if nv:
                self.temp_lines[2].key = nv
        for k, _lab in keys:
            v = st.last(k)
            self.temps.set(k, fmt_temp(v) if v is not None else
                           ("asleep" if k == "dgpu.temp" else "–"))
        set_text(self.sub, f"{len(fans)} fans · {len(keys)} temperature sensors · hwmon")
        self.redraw()
