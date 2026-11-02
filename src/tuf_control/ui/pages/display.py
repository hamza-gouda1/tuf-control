# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..display_config import make_display_config
from ..tasks import run_bg
from ..viewmodels import parse_display_state
from .base import Page, info_row, prefs_page, value_row


class DisplayPage(Page):
    key, title = "display", "Display"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.dc = make_display_config()
        self.state = None
        self.monitor = None
        self._quiet = False

        self.panel = Adw.PreferencesGroup(title="Built-in display")
        self.res_row, self.res_val = value_row("Resolution")
        self.rate_row = Adw.ActionRow(title="Refresh rate",
                                      subtitle="Higher is smoother, lower saves battery")
        self.rate_box = Gtk.Box(valign=Gtk.Align.CENTER)
        self.rate_box.add_css_class("linked")
        self.rate_row.add_suffix(self.rate_box)
        self.overdrive = Adw.SwitchRow(title="Panel overdrive",
                                       subtitle="Faster pixel response, less motion blur")
        self.overdrive.connect("notify::active", self._overdrive)
        for r in (self.res_row, self.rate_row, self.overdrive):
            self.panel.add(r)
        self.msg = info_row("Display settings unavailable", "")
        self.msg.set_visible(False)
        self.panel.add(self.msg)

        self.vrr = Adw.PreferencesGroup(title="Variable refresh rate")
        self.vrr_row = info_row(
            "Set in GNOME", "VRR is experimental in GNOME 46+. Enable the "
            "“variable-refresh-rate” Mutter feature, then choose it in Settings › Displays.",
            "preferences-desktop-display-symbolic")
        self.vrr.add(self.vrr_row)
        self.set_child(prefs_page(self.panel, self.vrr))

    def refresh(self) -> None:
        self.dc.get_state(self._got_state, self._state_failed)
        run_bg(lambda: self.ctl.get_attributes()[0], done=self._attrs,
               error=lambda e: self._attrs(None, e))

    def _state_failed(self, exc) -> None:
        self.rate_row.set_sensitive(False)
        self.msg.set_subtitle("The desktop's display service did not report the screen's "
                              "current mode, so the controls are off. Restart TUF Control "
                              "to try again.")
        self.msg.set_visible(True)
        self.msg.set_tooltip_text(str(exc))

    def _got_state(self, state) -> None:
        self.state = state
        mons = parse_display_state(state)
        self.monitor = next((m for m in mons if m.builtin), mons[0] if mons else None)
        child = self.rate_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.rate_box.remove(child)
            child = nxt
        m = self.monitor
        if m is None or m.current is None:
            self.rate_row.set_sensitive(False)
            self.res_val.set_label("–")
            return
        self.panel.set_title(m.name if not m.builtin else "Built-in display")
        self.res_val.set_label(f"{m.current.width} × {m.current.height}")
        first = None
        self._quiet = True
        for mode in m.refresh_options():
            b = Gtk.ToggleButton(label=mode.label)
            b.add_css_class("numeric")
            if first:
                b.set_group(first)
            first = first or b
            b.set_active(mode.current)
            b.connect("toggled", self._pick_rate, mode)
            self.rate_box.append(b)
        self._quiet = False
        self.rate_row.set_sensitive(True)
        self.vrr_row.set_title("Supported by this panel" if m.vrr_supported else "Set in GNOME")

    def _pick_rate(self, b, mode) -> None:
        if self._quiet or not b.get_active() or self.state is None or self.monitor is None:
            return
        prev = self.monitor.current

        def ok(_res):
            self.win.toast(f"Refresh rate set to {mode.label}",
                           undo=(lambda: self._apply(prev)) if prev else None)
            self.refresh()

        def fail(exc):
            self.win.toast_error(exc)
            self.refresh()

        self.dc.set_mode(self.state, self.monitor.connector, mode.id, ok, fail)

    def _apply(self, mode) -> None:
        if self.state and self.monitor:
            self.dc.set_mode(self.state, self.monitor.connector, mode.id,
                             lambda _r: self.refresh(), self.win.toast_error)

    def _attrs(self, attrs, exc=None) -> None:
        od = next((a for a in attrs or [] if a.name == "panel_overdrive"), None)
        self._quiet = True
        if od is None:
            self.overdrive.set_sensitive(False)
            self.overdrive.set_subtitle("Panel overdrive is not available on this laptop" +
                                        (f" (the firmware settings could not be read: {exc})"
                                         if exc else ""))
        else:
            self.overdrive.set_sensitive(True)
            self.overdrive.set_active(str(od.current) == "1")
        self._quiet = False

    def _overdrive(self, *_):
        if self._quiet:
            return
        on = self.overdrive.get_active()
        self.win.run_write(lambda: self.ctl.set_attribute("panel_overdrive", 1 if on else 0),
                           f"Panel overdrive {'on' if on else 'off'}", after=None,
                           on_error=self.refresh)
