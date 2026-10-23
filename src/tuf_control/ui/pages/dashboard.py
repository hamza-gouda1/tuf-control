from __future__ import annotations

import time

from gi.repository import Adw, Gtk

from ...core import Capability
from ..viewmodels import (
    DGPU_POWER_TEXT,
    GPU_MODE_INFO,
    PROFILE_INFO,
    History,
    fmt_num,
    fmt_rpm,
)
from ..widgets import Tile, label
from .base import Page, pill

WINDOW_S = 120.0


class DashboardPage(Page):
    key, title = "dashboard", "Dashboard"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.h = {k: History(WINDOW_S) for k in ("cpu", "gpu", "fan", "bat")}
        self.health = None
        self.gpu_mode = None
        self.threshold = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                        margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)
        head = Gtk.Box(spacing=12, orientation=Gtk.Orientation.VERTICAL)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.heading = label("Overview", ("hero-heading",))
        titles.append(self.heading)
        self.caption = label("Live readings, last two minutes", ("section-caption",))
        titles.append(self.caption)
        head.append(titles)
        self.pills = Gtk.Box(spacing=6, halign=Gtk.Align.START)
        head.append(self.pills)
        outer.append(head)

        self.cpu = Tile("Processor", "computer-symbolic", "°C", self.h["cpu"], 20, 15)
        self.cpu_power = self.cpu.add_mini("watts")
        self.gpu = Tile("Graphics", "application-x-firmware-symbolic", "°C", self.h["gpu"], 20, 15)
        self.gpu_power = self.gpu.add_mini("watts")
        self.gpu_util = self.gpu.add_mini("load %")
        self.fan = Tile("Fans", "weather-windy-symbolic", "rpm", self.h["fan"], 0, 800)
        self.fan2 = self.fan.add_mini("gpu rpm")
        self.bat = Tile("Battery", "battery-level-80-symbolic", "W", self.h["bat"], 0, 8)
        self.bat_pct = self.bat.add_mini("charge %")
        self.bat_health = self.bat.add_mini("health %")

        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           max_children_per_line=2, min_children_per_line=1,
                           column_spacing=14, row_spacing=14)
        for tile in (self.cpu, self.gpu, self.fan, self.bat):
            flow.append(tile)
            tile.get_parent().set_focusable(False)
        outer.append(flow)

        clamp = Adw.Clamp(maximum_size=1080, tightening_threshold=800, child=outer)
        self.set_child(Gtk.ScrolledWindow(child=clamp,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))

    def refresh(self) -> None:
        from ..tasks import run_bg

        def summary():
            out = {}
            for key, fn in (("health", lambda: self.ctl.read(Capability.BATTERY_HEALTH)[0]),
                            ("gpu", lambda: self.ctl.get_gpu_status()[0]),
                            ("thr", lambda: self.ctl.get_battery_threshold()[0])):
                try:
                    out[key] = fn()
                except Exception:  # each summary item is optional
                    out[key] = None
            return out

        def done(res):
            self.health, self.gpu_mode, self.threshold = res["health"], res["gpu"], res["thr"]
            self._pills()
            if self.health is not None:
                self.bat_health.set_label(fmt_num(self.health.health_percent))

        run_bg(summary, done=done)

    def _pills(self, profile=None) -> None:
        prof = profile or self.win.profile
        key = (prof, self.gpu_mode.mode if self.gpu_mode else None, self.threshold)
        if key == getattr(self, "_pill_key", None):
            return
        self._pill_key = key
        child = self.pills.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.pills.remove(child)
            child = nxt
        if prof is not None:
            self.pills.append(pill(PROFILE_INFO[prof][0], "accent"))
        if self.gpu_mode is not None:
            self.pills.append(pill(GPU_MODE_INFO[self.gpu_mode.mode][0] + " graphics"))
        if self.threshold is not None:
            self.pills.append(pill(f"Charge limit {self.threshold}%"))

    def ingest(self, t, now: float | None = None) -> None:
        """Push one Telemetry sample (also used by the screenshot harness)."""
        now = time.monotonic() if now is None else now
        self.h["cpu"].push(now, t.cpu_temp_c)
        self.h["gpu"].push(now, t.gpu_temp_c)
        fans = list(t.fan_rpm.values())
        self.h["fan"].push(now, fans[0] if fans else None)
        self.h["bat"].push(now, t.battery_power_w)

    def on_telemetry(self, t) -> None:
        self.ingest(t)
        self.cpu.value.set_label(fmt_num(t.cpu_temp_c))
        self.cpu_power.set_label(fmt_num(t.cpu_power_w, 1))
        self.cpu.sub.set_label("Package temperature" + (
            "" if t.cpu_power_w is not None else " · power needs root (RAPL)"))
        asleep = t.gpu_runtime not in (None, "active")
        self.gpu.value.set_label(fmt_num(t.gpu_temp_c) if not asleep else "–")
        self.gpu_power.set_label(fmt_num(t.gpu_power_w, 1) if not asleep else "–")
        self.gpu_util.set_label(fmt_num(t.gpu_util_pct) if not asleep else "–")
        state = DGPU_POWER_TEXT.get(t.gpu_runtime or "", None)
        self.gpu.set_badge(state, "gold" if t.gpu_runtime == "active" else "")
        self.gpu.sub.set_label("Sleeping, not polled to save power" if asleep else
                               ("Discrete GPU" if t.gpu_temp_c is not None
                                else "No readings (nvidia-smi missing?)"))
        fans = list(t.fan_rpm.items())
        self.fan.value.set_label(fmt_rpm(fans[0][1]) if fans else "–")
        self.fan2.set_label(fmt_rpm(fans[1][1]) if len(fans) > 1 else "–")
        self.fan.sub.set_label("CPU fan" if fans else "No fan sensors")
        self.bat.value.set_label(fmt_num(t.battery_power_w, 1))
        self.bat_pct.set_label(fmt_num(t.battery_percent))
        st = (t.battery_status or "").lower()
        self.bat.sub.set_label({"discharging": "Draw from battery", "charging": "Charging rate",
                                "full": "Full, on AC", "not charging": "Held at limit, on AC"}
                               .get(st, t.battery_status or "No battery"))
        self.bat.set_badge(t.battery_status if st in ("charging", "full", "not charging")
                           else None, "gold")
        if t.profile is not None:
            self._pills(t.profile)
        for tile in (self.cpu, self.gpu, self.fan, self.bat):
            tile.graph.queue_draw()
