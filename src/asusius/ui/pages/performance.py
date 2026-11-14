# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ...core import Profile
from ..tasks import run_bg
from ..viewmodels import (
    PROFILE_INFO,
    PROFILE_ORDER,
    fmt_num,
    fmt_temp,
    power_limits_view,
    snap_to_step,
)
from ..widgets import label
from .base import Page, a11y_name, button, prefs_page


class ProfileCards(Gtk.Box):
    """Three large toggle cards; emits on_pick(profile) on user clicks only."""

    def __init__(self, on_pick) -> None:
        super().__init__(spacing=10, homogeneous=True)
        self.on_pick = on_pick
        self.buttons: dict[Profile, Gtk.ToggleButton] = {}
        self._quiet = False
        group = None
        for prof in PROFILE_ORDER:
            title, sub, icon = PROFILE_INFO[prof]
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            top = Gtk.Box(spacing=8)
            badge = Gtk.Box()
            badge.add_css_class("card-icon-badge")
            badge.append(Gtk.Image.new_from_icon_name(icon))
            top.append(badge)
            top.append(Gtk.Box(hexpand=True))
            tick = Gtk.Image.new_from_icon_name("object-select-symbolic")
            tick.add_css_class("card-tick")
            top.append(tick)
            box.append(top)
            box.append(label(title, ("card-title",), margin_top=6))
            box.append(label(sub, ("card-sub",), wrap=True, max_width_chars=22))
            b = Gtk.ToggleButton(child=box)
            b.add_css_class("profile-card")
            b.tick = tick
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", self._toggled, prof)
            self.buttons[prof] = b
            self.append(b)
        self._sync_ticks()

    def _sync_ticks(self):
        for b in self.buttons.values():
            b.tick.set_opacity(1.0 if b.get_active() else 0.0)

    def _toggled(self, b, prof):
        self._sync_ticks()
        if b.get_active() and not self._quiet:
            self.on_pick(prof)

    def set_profile(self, prof: Profile | None) -> None:
        self._quiet = True
        if prof in self.buttons:
            self.buttons[prof].set_active(True)
        self._quiet = False
        self._sync_ticks()


class PerformancePage(Page):
    key, title = "performance", "Performance"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.cards = ProfileCards(lambda p: self.win.request_profile(p))
        g1 = Adw.PreferencesGroup(title="Profile",
                                  description="Sets fan behaviour and firmware power targets.")
        g1.add(self.cards)
        live_strip = Gtk.Box(spacing=8, margin_top=10, halign=Gtk.Align.START)
        self.live_temp_pill = label("", ("live-pill", "numeric"), visible=False)
        self.live_power_pill = label("", ("live-pill", "numeric"), visible=False)
        self.live_gov_pill = label("", ("live-pill",), visible=False)
        for p in (self.live_temp_pill, self.live_power_pill, self.live_gov_pill):
            live_strip.append(p)
        g1.add(live_strip)
        self.limits = Adw.PreferencesGroup(title="Power limits")
        self.limit_rows: list = []
        self.scales: dict[str, tuple] = {}
        self.page = prefs_page(g1, self.limits)
        self.set_child(self.page)

    def set_profile(self, prof) -> None:
        self.cards.set_profile(prof)

    def _clear_limits(self):
        for r in self.limit_rows:
            self.limits.remove(r)
        self.limit_rows = []
        self.limits.set_header_suffix(None)

    def refresh(self) -> None:
        self.cards.set_profile(self.win.profile)
        self._sync_live_chips()

        def load():
            try:
                lims = self.ctl.get_limits()[0]
            except Exception:
                lims = None
            return lims

        run_bg(load, done=self._show_limits)

    def on_telemetry(self, t) -> None:
        self._sync_live_chips(t)

    def _sync_live_chips(self, t=None) -> None:
        if t is None:
            t = self.win.telemetry()
        if t.cpu_temp_c is not None:
            self.live_temp_pill.set_label(f"CPU {fmt_temp(t.cpu_temp_c)}")
            self.live_temp_pill.set_visible(True)
        if t.cpu_power_w is not None:
            self.live_power_pill.set_label(f"Package {fmt_num(t.cpu_power_w, 1)} W")
            self.live_power_pill.set_visible(True)
        try:
            gov = self.win.hub.sampler.cpu.policy().get("governor")
        except Exception:
            gov = None
        if gov:
            self.live_gov_pill.set_label(f"Governor: {gov}")
            self.live_gov_pill.set_visible(True)

    def _show_limits(self, lims) -> None:
        self._clear_limits()
        view = power_limits_view(self.cap("power_limits"), lims)
        if not view.available:
            self.limits.set_description(None)
            exp = Adw.ExpanderRow(
                title="Why aren't power limits shown?",
                subtitle="Firmware did not expose writable min/max bounds")
            exp.add_prefix(Gtk.Image.new_from_icon_name("security-high-symbolic"))
            exp.set_tooltip_text(view.reason or None)
            retry = button("Retry", "flat", on_click=self.refresh)
            retry.set_valign(Gtk.Align.CENTER)
            exp.add_suffix(retry)
            detail = Adw.ActionRow(
                subtitle="Asusius only exposes power sliders when the kernel and firmware "
                         "provide verified safe limits, rather than guessing values. "
                         "If asusd was just started, click Retry.")
            detail.set_subtitle_lines(0)
            exp.add_row(detail)
            self.limits.add(exp)
            self.limit_rows.append(exp)
            return
        self.limits.set_description("Bounds come from the firmware. Changes apply after you "
                                    "confirm.")
        self.scales = {}
        for lr in view.rows:
            row = Adw.PreferencesRow(activatable=False, title=lr.title)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=10,
                           margin_bottom=6, margin_start=12, margin_end=12)
            top = Gtk.Box(spacing=12)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            texts.append(label(lr.title, wrap=True))
            sub = label(lr.subtitle, ("caption", "dim-label"), wrap=True)
            texts.append(sub)
            top.append(texts)
            adj = Gtk.Adjustment(value=lr.value, lower=lr.min, upper=lr.max,
                                 step_increment=lr.step, page_increment=lr.step * 5)
            scale = Gtk.Scale(adjustment=adj, draw_value=False, hexpand=True)
            a11y_name(scale, lr.title)
            if lr.default is not None:
                scale.add_mark(lr.default, Gtk.PositionType.BOTTOM, None)
            val = label(f"{lr.value} {lr.unit}", ("scale-value",), xalign=1.0)

            def changed(a, lr=lr, val=val):
                v = snap_to_step(a.get_value(), lr.min, lr.max, lr.step)
                if v != a.get_value():
                    GLib.idle_add(a.set_value, v)
                val.set_label(f"{v} {lr.unit}")
                self._dirty()

            adj.connect("value-changed", changed)
            val.set_valign(Gtk.Align.CENTER)
            top.append(val)
            body.append(top)
            body.append(scale)
            row.set_child(body)
            self.scales[lr.name] = (lr, adj)
            self.limits.add(row)
            self.limit_rows.append(row)
        box = Gtk.Box(spacing=6)
        self.reset_btn = button("Defaults", "flat", on_click=self._defaults)
        self.apply_btn = button("Apply…", "suggested-action", on_click=self._apply)
        box.append(self.reset_btn)
        box.append(self.apply_btn)
        self.limits.set_header_suffix(box)
        self._dirty()

    def _changes(self):
        out = []
        for name, (lr, adj) in self.scales.items():
            v = snap_to_step(adj.get_value(), lr.min, lr.max, lr.step)
            if v != lr.value:
                out.append((lr, v))
        return out

    def _dirty(self):
        if hasattr(self, "apply_btn"):
            self.apply_btn.set_sensitive(bool(self._changes()))

    def _defaults(self):
        for lr, adj in self.scales.values():
            if lr.default is not None:
                adj.set_value(lr.default)

    def _apply(self):
        changes = self._changes()
        if not changes:
            return
        body = "\n".join(f"{lr.title}: {lr.value} → {v} {lr.unit}" for lr, v in changes)
        body += "\n\nValues stay within the firmware's limits. Higher limits mean more heat."

        def go():
            def work():
                return [self.ctl.set_limit(lr.name, v) for lr, v in changes]
            self.win.run_write(work, "Power limits applied", after=self.refresh)

        self.win.confirm("Apply power limits?", body, "Apply", go)
