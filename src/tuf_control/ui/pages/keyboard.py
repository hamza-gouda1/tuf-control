from __future__ import annotations

import dataclasses

from gi.repository import Adw, Gdk, Gtk

from ...core import KbdEffect
from ..tasks import run_bg
from ..viewmodels import (
    AURA_MODES,
    BRIGHTNESS_NAMES,
    DIRECTIONS,
    MODE_USES_COLOUR1,
    MODE_USES_COLOUR2,
    MODE_USES_DIRECTION,
    MODE_USES_SPEED,
    SPEEDS,
    effect_from_rgb_data,
    speed_index,
)
from ..widgets import KeyboardPreview
from .base import Page, button, info_row, prefs_page
from .keyboard_effects import EffectsSection


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
        self.effect = KbdEffect(0)
        self.saved: KbdEffect | None = None
        self.builtins: dict[int, KbdEffect] = {}
        self.modes: list[int] = [0]
        self.brightness = 3
        self._quiet = False

        self.preview = KeyboardPreview()
        g_prev = Adw.PreferencesGroup()
        g_prev.add(self.preview)

        self.backlight = Adw.PreferencesGroup(title="Backlight")
        self.bri_row = Adw.ActionRow(title="Brightness")
        self.bri_box = Gtk.Box(valign=Gtk.Align.CENTER)
        self.bri_box.add_css_class("linked")
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

        self.fx = Adw.PreferencesGroup(title="Hardware effect",
                                       description="Runs on the keyboard itself, no CPU cost.")
        self.apply_btn = button("Apply", "suggested-action", on_click=self._apply)
        self.apply_btn.set_sensitive(False)
        self.fx.set_header_suffix(self.apply_btn)
        self.mode_row = Adw.ComboRow(title="Effect")
        self.mode_row.connect("notify::selected", self._edit)
        self.c1 = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                        valign=Gtk.Align.CENTER)
        self.c1.connect("notify::rgba", self._edit)
        self.c1_row = Adw.ActionRow(title="Colour")
        self.c1_row.add_suffix(self.c1)
        self.c2 = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                        valign=Gtk.Align.CENTER)
        self.c2.connect("notify::rgba", self._edit)
        self.c2_row = Adw.ActionRow(title="Second colour")
        self.c2_row.add_suffix(self.c2)
        self.speed_row = Adw.ComboRow(title="Speed",
                                      model=Gtk.StringList.new([s for s, _ in SPEEDS]))
        self.speed_row.connect("notify::selected", self._edit)
        self.dir_row = Adw.ComboRow(title="Direction",
                                    model=Gtk.StringList.new([d for d, _ in DIRECTIONS]))
        self.dir_row.connect("notify::selected", self._edit)
        self.fx_err = info_row("Effects unavailable", "")
        self.fx_err.set_visible(False)
        for r in (self.mode_row, self.c1_row, self.c2_row, self.speed_row, self.dir_row,
                  self.fx_err):
            self.fx.add(r)

        self.soft = EffectsSection(self)
        self.prefs = prefs_page(g_prev, self.backlight, self.fx, self.soft)
        self.set_child(self.prefs)

    # -- data -----------------------------------------------------------------------------
    def refresh(self) -> None:
        run_bg(lambda: self.ctl.get_kbd_brightness()[0], done=self._got_bri,
               error=lambda e: (self.bri_row.set_sensitive(False),
                                self.bri_row.set_subtitle(str(e))))
        run_bg(lambda: self.ctl.get_kbd_rgb()[0], done=self._got_rgb, error=self._rgb_failed)
        self.soft.refresh()

    def on_telemetry(self, t) -> None:
        self.soft.on_telemetry(t)

    def _show(self) -> None:
        if not self.soft.active:  # a software effect owns the preview while selected
            self.preview.set_effect(self.effect, self.brightness)

    def _got_bri(self, val) -> None:
        cur, mx = val
        self.brightness = cur
        self._quiet = True
        for i, b in enumerate(self.bri_buttons):
            b.set_visible(i <= mx)
        if 0 <= cur < len(self.bri_buttons):
            self.bri_buttons[cur].set_active(True)
        self._quiet = False
        self._show()

    def _rgb_failed(self, exc) -> None:
        for r in (self.mode_row, self.c1_row, self.c2_row, self.speed_row, self.dir_row):
            r.set_sensitive(False)
        self.fx_err.set_subtitle(str(exc))
        self.fx_err.set_visible(True)
        self._show()

    def _got_rgb(self, info) -> None:
        self.saved = effect_from_rgb_data(info.get("mode_data"))
        self.effect = dataclasses.replace(self.saved)
        # asusd keeps colours/speed per mode: switching the picker shows that mode's own
        self.builtins = {int(m): effect_from_rgb_data(d)
                         for m, d in (info.get("builtins") or {}).items()}
        self.builtins[self.saved.mode] = self.saved
        modes = [int(m) for m in (info.get("supported_modes") or [self.saved.mode])]
        self.modes = [m for m in modes if m in AURA_MODES] or [self.saved.mode]
        self._quiet = True
        self.mode_row.set_model(Gtk.StringList.new([AURA_MODES[m] for m in self.modes]))
        if self.saved.mode in self.modes:
            self.mode_row.set_selected(self.modes.index(self.saved.mode))
        self.c1.set_rgba(_rgba(self.saved.colour1))
        self.c2.set_rgba(_rgba(self.saved.colour2))
        self.speed_row.set_selected(speed_index(self.saved.speed))
        self.dir_row.set_selected(min(self.saved.direction, len(DIRECTIONS) - 1))
        self._quiet = False
        self._edit()

    def _load_mode(self, eff: KbdEffect) -> None:
        self._quiet = True
        self.c1.set_rgba(_rgba(eff.colour1))
        self.c2.set_rgba(_rgba(eff.colour2))
        self.speed_row.set_selected(speed_index(eff.speed))
        self.dir_row.set_selected(min(eff.direction, len(DIRECTIONS) - 1))
        self._quiet = False

    # -- events ---------------------------------------------------------------------------
    def _edit(self, *_):
        if self._quiet:
            return
        sel = self.mode_row.get_selected()
        mode = self.modes[sel] if 0 <= sel < len(self.modes) else self.effect.mode
        if mode != self.effect.mode and mode in self.builtins:
            self._load_mode(self.builtins[mode])
        speed = SPEEDS[self.speed_row.get_selected()][1] if self.saved is None or \
            self.saved.speed in (0, 1, 2) else (0xE1, 0xEB, 0xF5)[self.speed_row.get_selected()]
        self.effect = KbdEffect(mode, self.effect.zone, _rgb(self.c1.get_rgba()),
                                _rgb(self.c2.get_rgba()), speed,
                                DIRECTIONS[self.dir_row.get_selected()][1])
        self.c1_row.set_visible(mode in MODE_USES_COLOUR1)
        self.c2_row.set_visible(mode in MODE_USES_COLOUR2)
        self.speed_row.set_visible(mode in MODE_USES_SPEED)
        self.dir_row.set_visible(mode in MODE_USES_DIRECTION)
        self.apply_btn.set_sensitive(self.saved is not None and self.effect != self.saved)
        self._show()

    def _apply(self):
        eff = self.effect
        name = AURA_MODES.get(eff.mode, "Effect")

        def work():
            # a running software effect would paint over the hardware mode within a frame:
            # turn it off first and wait until tuf-controld has let go of the keyboard
            stopped = self.soft.release_for_hardware()
            self.ctl.set_kbd_effect(eff, verify=True)  # read back: no silent no-op
            return stopped

        def done(stopped):
            self.saved = eff
            self.builtins[eff.mode] = eff
            self._edit()
            self.win.toast(f"{name} applied" + (" · software effect off" if stopped else "")
                           + (" (fake hardware)" if self.win.fake else ""))
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
