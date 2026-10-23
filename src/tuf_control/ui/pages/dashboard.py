"""Overview: one live card per subsystem; a card opens its detail page."""
from __future__ import annotations

from gi.repository import Gtk, Pango

from ...core import Capability
from ...telemetry import fmt
from ..charts import Line
from ..viewmodels import GPU_MODE_INFO, PROFILE_INFO
from ..widgets import label
from .base import pill
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
        self.extra = Gtk.Box(spacing=16, halign=Gtk.Align.END, hexpand=True,
                             valign=Gtk.Align.END)
        row.append(self.extra)
        self.append(row)
        self.chart = page.chart(lines, window_s=WINDOW_S, compact=True, height=86, **chart_kw)
        self.chart.set_margin_top(6)
        self.append(self.chart)
        self.foot = label("", ("stat-sub", "numeric"), ellipsize=Pango.EllipsizeMode.END)
        self.foot.set_margin_top(4)
        self.append(self.foot)

    def mini(self, caption: str) -> Gtk.Label:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        val = label("–", ("mini-value", "numeric"), xalign=1.0)
        box.append(val)
        box.append(label(caption, ("mini-label",), xalign=1.0))
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
        self.heading, self.caption, _ = self.header("Overview", "Live readings, last two minutes")
        self.pills = Gtk.Box(spacing=6, halign=Gtk.Align.START)
        self.body.append(self.pills)
        pct = dict(y_fixed=(0, 100), fmt=lambda v: f"{v:.0f}%")
        rpm = dict(floor=0, min_span=1000, fmt=lambda v: f"{v / 1000:g}k")
        rate = dict(floor=0, min_span=1e5, fmt=fmt.axis_rate)
        C = MetricCard
        self.cpu = C(self, "cpu", "Processor", "computer-symbolic", [Line("cpu.total")], **pct)
        self.cpu_temp = self.cpu.mini("°C pkg")
        self.cpu_freq = self.cpu.mini("GHz avg")
        self.mem = C(self, "memory", "Memory", "media-flash-symbolic",
                     [Line("mem.in_use", scale=1 / GIB)], floor=0, min_span=1,
                     fmt=lambda v: f"{v:g} G")
        self.mem_swap = self.mem.mini("GiB swap")
        self.mem_avail = self.mem.mini("GiB avail")
        self.igpu = C(self, "igpu", "Intel graphics", "video-display-symbolic",
                      [Line("igpu.util")], **pct)
        self.igpu_freq = self.igpu.mini("MHz")
        self.igpu_awake = self.igpu.mini("% awake")
        self.dgpu = C(self, "dgpu", "NVIDIA graphics", "applications-graphics-symbolic",
                      [Line("dgpu.util")], **pct)
        self.dgpu_temp = self.dgpu.mini("°C")
        self.dgpu_power = self.dgpu.mini("W")
        self.fan_cpu = C(self, "fanmon", "CPU fan", "weather-windy-symbolic",
                         [Line("fan.cpu")], **rpm)
        self.fan_cpu_t = self.fan_cpu.mini("°C cpu")
        self.fan_gpu = C(self, "fanmon", "GPU fan", "weather-windy-symbolic",
                         [Line("fan.gpu", color="gold")], **rpm)
        self.fan_gpu_t = self.fan_gpu.mini("°C gpu")
        self.disk = C(self, "storage", "Storage", "drive-harddisk-solidstate-symbolic",
                      [Line("disk.read", "read"),
                       Line("disk.write", "write", color="gold", fill=False)], **rate)
        self.disk_w = self.disk.mini("write")
        self.disk_t = self.disk.mini("°C")
        self.net = C(self, "network", "Network", "network-transmit-receive-symbolic",
                     [Line("net.rx", "down"), Line("net.tx", "up", color="gold", fill=False)],
                     **rate)
        self.net_tx = self.net.mini("up")
        self.bat = C(self, "battery", "Battery", "battery-level-80-symbolic",
                     [Line("bat.power")], floor=0, min_span=10, fmt=lambda v: f"{v:g} W")
        self.bat_pct = self.bat.mini("% charge")
        self.bat_health = self.bat.mini("% health")

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

    # -- summary pills (profile, GPU mode, charge limit) ---------------------------------
    def refresh(self) -> None:
        from ..tasks import run_bg

        def summary():
            out = {}
            for key, fn in (("gpu", lambda: self.ctl.get_gpu_status()[0]),
                            ("thr", lambda: self.ctl.get_battery_threshold()[0])):
                try:
                    out[key] = fn()
                except Exception:  # each summary item is optional
                    out[key] = None
            return out

        def done(res):
            self.gpu_mode, self.threshold = res["gpu"], res["thr"]
            self._pills()

        if self.win.caps is not None:  # after the controller's probe (it is not reentrant)
            run_bg(summary, done=done)
        self.update()

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

    def on_telemetry(self, t) -> None:
        if t.profile is not None:
            self._pills(t.profile)

    # -- live --------------------------------------------------------------------------
    def _stats(self, key: str):
        st = self.hub.store
        return st.stats(key, (st.last_t or 0) - WINDOW_S)

    def update(self) -> None:
        st = self.hub.store
        info = self.hub.info
        last = st.last
        # CPU
        cpu = info.get("cpu") or {}
        static = self.hub.sampler.cpu.static
        self.cpu.set_value(fmt.num(last("cpu.total")), "%")
        set_text(self.cpu_temp, fmt.num(last("temp.cpu")))
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
        if ig.get("present"):
            self.igpu.set_value(fmt.num(last("igpu.util")), "%")
            set_text(self.igpu_freq, fmt.num(last("igpu.freq")))
            set_text(self.igpu_awake, fmt.num(last("igpu.awake")))
            req = last("igpu.freq_req")
            set_text(self.igpu.foot, "render engine busy" +
                     (f" · requested {req:.0f} MHz" if req is not None else ""))
            set_text(self.igpu.title, (ig.get("name") or "Intel graphics")
                     .replace(" Graphics", ""))
        else:
            self.igpu.set_value("–")
            set_text(self.igpu.foot, ig.get("reason") or "No Intel GPU")
        # dGPU
        dg = info.get("dgpu") or {}
        self._dgpu(dg, last)
        # fans
        set_text(self.fan_cpu_t, fmt.num(last("temp.cpu")))
        set_text(self.fan_gpu_t, fmt.num(last("dgpu.temp")) if dg.get("state") == "awake"
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
        set_text(self.disk_t, fmt.num(last(f"temp.disk.{main}.0")) if main else "–")
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
        if not dg.get("present") or not dg.get("backend"):
            c.set_value("–")
            set_text(c.foot, dg.get("reason") or "No NVIDIA GPU")
            c.set_badge(None)
            return
        name = dg.get("name") or "NVIDIA graphics"
        set_text(c.title, name.replace("NVIDIA GeForce ", "GeForce ").replace(" Laptop GPU", ""))
        state = dg.get("state")
        if state in ("sleeping", "idle-released"):
            sleeping = state == "sleeping"
            c.set_value("Sleeping" if sleeping else "Idle")
            set_text(c.foot, f"{dg.get('power_state') or 'D3'}, not polled so it stays asleep"
                     if sleeping else "idle: polling paused so it can sleep · last check "
                     f"{dg.get('last_age_s') or 0:.0f} s ago")
            c.set_badge((dg.get("power_state") or "D3") if sleeping else "Idle")
            last = (dg.get("last_q") or {}) if not sleeping else {}
            set_text(self.dgpu_temp, fmt.num(last.get("temp")))
            set_text(self.dgpu_power, fmt.num(last.get("power"), 1))
            return
        q = dg.get("q") or {}
        c.set_value(fmt.num(last("dgpu.util")), "%")
        set_text(self.dgpu_temp, fmt.num(last("dgpu.temp")))
        set_text(self.dgpu_power, fmt.num(last("dgpu.power"), 1))
        ps = q.get("pstate")
        c.set_badge(f"P{ps}" if ps is not None else "Awake", "gold")
        used, total = q.get("vram_used"), q.get("vram_total")
        set_text(c.foot, f"VRAM {fmt.bytes_(used)} of {fmt.bytes_(total, 0)}"
                 if used is not None else (dg.get("reason") or "awake"))
