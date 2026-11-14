# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ...core import Profile
from ..tasks import run_bg
from ..viewmodels import (
    FAN_PRESETS,
    PROFILE_INFO,
    PROFILE_ORDER,
    FanCurveModel,
    apply_fan_preset,
    fmt_rpm,
    fmt_temp,
)
from ..widgets import FAN_CURVE_KEYBOARD_HELP, FanCurveCanvas, label
from .base import Page, button, info_row, prefs_page


class FansPage(Page):
    key, title = "fans", "Fan curves"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.models: dict[str, FanCurveModel] = {}
        self.fan = "CPU"
        self.profile: Profile | None = None
        self.follow = True  # track the active profile until the user picks another
        self._busy = False

        self.group = Adw.PreferencesGroup(
            title="Fan curve",
            description="Drag a point to adjust fan speed at each temperature. "
                        + FAN_CURVE_KEYBOARD_HELP)
        self.switcher = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        self.switcher.add_css_class("linked")
        self.fan_buttons = {}
        first = None
        for fan in ("CPU", "GPU"):
            b = Gtk.ToggleButton(label=fan)
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._pick_fan, fan)
            self.fan_buttons[fan] = b
            self.switcher.append(b)
        self.group.set_header_suffix(self.switcher)

        self.profile_row = Adw.ComboRow(title="Profile",
                                        subtitle="Each profile keeps its own curves")
        self.profile_row.set_model(Gtk.StringList.new([PROFILE_INFO[p][0] for p in PROFILE_ORDER]))
        self.profile_row.connect("notify::selected", self._pick_profile)
        self.enable_row = Adw.SwitchRow(title="Use custom curve",
                                        subtitle="Off: the firmware's automatic curve")
        self.enable_row.connect("notify::active", self._toggle_enabled)
        self.group.add(self.profile_row)
        self.group.add(self.enable_row)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12)
        frame.add_css_class("curve-frame")

        # live RPM / temp strip + quick presets right at the top of the curve card
        top_bar = Gtk.Box(spacing=10, margin_start=14, margin_end=14, margin_top=12)
        self.live_strip = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                      min_children_per_line=1, max_children_per_line=4,
                                      column_spacing=6, row_spacing=4, hexpand=True)
        self.live_pills: dict[str, Gtk.Label] = {}
        top_bar.append(self.live_strip)
        frame.append(top_bar)

        presets_bar = Gtk.Box(spacing=6, margin_start=14, margin_end=14)
        presets_bar.append(label("Presets", ("section-caption",), valign=Gtk.Align.CENTER))
        self.preset_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                       min_children_per_line=2, max_children_per_line=4,
                                       column_spacing=6, row_spacing=4)
        for name in FAN_PRESETS:
            pb = Gtk.Button(label=name, tooltip_text=f"Load {name} fan curve preset (preview before applying)")
            pb.add_css_class("preset-chip")
            pb.connect("clicked", lambda _b, n=name: self._apply_preset(n))
            self.preset_flow.append(pb)
            ch = pb.get_parent()
            if ch is not None:
                ch.set_focusable(False)
        presets_bar.append(self.preset_flow)
        frame.append(presets_bar)

        self.readout = label("", ("curve-readout", "numeric"), margin_start=14, margin_top=2)
        frame.append(self.readout)
        self.canvas = FanCurveCanvas(on_change=self._changed, on_select=self._selected)
        self.canvas.set_margin_end(4)
        frame.append(self.canvas)
        self.status = info_row("Fan curves unavailable", "")
        self.status.set_visible(False)
        retry_btn = button("Retry", "flat", on_click=self.refresh)
        retry_btn.set_valign(Gtk.Align.CENTER)
        self.status.add_suffix(retry_btn)
        foot = Gtk.Box(spacing=8, margin_start=10, margin_end=10, margin_bottom=10,
                       halign=Gtk.Align.END)
        self.foot = foot
        self.reset_btn = button("Factory curve…", "flat", on_click=self._reset)
        self.revert_btn = button("Revert", "flat", on_click=self._revert)
        self.apply_btn = button("Apply…", "suggested-action", on_click=self._apply)
        for b in (self.reset_btn, self.revert_btn, self.apply_btn):
            foot.append(b)
        frame.append(foot)
        self.group.add(frame)
        self.frame = frame

        self.page = prefs_page(self._status_group(), self.group)
        self.set_child(self.page)
        self.fan_buttons["CPU"].set_active(True)

    def fit_width(self, level: int) -> None:
        """Phone width: the three buttons stack instead of needing a wider window."""
        narrow = level == 3
        self.foot.set_orientation(Gtk.Orientation.VERTICAL if narrow
                                  else Gtk.Orientation.HORIZONTAL)
        self.foot.set_halign(Gtk.Align.FILL if narrow else Gtk.Align.END)

    def _status_group(self):
        g = Adw.PreferencesGroup()
        g.add(self.status)
        return g

    # -- data -------------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.profile is None:
            self.profile = self.win.profile or Profile.BALANCED
            self._quiet_select(self.profile)
        prof = self.profile

        def load():
            return self.ctl.get_fan_curves(prof)[0]

        run_bg(load, done=self._loaded, error=self._failed)

    def _quiet_select(self, prof):
        self._busy = True
        self.profile_row.set_selected(PROFILE_ORDER.index(prof))
        self._busy = False

    def _loaded(self, curves) -> None:
        self.models = {name.upper(): FanCurveModel(c) for name, c in curves.items()}
        self.status.set_visible(False)
        self.frame.set_sensitive(True)
        self.enable_row.set_sensitive(True)
        for fan, b in self.fan_buttons.items():
            b.set_sensitive(fan in self.models)
        if self.fan not in self.models and self.models:
            self.fan = next(iter(self.models))
        self._show()

    def _failed(self, exc) -> None:
        self.models = {}
        self.canvas.set_model(None)
        self.frame.set_sensitive(False)
        self.enable_row.set_sensitive(False)
        msg = str(exc)
        if "active profile" in msg:
            title = "Only the active profile's curve is readable"
            msg = "Switch to this profile in the header to edit its curve."
        else:
            title = "Fan curves unavailable"
            msg = ("Not available right now: the fan curves could not be read. Check that "
                   "the asusd service is running, then restart Asusius.")
        self.status.set_title(GLib.markup_escape_text(title))
        self.status.set_subtitle(GLib.markup_escape_text(msg))
        self.status.set_tooltip_text(str(exc))
        self.status.set_visible(True)
        self.readout.set_label("")

    def _show(self) -> None:
        m = self.models.get(self.fan)
        self.canvas.set_model(m)
        if m:
            self._busy = True
            self.enable_row.set_active(m.enabled)
            self._busy = False
        self._changed()

    # -- events -------------------------------------------------------------------------------
    def _pick_fan(self, b, fan):
        if b.get_active():
            self.fan = fan
            self._show()

    def _pick_profile(self, *_):
        if self._busy:
            return
        self.profile = PROFILE_ORDER[self.profile_row.get_selected()]
        self.follow = self.profile is self.win.profile
        self.refresh()

    def on_profile(self, prof: Profile) -> None:
        """The active profile changed elsewhere: follow it unless edits are pending."""
        if not self.follow and prof is not self.profile:
            return
        self.follow = True
        if any(m.dirty for m in self.models.values()):
            return
        if prof is self.profile and self.models:
            return
        self.profile = prof
        self._quiet_select(prof)
        if self.loaded:
            self.refresh()

    def _apply_preset(self, name: str) -> None:
        m = self.models.get(self.fan)
        if m is None:
            return
        apply_fan_preset(m, name)
        self._busy = True
        self.enable_row.set_active(True)
        self._busy = False
        self.canvas.queue_draw()
        self._changed()
        self.win.toast(f"Loaded {name} preset for {self.fan} — click Apply to save")

    def _toggle_enabled(self, *_):
        m = self.models.get(self.fan)
        if m and not self._busy:
            m.enabled = self.enable_row.get_active()
            self.canvas.queue_draw()
            self._changed()

    def _changed(self):
        dirty = any(x.dirty for x in self.models.values())
        self.apply_btn.set_sensitive(dirty)
        self.revert_btn.set_sensitive(dirty)
        self._selected()

    def _selected(self):
        m = self.models.get(self.fan)
        if m and self.canvas.active is not None:
            idx = self.canvas.active
            t_lo, t_hi, _, _ = m.limits_for(idx)
            self.readout.set_label(
                f"Point {idx + 1} of {len(m.points)}  ·  bounds {t_lo}–{t_hi} °C")
        elif m:
            self.readout.set_label("Click a point on the curve to edit it")

    def _revert(self):
        for m in self.models.values():
            m.revert()
        self._show()

    def _apply(self):
        dirty = [m for m in self.models.values() if m.dirty]
        if not dirty or self.profile is None:
            return
        try:
            curves = [m.to_curve() for m in dirty]
        except Exception as exc:
            self.win.toast_error(exc)
            return
        prof = self.profile
        names = " and ".join(c.fan for c in curves)
        body = (f"Writes the {names} fan curve for the {PROFILE_INFO[prof][0]} profile. "
                "A curve that is too low can let the laptop run hot; the firmware still "
                "protects itself at its thermal limit.")

        def go():
            def work():
                return [self.ctl.set_fan_curve(prof, c) for c in curves]

            def after(_=None):
                for m in dirty:
                    m.commit()
                self._changed()
            self.win.run_write(work, f"{names} fan curve applied", after=after)

        self.win.confirm("Apply fan curve?", body, "Apply", go)

    def _reset(self):
        if self.profile is None:
            return
        prof = self.profile

        def go():
            self.win.run_write(lambda: self.ctl.reset_fan_curves(prof),
                               "Factory fan curves restored", after=self.refresh)

        self.win.confirm("Restore factory curves?",
                         f"Both fans in the {PROFILE_INFO[prof][0]} profile go back to the "
                         "firmware default curve.", "Restore", go, destructive=True)

    def on_telemetry(self, t) -> None:
        # Never show CPU temperature on the GPU curve while the dGPU is sleeping.
        self.canvas.live_temp = t.cpu_temp_c if self.fan == "CPU" else t.gpu_temp_c
        self.canvas.queue_draw()
        for name, rpm in t.fan_rpm.items():
            pill_lbl = self.live_pills.get(name)
            if pill_lbl is None:
                pill_lbl = label("", ("live-pill", "numeric"))
                self.live_strip.append(pill_lbl)
                ch = pill_lbl.get_parent()
                if ch is not None:
                    ch.set_focusable(False)
                self.live_pills[name] = pill_lbl
            short = name.replace("_", " ").replace("fan", "").strip().upper()
            temp_txt = ""
            if "CPU" in short and t.cpu_temp_c is not None:
                temp_txt = f" · {fmt_temp(t.cpu_temp_c)}"
            elif "GPU" in short:
                temp_txt = (f" · {fmt_temp(t.gpu_temp_c)}" if t.gpu_temp_c is not None
                            else " · sleeping")
            pill_lbl.set_label(f"{short}: {fmt_rpm(rpm)} rpm{temp_txt}")
