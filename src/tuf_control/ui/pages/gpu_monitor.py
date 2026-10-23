"""Detail pages for the Intel iGPU and the NVIDIA dGPU."""
from __future__ import annotations

from gi.repository import Gtk, Pango

from ...telemetry import fmt
from ...telemetry.gpu_intel import ENGINE_NAMES
from ...telemetry.util import join, read_str
from ..charts import Line, legend
from ..widgets import label
from .monitor_base import MonitorPage, StatGrid, note, set_text

pct = lambda v: f"{v:.0f}%"  # noqa: E731


class ProcTable(Gtk.Box):
    """Small table of processes using a GPU (rebuilt only when the set changes)."""

    def __init__(self, root: str, columns: list[str]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.root, self.columns = root, columns
        self.add_css_class("gpu-procs")
        self._key = None
        self._names: dict[int, str] = {}
        self.empty = note("No processes reported.")

    def name(self, pid: int) -> str:
        n = self._names.get(pid)
        if n is None:
            n = read_str(join(self.root, f"proc/{pid}/comm")) or f"pid {pid}"
            self._names[pid] = n
        return n

    def set_rows(self, rows: list[tuple[int, list[str]]]) -> None:
        key = tuple((pid, tuple(cells)) for pid, cells in rows)
        if key == self._key:
            return
        self._key = key
        child = self.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt
        if not rows:
            self.append(self.empty)
            return
        head = Gtk.Box(spacing=12)
        head.add_css_class("gpu-procs-head")
        head.append(label("Process", ("stat-caption",), hexpand=True))
        for c in self.columns:
            head.append(label(c, ("stat-caption",), xalign=1.0, width_chars=9))
        self.append(head)
        for pid, cells in rows:
            r = Gtk.Box(spacing=12)
            r.add_css_class("gpu-procs-row")
            r.append(label(f"{self.name(pid)}", ("numeric",), hexpand=True,
                           ellipsize=Pango.EllipsizeMode.END))
            r.append(label(str(pid), ("dim-label", "numeric"), xalign=1.0, width_chars=7))
            for c in cells:
                r.append(label(c, ("numeric",), xalign=1.0, width_chars=9))
            self.append(r)


class IntelGpuPage(MonitorPage):
    key, title = "igpu", "Intel graphics"

    def __init__(self, win) -> None:
        super().__init__(win)
        ig = self.hub.sampler.igpu
        self.name, self.sub, self.pills = self.header(ig.name or "Intel graphics", "")
        p = self.panel("Utilisation", suffix=legend([("render engine busy", "primary"),
                                                     ("GT awake (not in RC6)", "gold")]))
        p.append(self.chart([Line("igpu.util", "render"),
                             Line("igpu.awake", "awake", "gold", fill=False, dash=True)],
                            y_fixed=(0, 100), fmt=pct, height=180))
        self.stats = StatGrid(2, 4)
        for key, cap, tip in (("util", "Render busy", None), ("awake", "GT awake", None),
                              ("rc6", "RC6 (power-gated)", "Share of time the GT slept"),
                              ("freq", "Actual clock", "gt_act_freq_mhz"),
                              ("req", "Requested clock", "gt_cur_freq_mhz"),
                              ("range", "Clock range", "min – max (RP0)"),
                              ("clients", "DRM clients", None),
                              ("throttle", "Throttling", "throttle_reason_status")):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        p.append(note("Utilisation is summed from the per-client engine time the i915/xe "
                      "driver publishes in /proc/<pid>/fdinfo, for processes this app may "
                      "read (your session). The iGPU has no VRAM: it shares system memory."))
        fp = self.panel("Clock", suffix=legend([("actual MHz", "primary"),
                                                ("requested MHz", "gold")]))
        fp.append(self.chart([Line("igpu.freq", "actual"),
                              Line("igpu.freq_req", "requested", "gold", fill=False)],
                             floor=0, min_span=200, fmt=lambda v: f"{v:.0f}", height=130))
        ep = self.panel("Engines")
        self.engines = StatGrid(2, 5)
        ep.append(self.engines)
        self._engines_shown: set[str] = set()
        pp = self.panel("Processes using the iGPU")
        self.procs = ProcTable(self.hub.root, ["busy"])
        pp.append(self.procs)

    def update(self) -> None:
        st = self.hub.store
        ig = self.hub.info.get("igpu") or {}
        if not ig.get("present"):
            set_text(self.sub, ig.get("reason") or "No Intel GPU found")
            return
        set_text(self.sub, f"{ig.get('driver')} · {ig.get('pdev')} · shares system memory")
        g = self.stats
        g.set("util", fmt.pct(st.last("igpu.util"), 1))
        g.set("awake", fmt.pct(st.last("igpu.awake"), 1))
        g.set("rc6", fmt.pct(st.last("igpu.rc6"), 1))
        act = st.last("igpu.freq")
        g.set("freq", ("0 MHz (asleep)" if act == 0 else f"{act:.0f} MHz") if act is not None
              else "–")
        g.set("req", fmt.num(st.last("igpu.freq_req")) + " MHz")
        g.set("range", f"{ig.get('min_mhz') or '–'} – {ig.get('rp0_mhz') or ig.get('max_mhz')}"
                       " MHz")
        g.set("clients", str(ig.get("clients", "–")))
        thr = ig.get("throttled")
        g.set("throttle", "–" if thr is None else ("yes" if thr else "no"))
        for e in ig.get("engines") or []:
            if e not in self._engines_shown:
                self._engines_shown.add(e)
                self.engines.add(e, ENGINE_NAMES.get(e, e.title()))
            self.engines.set(e, fmt.pct(st.last(f"igpu.engine.{e}"), 1))
        per = ig.get("per_pid") or {}
        rows = sorted(((pid, v) for pid, v in per.items() if v >= 0.05), key=lambda x: -x[1])
        self.procs.set_rows([(pid, [f"{v:.1f} %"]) for pid, v in rows[:12]])
        self.redraw()


class NvidiaGpuPage(MonitorPage):
    key, title = "dgpu", "NVIDIA graphics"

    def __init__(self, win) -> None:
        super().__init__(win)
        dg = self.hub.sampler.dgpu
        self.name, self.sub, self.pills = self.header(dg.name(), "")
        self.state_label = label("", ("pill",))
        self.pills.append(self.state_label)
        self.sleep_note = note("", "monitor-callout")
        self.body.append(self.sleep_note)
        p = self.panel("Utilisation", suffix=legend([("GPU", "primary"),
                                                     ("memory controller", "gold")]))
        p.append(self.chart([Line("dgpu.util", "GPU"),
                             Line("dgpu.mem_util", "memory", "gold", fill=False)],
                            y_fixed=(0, 100), fmt=pct, height=170))
        self.stats = StatGrid(2, 4)
        for key, cap, tip in (("util", "GPU busy", None), ("vram", "VRAM used", None),
                              ("gr", "Graphics clock", None), ("mem", "Memory clock", None),
                              ("temp", "Temperature", None), ("power", "Power · limit", None),
                              ("pstate", "Performance state", "P0 fastest … P8/P12 idle"),
                              ("fan", "Fan", None), ("enc", "Encoder · decoder", None),
                              ("driver", "Driver", None), ("runtime", "Runtime PM", None),
                              ("source", "Source", None)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        vp = self.panel("Video memory")
        self.vram_chart = self.chart([Line("dgpu.vram", "VRAM", "slate", scale=1 / (1 << 30))],
                                     floor=0, min_span=0.5, fmt=lambda v: f"{v:g} GiB",
                                     height=120)
        vp.append(self.vram_chart)
        cp = self.panel("Clock, temperature and power")
        cp.append(legend([("graphics MHz", "primary")]))
        cp.append(self.chart([Line("dgpu.gr_mhz", "graphics")], floor=0, min_span=300,
                             fmt=lambda v: f"{v:.0f}", height=110))
        cp.append(legend([("°C", "rose"), ("W", "gold")]))
        cp.append(self.chart([Line("dgpu.temp", "°C", "rose", fill=False),
                              Line("dgpu.power", "W", "gold", fill=False)], floor=0,
                             min_span=20, fmt=lambda v: f"{v:.0f}", height=110))
        pp = self.panel("Processes using the dGPU")
        self.procs = ProcTable(self.hub.root, ["type", "VRAM", "SM"])
        pp.append(self.procs)
        pp.append(note("A process listed here with a few MiB (often the compositor) holds a "
                       "context on the GPU; that alone can keep it from sleeping."))

    def update(self) -> None:
        st = self.hub.store
        dg = self.hub.info.get("dgpu") or {}
        g = self.stats
        if not dg.get("present") or not dg.get("backend"):
            set_text(self.sub, dg.get("reason") or "No NVIDIA GPU found")
            set_text(self.sleep_note, dg.get("reason") or "")
            return
        set_text(self.name, dg.get("name") or "NVIDIA graphics")
        stt = dg.get("state")
        rs, ps = dg.get("runtime"), dg.get("power_state")
        set_text(self.sub, f"{dg.get('addr')} · read through "
                 f"{'NVML' if dg.get('backend') == 'nvml' else dg.get('backend')}")
        g.set("runtime", f"{rs or 'no runtime PM'} · {ps or '–'}")
        g.set("source", {"nvml": "NVML (libnvidia-ml)", "nvidia-smi": "nvidia-smi (every 5 s)",
                         "fake": "fake tree"}.get(dg.get("backend"), "–"))
        if stt in ("sleeping", "idle-released"):
            set_text(self.state_label, f"Sleeping ({ps or 'D3'})" if stt == "sleeping"
                     else "Idle, released")
            self.sleep_note.set_visible(True)
            if stt == "sleeping":
                set_text(self.sleep_note, f"The GPU is powered down ({ps or 'D3'}). Reading "
                         "its sensors would wake it and cost battery, so nothing is queried "
                         "until something else wakes it. Graphs resume then.")
            else:
                left = dg.get("next_check_s") or 0
                age = dg.get("last_age_s") or 0
                set_text(self.sleep_note, "The GPU is awake but idle. Polling is paused so "
                         f"the driver can put it to sleep; next check in {left:.0f} s. "
                         f"Figures below are from the last check, {age:.0f} s ago.")
            last = dg.get("last_q") if stt == "idle-released" else None
            if last:
                self._fill(last, dg.get("static") or {}, st)
            else:
                for k in ("util", "vram", "gr", "mem", "temp", "power", "pstate", "fan",
                          "enc"):
                    g.set(k, "–")
            self.procs.set_rows([])
            self.redraw()
            return
        set_text(self.state_label, "Awake")
        self.sleep_note.set_visible(False)
        self._fill(dg.get("q") or {}, dg.get("static") or {}, st)
        rows = []
        for pid, pr in sorted((dg.get("procs") or {}).items(),
                              key=lambda kv: -(kv[1].get("vram") or 0)):
            rows.append((pid, [pr.get("type") or "", fmt.bytes_(pr.get("vram")),
                               fmt.pct(pr.get("sm"))]))
        self.procs.set_rows(rows[:16])
        self.redraw()

    def _fill(self, q: dict, sta: dict, st) -> None:
        g = self.stats
        g.set("util", fmt.pct(q.get("util")))
        used, total = q.get("vram_used"), q.get("vram_total")
        g.set("vram", f"{fmt.bytes_(used)} of {fmt.bytes_(total, 0)}" if used is not None
              else "–")
        if total:
            self.vram_chart.y_fixed = (0, total / (1 << 30))
        g.set("gr", f"{fmt.num(q.get('gr_mhz'))} / {fmt.num(sta.get('max_gr_mhz'))} MHz")
        g.set("mem", f"{fmt.num(q.get('mem_mhz'))} MHz")
        g.set("temp", fmt.temp(q.get("temp")))
        g.set("power", f"{fmt.watts(q.get('power'))} · {fmt.watts(q.get('power_limit'), 0)}")
        g.set("pstate", f"P{q['pstate']}" if q.get("pstate") is not None else "–")
        fan = q.get("fan")
        gf = st.last("fan.gpu")
        g.set("fan", f"{fan} %" if fan is not None else
              (f"{fmt.rpm(gf)} (laptop fan)" if gf is not None else "not reported"),
              tooltip=None if fan is not None else
              "Laptop GPUs report no fan to NVML; this is the GPU fan from the ASUS hwmon")
        g.set("enc", f"{fmt.pct(q.get('enc'))} · {fmt.pct(q.get('dec'))}")
        g.set("driver", sta.get("driver_version") or "–")
