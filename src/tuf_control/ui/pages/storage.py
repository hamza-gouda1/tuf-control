"""Storage: per-disk throughput, busy time, temperature; mounted filesystems."""
from __future__ import annotations

from gi.repository import Gtk, Pango

from ...telemetry import fmt
from ..charts import Line, Meter, legend
from ..widgets import label
from .monitor_base import MonitorPage, Panel, StatGrid, note, set_text


class StoragePage(MonitorPage):
    key, title = "storage", "Storage"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, _ = self.header("Storage", "")
        self.disks: dict[str, tuple] = {}
        self.disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.body.append(self.disk_box)
        self.fs = self.panel("File systems")
        self.fs_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.fs.append(self.fs_rows)
        self._fs_key = None
        self.body.append(note("Rates come from /proc/diskstats (512-byte sectors) over the "
                              "sampling interval; busy is the share of time the device had "
                              "I/O in flight. File systems refresh every 10 s."))

    def on_shown(self) -> None:
        self.hub.sampler.disk.want_mounts = True
        super().on_shown()

    def on_hidden(self) -> None:
        self.hub.sampler.disk.want_mounts = False

    def _disk_panel(self, name: str, meta: dict) -> tuple:
        title = f"{name} · {meta.get('model') or meta.get('kind')}"
        p = Panel(title, legend([("read", "primary"), ("write", "gold")]))
        p.append(self.chart([Line(f"disk.{name}.read", "read"),
                             Line(f"disk.{name}.write", "write", "gold", fill=False)],
                            floor=0, min_span=1e5, fmt=fmt.axis_rate, height=150))
        p.append(legend([("busy %", "slate")]))
        p.append(self.chart([Line(f"disk.{name}.busy", "busy", "slate")], y_fixed=(0, 100),
                            fmt=lambda v: f"{v:.0f}%", height=80))
        g = StatGrid(2, 4)
        for key, cap in (("read", "Read"), ("write", "Write"), ("busy", "Busy"),
                         ("temp", "Temperature"), ("size", "Capacity"), ("kind", "Type"),
                         ("rt", "Read since boot"), ("wt", "Written since boot")):
            g.add(key, cap)
        p.append(g)
        self.disk_box.append(p)
        return p, g

    def update(self) -> None:
        st = self.hub.store
        d = self.hub.info.get("disk") or {}
        disks = d.get("disks") or {}
        sens = self.hub.info.get("sensors") or {}
        for name, meta in disks.items():
            if name not in self.disks:
                self.disks[name] = self._disk_panel(name, meta)
            _p, g = self.disks[name]
            g.set("read", fmt.rate(st.last(f"disk.{name}.read")))
            g.set("write", fmt.rate(st.last(f"disk.{name}.write")))
            g.set("busy", fmt.pct(st.last(f"disk.{name}.busy"), 1))
            labels = (sens.get("nvme") or {}).get(name) or []
            temps = [st.last(f"temp.disk.{name}.{i}") for i in range(len(labels))]
            g.set("temp", " · ".join(fmt.temp(t) for t in temps if t is not None) or
                  "no sensor", tooltip=" · ".join(labels) or None)
            g.set("size", fmt.bytes_(meta.get("size")))
            g.set("kind", meta.get("kind") or "–")
            tot = (d.get("totals") or {}).get(name)
            g.set("rt", fmt.bytes_(tot[0]) if tot else "–")
            g.set("wt", fmt.bytes_(tot[1]) if tot else "–")
        set_text(self.sub, f"{len(disks)} physical disk{'s' if len(disks) != 1 else ''} · "
                 f"read {fmt.rate(st.last('disk.read'))} · write "
                 f"{fmt.rate(st.last('disk.write'))}")
        mounts = d.get("mounts")
        if mounts is not None:
            key = tuple((m["mount"], m["used"] >> 20) for m in mounts)
            if key != self._fs_key:
                self._fs_key = key
                self._fill_fs(mounts)
        self.redraw()

    def _fill_fs(self, mounts: list[dict]) -> None:
        child = self.fs_rows.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.fs_rows.remove(child)
            child = nxt
        if not mounts:
            self.fs_rows.append(note("No block-device file systems mounted."))
        for m in mounts:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            top = Gtk.Box(spacing=10)
            top.append(label(m["mount"], ("fs-mount",), ellipsize=Pango.EllipsizeMode.MIDDLE))
            top.append(label(f"{m['fstype']} · {m['source']}", ("dim-label", "stat-sub"),
                             hexpand=True, ellipsize=Pango.EllipsizeMode.END))
            frac = m["used"] / m["size"] if m["size"] else 0
            top.append(label(f"{fmt.bytes_(m['used'])} of {fmt.bytes_(m['size'])} · "
                             f"{fmt.bytes_(m['avail'])} free", ("numeric", "stat-sub"),
                             xalign=1.0))
            row.append(top)
            meter = Meter(height=8)
            meter.set_hexpand(True)
            meter.set_value(frac, "rose" if frac > 0.9 else "primary")
            row.append(meter)
            self.fs_rows.append(row)
