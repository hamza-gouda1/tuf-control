# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, Gdk, Gtk

from ...core import KbdEffect
from ..tasks import run_bg
from ..viewmodels import (
    AURA_MODES,
    BRIGHTNESS_NAMES,
    DIRECTIONS,
    PALETTES,
    SPEEDS,
    KbdForm,
    speed_index,
)
from ..widgets import EffectSwatch, KeyboardPreview, palette_buttons
from .base import Page, button, info_row, prefs_page
from .keyboard_effects import HW_NOTE, EffectsSection


def _rgba(rgb) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0
    return c


def _rgb(rgba: Gdk.RGBA) -> tuple[int, int, int]:
    return (round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255))


class KeyboardPage(Page):
    key, title = "keyboard", "Keyboard"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.form = KbdForm()
        self.brightness = 3
        self._quiet = False
        self._info: dict | None = None      # last get_kbd_rgb()
        self._soft_fx: dict | None = None   # last effects config section
        self._soft_key = None               # the gradient state the form was loaded with
        self.hw_note = HW_NOTE              # set by the software section from the daemon

        self.preview = KeyboardPreview()
        g_prev = Adw.PreferencesGroup()
        g_prev.add(self.preview)

        self.backlight = Adw.PreferencesGroup(title="Backlight")
        self.bri_row = Adw.ActionRow(title="Brightness")
        self.bri_box = Gtk.Box(valign=Gtk.Align.CENTER)
        self.bri_box.add_css_class("linked")
        self.bri_box.add_css_class("bri-toggle")
        self.bri_buttons = []
        first = None
        for i, name in enumerate(BRIGHTNESS_NAMES):
            b = Gtk.ToggleButton(label=name)
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._bri, i)
            self.bri_box.append(b)
            self.bri_buttons.append(b)
        self.bri_row.add_suffix(self.bri_box)
        self.backlight.add(self.bri_row)

        self.fx = Adw.PreferencesGroup(title="Hardware effect", description=HW_NOTE)
        self.apply_btn = button("Apply", "suggested-action", on_click=self._apply)
        self.apply_btn.set_sensitive(False)
        self.fx.set_header_suffix(self.apply_btn)
        self.mode_row = Adw.ComboRow(title="Effect")
        self.mode_row.set_subtitle_lines(0)
        self.swatch = EffectSwatch()
        self.swatch.set_tooltip_text("How the effect's colour moves")
        self.mode_row.add_suffix(self.swatch)
        self.mode_row.connect("notify::selected", self._mode_picked)
        self.palette_row = Adw.ActionRow(title="Palette", subtitle="Sets Colour 1 and Colour 2")
        self.palette_row.add_suffix(palette_buttons(PALETTES, self._palette))
        self.c1 = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Choose colour", with_alpha=False),
            valign=Gtk.Align.CENTER)
        self.c1.connect("notify::rgba", self._edit)
        self.c1_row = Adw.ActionRow(title="Colour 1")
        self.c1_row.add_suffix(self.c1)
        self.c2 = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Choose colour", with_alpha=False),
            valign=Gtk.Align.CENTER)
        self.c2.connect("notify::rgba", self._edit)
        self.c2_row = Adw.ActionRow(title="Colour 2")
        self.c2_row.add_suffix(self.c2)
        self.blend_row = Adw.SwitchRow(
            title="Two colours",
            subtitle="Fade Colour 1 ↔ Colour 2 instead. Run by asusiusd, a few writes "
                     "per second.")
        self.blend_row.set_subtitle_lines(0)
        self.blend_row.connect("notify::active", self._blend)
        self.speed_row = Adw.ComboRow(title="Speed",
                                      model=Gtk.StringList.new([s for s, _ in SPEEDS]))
        self.speed_row.connect("notify::selected", self._edit)
        self.dir_row = Adw.ComboRow(title="Direction",
                                    model=Gtk.StringList.new([d for d, _ in DIRECTIONS]))
        self.dir_row.connect("notify::selected", self._edit)
        self.fx_err = info_row("Effects unavailable", "")
        self.fx_err.set_visible(False)
        self.fx_rows = (self.mode_row, self.blend_row, self.palette_row, self.c1_row,
                        self.c2_row, self.speed_row, self.dir_row)
        for r in (*self.fx_rows, self.fx_err):
            self.fx.add(r)

        self.soft = EffectsSection(self)
        self.prefs = prefs_page(g_prev, self.backlight, self.fx, self.soft)
        self.set_child(self.prefs)

    # the form's state, under the names the rest of the app (and the shot harness) uses
    @property
    def effect(self) -> KbdEffect:
        return self.form.current

    @property
    def saved(self) -> KbdEffect | None:
        return self.form.saved

    # -- data -----------------------------------------------------------------------------
    def refresh(self) -> None:
        run_bg(lambda: self.ctl.get_kbd_brightness()[0], done=self._got_bri,
               error=self._bri_failed)
        run_bg(lambda: self.ctl.get_kbd_rgb()[0], done=self._got_rgb, error=self._rgb_failed)
        self.soft.refresh()

    def on_hidden(self) -> None:
        self.soft.on_hidden()

    def _bri_failed(self, exc) -> None:
        self.bri_row.set_sensitive(False)
        self.bri_row.set_subtitle(
            "Not available right now: keyboard brightness could not be read. Check that the "
            "asusd service is running, then press Refresh.")
        self.bri_row.set_tooltip_text(str(exc))

    def on_telemetry(self, t) -> None:
        self.soft.on_telemetry(t)

    def _show(self) -> None:
        breathe = self.form.controls.breathe
        self.swatch.set_effect(self.effect, self.brightness, breathe)
        if not self.soft.active:  # a software effect owns the preview while selected
            self.preview.set_effect(self.effect, self.brightness, breathe)

    def _got_bri(self, val) -> None:
        cur, mx = val
        self.brightness = cur
        self.bri_row.set_sensitive(True)
        self.bri_row.set_subtitle("")
        self.bri_row.set_tooltip_text(None)
        self._quiet = True
        for i, b in enumerate(self.bri_buttons):
            b.set_visible(i <= mx)
        if 0 <= cur < len(self.bri_buttons):
            self.bri_buttons[cur].set_active(True)
        self._quiet = False
        self._show()

    def _rgb_failed(self, exc) -> None:
        for r in self.fx_rows:
            r.set_sensitive(False)
        self.fx_err.set_subtitle(
            "Not available right now: the asusd service did not answer, so hardware keyboard "
            "effects cannot be read. Start it, then refresh.")
        self.fx_err.set_visible(True)
        self.fx_err.set_tooltip_text(str(exc))
        self.preview.set_opacity(0.4)  # nothing to show: not a lit keyboard
        self._show()

    @staticmethod
    def _gradient_key(fx: dict | None):
        fx = fx or {}
        on = fx.get("active") == "gradient"
        return (on, repr(sorted(((fx.get("params") or {}).get("gradient") or {}).items()))
                if on else None)

    def _got_rgb(self, info) -> None:
        self.preview.set_opacity(1.0)
        for r in self.fx_rows:
            r.set_sensitive(True)
        self.fx_err.set_visible(False)
        self._info = info
        self._load()

    def soft_config_loaded(self, fx: dict) -> None:
        """The software section read the config: a running Gradient breathing is the
        hardware Breathe's Two colours, so the form shows it that way."""
        self._soft_fx = fx
        if self._info is not None and self._gradient_key(fx) != self._soft_key:
            self._load()

    def _load(self) -> None:
        self._soft_key = self._gradient_key(self._soft_fx)
        self.form.load(self._info or {}, self._soft_fx)
        self._quiet = True
        self.mode_row.set_model(Gtk.StringList.new([AURA_MODES[m] for m in self.form.modes]))
        if self.effect.mode in self.form.modes:
            self.mode_row.set_selected(self.form.modes.index(self.effect.mode))
        self.blend_row.set_active(self.form.blend)
        self._quiet = False
        self._sync_widgets()

    def _sync_widgets(self) -> None:
        """Put the form's current effect into the widgets and show the rows it uses."""
        eff, ctl = self.effect, self.form.controls
        self._quiet = True
        self.c1.set_rgba(_rgba(eff.colour1))
        self.c2.set_rgba(_rgba(eff.colour2))
        self.speed_row.set_selected(speed_index(eff.speed))
        self.dir_row.set_selected(min(eff.direction, len(DIRECTIONS) - 1))
        self.blend_row.set_active(self.form.blend)
        self._quiet = False
        self.mode_row.set_subtitle(ctl.summary)
        self.c1_row.set_title(ctl.colour1_title)
        self.c1_row.set_visible(ctl.colour1)
        self.c2_row.set_visible(ctl.colour2)
        self.blend_row.set_visible(ctl.blend)
        self.palette_row.set_visible(ctl.palettes)
        self.speed_row.set_visible(ctl.speed)
        self.dir_row.set_visible(ctl.direction)
        self.apply_btn.set_sensitive(self.form.dirty)
        self.describe()
        self._show()

    def describe(self) -> None:
        note = self.hw_note
        if note == HW_NOTE and self.soft.active:
            note = ("Paused while a software effect is selected below; choose Off there to "
                    "resume firmware lighting.")
        elif self.form.controls.breathe == "blend" and note == HW_NOTE:
            note = ("The firmware breathes Colour 1; Two colours adds the fade to Colour 2 "
                    "in software.")
        self.fx.set_description(note)

    # -- events ---------------------------------------------------------------------------
    def _mode_picked(self, *_):
        if self._quiet:
            return
        sel = self.mode_row.get_selected()
        if 0 <= sel < len(self.form.modes):
            self.form.select(self.form.modes[sel])
        self._sync_widgets()

    def _edit(self, *_):
        if self._quiet:
            return
        self.form.edit(colour1=_rgb(self.c1.get_rgba()), colour2=_rgb(self.c2.get_rgba()),
                       speed_idx=self.speed_row.get_selected(),
                       direction=DIRECTIONS[self.dir_row.get_selected()][1])
        self._sync_widgets()

    def _blend(self, *_):
        if self._quiet:
            return
        self.form.set_blend(self.blend_row.get_active())
        self._sync_widgets()

    def _palette(self, index: int) -> None:
        self.form.palette(index)
        self._sync_widgets()

    def _apply(self):
        plan = self.form.plan()
        eff = plan.hardware
        name = AURA_MODES.get(eff.mode, "Effect")

        def work():
            # a running software effect would paint over the hardware mode within a frame:
            # turn it off first and wait until asusiusd has let go of the keyboard
            stopped = self.soft.release_for_hardware()
            self.ctl.set_kbd_effect(eff, verify=True)  # read back: no silent no-op
            if plan.software is None:
                return stopped, None
            # Two colours: the firmware keeps Breathe (Colour 1 <-> off, the mode restored
            # when effects stop); asusiusd fades Colour 1 <-> Colour 2 over it
            return stopped, self.soft.enable(plan.software["active"],
                                             plan.software["params"])

        def done(res):
            stopped, soft = res
            self.form.applied(plan)
            self._sync_widgets()
            fake = " (fake hardware)" if self.win.fake else ""
            if soft is not None:
                cfg, started = soft
                self._soft_fx, self._soft_key = cfg["effects"], self._gradient_key(cfg["effects"])
                self.soft._got_config(cfg)
                self.win.toast(f"{name}: Colour 1 ↔ Colour 2 on"
                               + (" · service started" if started else "") + fake)
                self.soft.poll_status()
                return
            self.win.toast(f"{name} applied" + (" · software effect off" if stopped else "")
                           + fake)
            if stopped:
                self.soft.refresh()

        def fail(exc):
            self.win.toast_error(exc)
            self.refresh()  # show what the keyboard really runs
        self.apply_btn.set_sensitive(False)
        run_bg(work, done=done, error=fail)

    def _bri(self, b, level):
        if not b.get_active():
            return
        self.brightness = level
        self._show()
        if self._quiet:
            return
        self.win.run_write(lambda: self.ctl.set_kbd_brightness(level),
                           f"Backlight {BRIGHTNESS_NAMES[level].lower()}", on_error=self.refresh)
