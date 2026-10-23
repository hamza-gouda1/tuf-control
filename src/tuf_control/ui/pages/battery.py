from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ...core import Capability
from ..tasks import run_bg
from ..viewmodels import (
    CHARGE_PRESETS,
    CHARGE_RANGE,
    charge_limit_hint,
    fmt_num,
    health_label,
    validate_charge_limit,
)
from ..widgets import label
from .base import Page, button, info_row, prefs_page, value_row


class BatteryPage(Page):
    key, title = "battery", "Battery"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.current: int | None = None
        self._quiet = False
        self._pending = None

        self.limit = Adw.PreferencesGroup(
            title="Charge limit",
            description="Stopping below 100% slows battery wear when you are mostly plugged in.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("tile")
        top = Gtk.Box(spacing=12)
        self.big = label("–", ("big-scale-value",))
        top.append(self.big)
        self.hint = label("", ("stat-sub",), wrap=True, hexpand=True, valign=Gtk.Align.CENTER)
        top.append(self.hint)
        self.presets = Gtk.Box(valign=Gtk.Align.CENTER)
        self.presets.add_css_class("linked")
        for p in CHARGE_PRESETS:
            b = Gtk.Button(label=f"{p}%")
            b.add_css_class("numeric")
            b.connect("clicked", lambda _b, p=p: self.adj.set_value(p))
            self.presets.append(b)
        top.append(self.presets)
        box.append(top)
        lo, hi = CHARGE_RANGE
        self.adj = Gtk.Adjustment(value=80, lower=lo, upper=hi, step_increment=1,
                                  page_increment=5)
        self.scale = Gtk.Scale(adjustment=self.adj, draw_value=False, hexpand=True)
        for p in CHARGE_PRESETS:
            self.scale.add_mark(p, Gtk.PositionType.BOTTOM, None)
        self.adj.connect("value-changed", self._changed)
        box.append(self.scale)
        self.limit.add(box)
        self.oneshot = Adw.ActionRow(title="Charge to 100% once",
                                     subtitle="For a long trip. The limit returns afterwards.")
        self.oneshot.add_suffix(button("Charge now", on_click=self._oneshot))
        self.limit_err = info_row("Charge limit unavailable", "")
        self.limit_err.set_visible(False)
        g_extra = Adw.PreferencesGroup()
        g_extra.add(self.oneshot)
        g_extra.add(self.limit_err)

        self.health = Adw.PreferencesGroup(title="Health")
        self.h_row, self.h_val = value_row("Health", "Full charge vs. design capacity")
        self.level = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER,
                                  width_request=120)
        self.h_row.add_suffix(self.level)
        self.cap_row, self.cap_val = value_row("Capacity")
        self.cyc_row, self.cyc_val = value_row("Charge cycles")
        self.now_row, self.now_val = value_row("Right now")
        for r in (self.h_row, self.cap_row, self.cyc_row, self.now_row):
            self.health.add(r)
        self.set_child(prefs_page(self.limit, g_extra, self.health))

    def refresh(self) -> None:
        run_bg(lambda: self.ctl.get_battery_threshold()[0], done=self._got_limit,
               error=self._limit_failed)
        run_bg(lambda: self.ctl.read(Capability.BATTERY_HEALTH)[0], done=self._got_health,
               error=lambda e: self.h_val.set_label("Unavailable"))

    def _limit_failed(self, exc) -> None:
        self.limit_err.set_subtitle(str(exc))
        self.limit_err.set_visible(True)
        self.scale.set_sensitive(False)
        self.presets.set_sensitive(False)

    def _got_limit(self, value: int) -> None:
        self.current = value
        self._quiet = True
        self.adj.set_value(value)
        self._quiet = False
        self._label(value)

    def _label(self, v: int) -> None:
        self.big.set_label(f"{v}%")
        self.hint.set_label(charge_limit_hint(v))

    def _changed(self, adj) -> None:
        v = int(round(adj.get_value()))
        self._label(v)
        if self._quiet:
            return
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(700, self._commit)

    def _commit(self):
        self._pending = None
        try:
            v = validate_charge_limit(self.adj.get_value())
        except Exception as exc:
            self.win.toast_error(exc)
            return False
        if v != self.current:
            def after(_=None, v=v):
                self.current = v
            self.win.run_write(lambda: self.ctl.set_battery_threshold(v),
                               f"Charge limit set to {v}%", after=after,
                               on_error=self.refresh)
        return False

    def _oneshot(self) -> None:
        self.win.run_write(self.ctl.one_shot_full_charge,
                           "Charging to 100% this time", after=self.refresh)

    def _got_health(self, h) -> None:
        hp = h.health_percent
        self.h_val.set_label(f"{fmt_num(hp)}% · {health_label(hp)}")
        self.level.set_value(hp)
        self.cap_val.set_label(f"{h.energy_full_uwh / 1e6:.1f} of "
                               f"{h.energy_full_design_uwh / 1e6:.1f} Wh")
        self.cyc_val.set_label("Not reported" if not h.cycle_count else str(h.cycle_count))

    def on_telemetry(self, t) -> None:
        st = (t.battery_status or "–").lower()
        self.now_val.set_label(f"{fmt_num(t.battery_percent)}% · {st} · "
                               f"{fmt_num(t.battery_power_w, 1)} W")
