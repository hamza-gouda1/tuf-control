# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Overview: one live card per subsystem; a card opens its detail page."""
from __future__ import annotations

from gi.repository import Gtk, Pango

from ...telemetry import fmt
from .. import prefs
from ..charts import Line
from ..tasks import run_bg
from ..viewmodels import (
    GPU_MODE_INFO,
    PROFILE_INFO,
    PROFILE_ORDER,
    dashboard_readiness,
    fmt_rpm,
    fmt_temp,
    gpu_vendor_title,
    temp_unit_label,
    temp_value,
)
from ..widgets import label
from .monitor_base import WINDOW_S, MonitorPage, set_text

GIB = 1 << 30


class MetricCard(Gtk.Box):
    def __init__(self, page, key: str, title: str, icon: str, lines: list[Line],
                 **chart_kw) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.key = key
        self.add_css_class("tile")
        self.add_css_class("metric-card")
        head = Gtk.Box(spacing=8)
        img = Gtk.Image.new_from_icon_name(icon)
        img.add_css_class("tile-icon")
        head.append(img)
        self.title = label(title, ("tile-title",), hexpand=True,
                           ellipsize=Pango.EllipsizeMode.END)
        head.append(self.title)
        self.badge = label("", ("pill",))
        self.badge.set_visible(False)
        head.append(self.badge)
        go = Gtk.Image.new_from_icon_name("go-next-symbolic")
        go.add_css_class("card-go")
        head.append(go)
        self.append(head)
        row = Gtk.Box(spacing=6, margin_top=4)
        self.value = label("–", ("stat-value",))
        self.unit = label("", ("stat-unit",))
        self.unit.set_valign(Gtk.Align.END)
        row.append(self.value)
        row.append(self.unit)
        self.extra = Gtk.Box(spacing=12, halign=Gtk.Align.END, hexpand=True,
                             valign=Gtk.Align.END)
        row.append(self.extra)
        self.append(row)
        self.chart = page.chart(lines, window_s=WINDOW_S, compact=True, height=112, **chart_kw)
        self.chart.set_margin_top(6)
        self.append(self.chart)
        self.foot = label("", ("stat-sub", "numeric"), wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
        # a wrapping label still requests its single-line width as the minimum unless
        # capped, which pushed the card past the 360 px window
        self.foot.set_max_width_chars(30)
        self.foot.set_margin_top(4)
        self.append(self.foot)

    def mini(self, caption: str) -> Gtk.Label:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        val = label("–", ("mini-value", "numeric"), xalign=1.0)
        box.append(val)
        cap = label(caption, ("mini-label",), xalign=1.0)
        # keep the cell's requested width small: two caption labels side by side would
        # otherwise push the card past the 360 px window
        cap.set_max_width_chars(12)
        cap.set_ellipsize(Pango.EllipsizeMode.END)
        cap.set_tooltip_text(caption)
        box.append(cap)
        self.cap_labels = getattr(self, "cap_labels", {})
        self.cap_labels[caption] = cap
        self.extra.append(box)
        return val

    def set_value(self, value: str, unit: str = "") -> None:
        set_text(self.value, value)
        set_text(self.unit, unit)

    def set_badge(self, text: str | None, css: str = "") -> None:
        self.badge.set_visible(bool(text))
        set_text(self.badge, text or "")
        for c in ("accent", "gold"):
            if c != css:
                self.badge.remove_css_class(c)
        if css:
            self.badge.add_css_class(css)


def _split(text: str) -> tuple[str, str]:
    return tuple(text.split(" ", 1)) if " " in text else (text, "")  # type: ignore


class DashboardPage(MonitorPage):
    key, title = "dashboard", "Dashboard"
    rate_s = 1.0

    def __init__(self, win) -> None:
        super().__init__(win)
        self.gpu_mode = self.threshold = self.health = None
        self._svc_status = None
        self._setup_spec = None
        self._toggling_bat = False
        self.heading, self.caption, _ = self.header("Overview", "Live readings, last two minutes")

        self.pills = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                 min_children_per_line=1, max_children_per_line=6,
                                 column_spacing=8, row_spacing=6, halign=Gtk.Align.FILL)
        self.body.append(self.pills)

        # dismissible setup / readiness card (shown only when a diagnostic needs action)
        self.setup_card = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER, visible=False)
        self.setup_card.add_css_class("setup-card")
        self.setup_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self.setup_icon.set_valign(Gtk.Align.START)
        self.setup_card.append(self.setup_icon)
        stexts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.setup_title = label("", ("panel-title",), wrap=True,
                                 wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.setup_body = label("", ("section-caption",), wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR)
        stexts.append(self.setup_title)
        stexts.append(self.setup_body)
        self.setup_card.append(stexts)
        self.setup_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.setup_btn.add_css_class("pill-button")
        self.setup_btn.connect("clicked", lambda *_: self._run_setup_action())
        self.setup_card.append(self.setup_btn)
        dismiss = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.START,
                             tooltip_text="Dismiss setup hint")
        dismiss.add_css_class("flat")
        dismiss.add_css_class("circular")
        dismiss.connect("clicked", lambda *_: self._dismiss_setup())
        self.setup_card.append(dismiss)
        self.body.append(self.setup_card)

        pct = dict(y_fixed=(0, 100), fmt=lambda v: f"{v:.0f}%")
        rpm = dict(y_fixed=(0, 7000), fmt=lambda v: f"{v / 1000:g}k")
        rate = dict(floor=0, min_span=1e6, fmt=fmt.axis_rate)
        C = MetricCard
        self.cpu = C(self, "cpu", "Processor", "computer-symbolic", [Line("cpu.total")], **pct)
        self.cpu_temp = self.cpu.mini("pkg")
        self.cpu_freq = self.cpu.mini("GHz avg")
        self.mem = C(self, "memory", "Memory", "media-flash-symbolic",
                     [Line("mem.in_use", scale=1 / GIB)], floor=0, min_span=16,
                     fmt=lambda v: f"{v:g} G")
        self.mem_swap = self.mem.mini("GiB swap")
        self.mem_avail = self.mem.mini("GiB avail")
        self.igpu = C(self, "igpu", "Intel graphics", "video-display-symbolic",
                      [Line("igpu.util")], **pct)
        self.igpu_freq = self.igpu.mini("MHz")
        self.igpu_awake = self.igpu.mini("% awake")
        self.dgpu = C(self, "dgpu", "NVIDIA graphics", "applications-graphics-symbolic",
                      [Line("dgpu.util")], **pct)
        self.dgpu_temp = self.dgpu.mini("gpu_t")
        self.dgpu_power = self.dgpu.mini("W")
        self.fan_cpu = C(self, "fanmon", "CPU fan", "weather-windy-symbolic",
                         [Line("fan.cpu")], **rpm)
        self.fan_cpu_t = self.fan_cpu.mini("cpu_t")
        self.fan_gpu = C(self, "fanmon", "GPU fan", "weather-windy-symbolic",
                         [Line("fan.gpu", color="gold")], **rpm)
        self.fan_gpu_t = self.fan_gpu.mini("gpu_fan_t")
        self.disk = C(self, "storage", "Storage", "drive-harddisk-solidstate-symbolic",
                      [Line("disk.read", "read"),
                       Line("disk.write", "write", color="gold", fill=False)], **rate)
        self.disk_w = self.disk.mini("write")
        self.disk_t = self.disk.mini("disk_t")
        self.net = C(self, "network", "Network", "network-transmit-receive-symbolic",
                     [Line("net.rx", "down"), Line("net.tx", "up", color="gold", fill=False)],
                     **rate)
        self.net_tx = self.net.mini("up")
        self.bat = C(self, "battery", "Battery", "battery-level-80-symbolic",
                     [Line("bat.power")], floor=0, min_span=60, fmt=lambda v: f"{v:g} W")
        self.bat_pct = self.bat.mini("% charge")
        self.bat_health = self.bat.mini("% health")
        self._sync_temp_labels()

        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           max_children_per_line=3, min_children_per_line=1,
                           column_spacing=14, row_spacing=14, activate_on_single_click=True)
        self.order = [self.cpu, self.mem, self.dgpu, self.igpu, self.fan_cpu, self.fan_gpu,
                      self.disk, self.net, self.bat]
        for c in self.order:
            flow.append(c)
            child = c.get_parent()
            child.add_css_class("card-child")
            child.card = c
        flow.connect("child-activated", lambda _f, ch: self.win.show_page(ch.card.key))
        self.flow = flow
        self.body.append(flow)

    # -- summary quick-controls + readiness ---------------------------------------------------
    def _sync_temp_labels(self) -> None:
        u = temp_unit_label()
        mapping = ((self.cpu, "pkg", f"{u} pkg"),
                   (self.dgpu, "gpu_t", u),
                   (self.fan_cpu, "cpu_t", f"{u} cpu"),
                   (self.fan_gpu, "gpu_fan_t", f"{u} gpu"),
                   (self.disk, "disk_t", u))
        for card, key, text in mapping:
            lbl = getattr(card, "cap_labels", {}).get(key)
            if lbl is not None:
                set_text(lbl, text)
                lbl.set_tooltip_text(text)

    def refresh(self) -> None:
        def summary():
            out: dict = {}
            for key, fn in (("gpu", lambda: self.ctl.get_gpu_status()[0]),
                            ("thr", lambda: self.ctl.get_battery_threshold()[0])):
                try:
                    out[key] = fn()
                except Exception:  # each summary item is optional
                    out[key] = None
            try:
                from ...daemon.client import DaemonClient
                out["svc"] = DaemonClient().status()
            except Exception:
                out["svc"] = None
            return out

        def done(res):
            self.gpu_mode, self.threshold = res["gpu"], res["thr"]
            self._svc_status = res.get("svc")
            self._pills()
            self._sync_setup()

        if self.win.caps is not None:  # after the controller's probe (it is not reentrant)
            run_bg(summary, done=done)
        self._sync_setup()
        self.update()

    def _sync_setup(self) -> None:
        spec = dashboard_readiness(self.win.caps, self.win.caps_error,
                                   self._svc_status, self.win.fake)
        if spec is None or (spec["id"] != "caps-error"
                            and prefs.load_prefs().get("dismissed_setup")):
            self.setup_card.set_visible(False)
            self._setup_spec = None
            return
        self._setup_spec = spec
        self.setup_icon.set_from_icon_name(spec["icon"])
        set_text(self.setup_title, spec["title"])
        set_text(self.setup_body, spec["body"])
        self.setup_btn.set_label(spec["action_label"])
        self.setup_btn.set_sensitive(True)
        self.setup_card.set_visible(True)

    def _dismiss_setup(self) -> None:
        prefs.save_prefs(dismissed_setup=True)
        self.setup_card.set_visible(False)

    def _run_setup_action(self) -> None:
        if not self._setup_spec:
            return
        act = self._setup_spec["action"]
        if act == "retry_caps":
            self.win.retry_caps()
        elif act == "open_about":
            self.win.show_page("about")
        elif act == "start_daemon":
            from ...daemon.client import DaemonClient
            self.setup_btn.set_sensitive(False)

            def start():
                DaemonClient().start()
                return DaemonClient().status()

            def done(st):
                self._svc_status = st
                self._sync_setup()
                self.win.toast("Background service started")

            def fail(exc):
                self.setup_btn.set_sensitive(True)
                self.win.toast_error(exc)

            run_bg(start, done=done, error=fail)

    def _append_pill(self, btn: Gtk.Button) -> None:
        self.pills.append(btn)
        child = btn.get_parent()
        if child is not None:
            child.set_focusable(False)

    def _cycle_profile(self) -> None:
        cur = self.win.profile
        if cur not in PROFILE_ORDER:
            nxt = PROFILE_ORDER[1]
        else:
            nxt = PROFILE_ORDER[(PROFILE_ORDER.index(cur) + 1) % len(PROFILE_ORDER)]
        self.win.request_profile(nxt)

    def _toggle_charge_limit(self) -> None:
        if self._toggling_bat or self.threshold is None:
            self.win.show_page("battery")
            return
        target = 100 if self.threshold < 95 else 80
        self._toggling_bat = True

        def done(_res=None):
            self._toggling_bat = False
            self.threshold = target
            self._pills()

        def err():
            self._toggling_bat = False

        self.win.run_write(lambda: self.ctl.set_battery_threshold(target),
                           f"Charge limit set to {target}%", after=done, on_error=err)

    def _pills(self, profile=None) -> None:
        prof = profile or self.win.profile
        st = self.hub.store
        cpu_t = st.last("temp.cpu")
        cpu_r = st.last("fan.cpu")
        unit = prefs.get_temp_unit()
        therm = (f"{fmt_temp(cpu_t, unit=unit)}"
                 + (f" · {fmt_rpm(cpu_r)} rpm" if cpu_r is not None else ""))
        key = (prof, self.gpu_mode.mode if self.gpu_mode else None, self.threshold,
               cpu_t is not None, round(cpu_t or 0), round(cpu_r or -1), unit)
        if key == getattr(self, "_pill_key", None):
            return
        self._pill_key = key
        child = self.pills.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.pills.remove(child)
            child = nxt
        if prof is not None:
            title, _sub, icon = PROFILE_INFO[prof]
            b = Gtk.Button(tooltip_text=f"{title} profile — click to cycle mode")
            b.add_css_class("quick-pill")
            b.add_css_class("accent")
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(label(title))
            b.set_child(box)
            b.connect("clicked", lambda *_: self._cycle_profile())
            self._append_pill(b)
        if self.gpu_mode is not None:
            gname = GPU_MODE_INFO[self.gpu_mode.mode][0] + " graphics"
            b = Gtk.Button(tooltip_text="Click to change GPU mode")
            b.add_css_class("quick-pill")
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image.new_from_icon_name("applications-graphics-symbolic"))
            box.append(label(gname))
            b.set_child(box)
            b.connect("clicked", lambda *_: self.win.show_page("gpu"))
            self._append_pill(b)
        if self.threshold is not None:
            b = Gtk.Button(tooltip_text="Click to toggle 80% / 100% battery charge limit")
            b.add_css_class("quick-pill")
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image.new_from_icon_name("battery-good-symbolic"))
            box.append(label(f"Charge limit {self.threshold}%"))
            b.set_child(box)
            b.connect("clicked", lambda *_: self._toggle_charge_limit())
            self._append_pill(b)
        if cpu_t is not None or cpu_r is not None:
            b = Gtk.Button(tooltip_text="Open Fans & temperatures monitor")
            b.add_css_class("quick-pill")
            b.add_css_class("gold")
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image.new_from_icon_name("weather-windy-symbolic"))
            box.append(label(therm, ("numeric",)))
            b.set_child(box)
            b.connect("clicked", lambda *_: self.win.show_page("fanmon"))
            self._append_pill(b)

    def on_telemetry(self, t) -> None:
        if t.profile is not None:
            self._pills(t.profile)

    # -- live --------------------------------------------------------------------------
    def _stats(self, key: str):
        st = self.hub.store
        return st.stats(key, (st.last_t or 0) - WINDOW_S)

    def update(self) -> None:
        self._sync_temp_labels()
        self._pills()
        st = self.hub.store
        info = self.hub.info
        last = st.last
        # CPU
        cpu = info.get("cpu") or {}
        static = self.hub.sampler.cpu.static
        self.cpu.set_value(fmt.num(last("cpu.total")), "%")
        set_text(self.cpu_temp, temp_value(last("temp.cpu")))
        f = last("cpu.freq.avg")
        set_text(self.cpu_freq, f"{f / 1000:.2f}" if f else "–")
        load = cpu.get("load")
        s = self._stats("cpu.total")
        set_text(self.cpu.foot, (f"load {load[0]:.2f} · " if load else "") +
                 f"{static['count']} threads" + (f" · max {s[1]:.0f} %" if s else ""))
        # memory
        mem = info.get("mem") or {}
        total = mem.get("total") or 0
        used = last("mem.in_use")
        self.mem.set_value(f"{used / GIB:.1f}" if used is not None else "–",
                           f"GiB of {total / GIB:.1f}" if total else "GiB")
        self.mem.chart.y_fixed = (0, total / GIB) if total else None
        set_text(self.mem_swap, f"{(mem.get('swap_used') or 0) / GIB:.1f}"
                 if mem.get("swap_total") else "off")
        set_text(self.mem_avail, f"{(mem.get('available') or 0) / GIB:.1f}" if mem else "–")
        set_text(self.mem.foot, f"{100 * used / total:.0f} % in use · cache "
                 f"{fmt.bytes_(mem.get('buff_cache'))}" if used is not None and total else "")
        # iGPU
        ig = info.get("igpu") or {}
        dg = info.get("dgpu") or {}
        ig_title = gpu_vendor_title(ig, "igpu")
        if self.igpu.get_parent() is not None:
            self.igpu.get_parent().set_visible(bool(ig.get("present")) or not dg.get("present"))
        if ig.get("present"):
            self.igpu.set_value(fmt.num(last("igpu.util")), "%")
            set_text(self.igpu_freq, fmt.num(last("igpu.freq")))
            set_text(self.igpu_awake, fmt.num(last("igpu.awake")))
            req = last("igpu.freq_req")
            set_text(self.igpu.foot, "render engine busy" +
                     (f" · requested {req:.0f} MHz" if req is not None else ""))
            set_text(self.igpu.title, (ig.get("name") or ig_title).replace(" Graphics", ""))
            self.igpu.title.set_tooltip_text(ig.get("name") or ig_title)
        else:
            self.igpu.set_value("–")
            set_text(self.igpu.foot, "No integrated GPU detected.")
        # dGPU
        self._dgpu(dg, last)
        # fans
        set_text(self.fan_cpu_t, temp_value(last("temp.cpu")))
        set_text(self.fan_gpu_t, temp_value(last("dgpu.temp")) if dg.get("state") == "awake"
                 else "–")
        for card, key in ((self.fan_cpu, "fan.cpu"), (self.fan_gpu, "fan.gpu")):
            v = last(key)
            card.set_value(f"{v:.0f}" if v is not None else "–", "rpm")
            s = self._stats(key)
            set_text(card.foot, f"min {s[0]:,.0f} · max {s[1]:,.0f} rpm".replace(",", " ")
                     if s else "No fan sensor")
        # storage
        disk = info.get("disk") or {}
        self.disk.set_value(*_split(fmt.rate(last("disk.read"))))
        set_text(self.disk_w, fmt.rate(last("disk.write")))
        main = next(iter(disk.get("disks") or {}), None)
        set_text(self.disk_t, temp_value(last(f"temp.disk.{main}.0")) if main else "–")
        set_text(self.disk.foot, "read · " + (f"{main} busy {fmt.pct(last(f'disk.{main}.busy'))}"
                                              if main else "no disks"))
        # network
        net = info.get("net") or {}
        self.net.set_value(*_split(fmt.rate(last("net.rx"))))
        set_text(self.net_tx, fmt.rate(last("net.tx")))
        up = [n for n, m in (net.get("ifaces") or {}).items()
              if not m["virtual"] and m.get("state") == "up"]
        set_text(self.net.foot, "down · " + (", ".join(up) if up else "no physical link up"))
        self._battery(info.get("battery") or {}, last)
        up_s = cpu.get("uptime")
        set_text(self.caption, "Live readings, last two minutes" +
                 (f" · up {fmt.duration(up_s)}" if up_s else ""))
        self.redraw()

    def _battery(self, b: dict, last) -> None:
        if not b.get("present"):
            self.bat.set_value("–")
            set_text(self.bat.foot, "No battery")
            return
        self.bat.set_value(fmt.num(last("bat.power"), 1), "W")
        set_text(self.bat_pct, fmt.num(b.get("percent")))
        health = None
        if b.get("energy_full") and b.get("energy_design"):
            health = 100 * b["energy_full"] / b["energy_design"]
        set_text(self.bat_health, fmt.num(health))
        stt = (b.get("status") or "").lower()
        text = {"discharging": "draw from battery", "charging": "charging rate",
                "full": "full, on AC", "not charging": "held at limit, on AC"}.get(
            stt, b.get("status") or "")
        if b.get("hours"):
            text += f" · {fmt.hours(b['hours'])} " + (
                "left at this draw" if stt == "discharging" else "to full")
        set_text(self.bat.foot, text)
        self.bat.set_badge(b.get("status") if stt in ("charging", "full", "not charging")
                           else None, "gold")

    def _dgpu(self, dg: dict, last) -> None:
        c = self.dgpu
        name = dg.get("name") or "NVIDIA graphics"
        set_text(c.title, name.replace("NVIDIA GeForce ", "GeForce ").replace(" Laptop GPU", ""))
        c.title.set_tooltip_text(name)
        state = dg.get("state")
        if state == "lost" or dg.get("runtime") == "error":
            c.set_value("Unavailable")
            set_text(c.foot, "driver lost the GPU · restart usually recovers it")
            c.set_badge("Error", "gold")
            set_text(self.dgpu_temp, "–")
            set_text(self.dgpu_power, "–")
            return
        if not dg.get("present") or not dg.get("backend"):
            c.set_value("–")
            set_text(c.foot, f"No readings from {gpu_vendor_title(dg, 'dgpu')}. Check the "
                             "driver, then reopen this page.")
            c.foot.set_tooltip_text(dg.get("reason") or None)
            c.set_badge(None)
            set_text(self.dgpu_temp, "–")
            set_text(self.dgpu_power, "–")
            return
        c.foot.set_tooltip_text(None)
        if state in ("sleeping", "idle-released"):
            sleeping = state == "sleeping"
            c.set_value("Sleeping" if sleeping else "Idle")
            set_text(c.foot, f"{dg.get('power_state') or 'D3'}, not polled so it stays asleep"
                     if sleeping else "idle: polling paused so it can sleep · last check "
                     f"{dg.get('last_age_s') or 0:.0f} s ago")
            c.set_badge((dg.get("power_state") or "D3") if sleeping else "Idle")
            last = (dg.get("last_q") or {}) if not sleeping else {}
            set_text(self.dgpu_temp, temp_value(last.get("temp")))
            set_text(self.dgpu_power, fmt.num(last.get("power"), 1))
            return
        q = dg.get("q") or {}
        c.set_value(fmt.num(last("dgpu.util")), "%")
        set_text(self.dgpu_temp, temp_value(last("dgpu.temp")))
        set_text(self.dgpu_power, fmt.num(last("dgpu.power"), 1))
        ps = q.get("pstate")
        c.set_badge(f"P{ps}" if ps is not None else "Awake", "gold")
        used, total = q.get("vram_used"), q.get("vram_total")
        set_text(c.foot, f"VRAM {fmt.bytes_(used)} of {fmt.bytes_(total, 0)}"
                 if used is not None else "Awake, but the VRAM reading did not come "
                                          "through.")
