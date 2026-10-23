"""Network: per-interface receive/send rates, totals and link details."""
from __future__ import annotations

from gi.repository import Gtk

from ...telemetry import fmt
from ..charts import Line, legend
from .monitor_base import MonitorPage, Panel, StatGrid, note, set_text


class NetworkPage(MonitorPage):
    key, title = "network", "Network"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, pills = self.header("Network", "")
        self.virtual = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.virtual.connect("notify::active", lambda *_: self.update())
        box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        box.append(Gtk.Label(label="Virtual interfaces"))
        box.append(self.virtual)
        pills.append(box)
        tp = self.panel("All physical interfaces", suffix=legend([("receive", "primary"),
                                                                  ("send", "gold")]))
        tp.append(self.chart([Line("net.rx", "receive"), Line("net.tx", "send", "gold",
                                                              fill=False)],
                             floor=0, min_span=1e5, fmt=fmt.axis_rate, height=170))
        self.ifaces: dict[str, tuple] = {}
        self.box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                               min_children_per_line=1, max_children_per_line=2,
                               column_spacing=14, row_spacing=14)
        self.body.append(self.box)
        self.body.append(note("Rates from /proc/net/dev byte counters. Virtual interfaces "
                              "(VPN tunnels, bridges, containers) carry traffic that also "
                              "crosses a physical link, so they are left out of the total."))

    def _panel(self, name: str, meta: dict) -> tuple:
        p = Panel(f"{name} · {meta['kind']}")
        p.append(self.chart([Line(f"net.{name}.rx", "receive"),
                             Line(f"net.{name}.tx", "send", "gold", fill=False)],
                            floor=0, min_span=1e4, fmt=fmt.axis_rate, height=110))
        g = StatGrid(2, 3)
        for key, cap in (("rx", "Receive"), ("tx", "Send"), ("state", "State"),
                         ("rxt", "Received since boot"), ("txt", "Sent since boot"),
                         ("link", "Link")):
            g.add(key, cap)
        p.append(g)
        self.box.append(p)
        p.get_parent().set_focusable(False)
        return p, g

    def update(self) -> None:
        st = self.hub.store
        net = self.hub.info.get("net") or {}
        ifs = net.get("ifaces") or {}
        show_virtual = self.virtual.get_active()
        n_phys = 0
        for name, m in ifs.items():
            if not m["virtual"]:
                n_phys += 1
            if name not in self.ifaces:
                self.ifaces[name] = self._panel(name, m)
            p, g = self.ifaces[name]
            p.get_parent().set_visible(show_virtual or not m["virtual"])
            g.set("rx", fmt.rate(st.last(f"net.{name}.rx")))
            g.set("tx", fmt.rate(st.last(f"net.{name}.tx")))
            g.set("state", m.get("state") or "–")
            g.set("rxt", fmt.bytes_(m.get("rx_total")))
            g.set("txt", fmt.bytes_(m.get("tx_total")))
            link = "–"
            if m.get("wifi"):
                q, sig = m["wifi"]
                link = f"{sig:.0f} dBm · quality {q:.0f}/70"
            elif m.get("speed_mbps"):
                link = f"{m['speed_mbps']} Mbit/s"
            g.set("link", link)
        set_text(self.sub, f"{n_phys} physical · {len(ifs) - n_phys} virtual · receive "
                 f"{fmt.rate(st.last('net.rx'))} · send {fmt.rate(st.last('net.tx'))}")
        self.redraw()
