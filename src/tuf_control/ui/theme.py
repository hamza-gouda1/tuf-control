# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Palette, CSS loading and colours for Cairo drawing."""
from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo

from ..brand_palette import BURGUNDY, GOLD, PALETTE

HERE = Path(__file__).resolve().parent
_mono_family: str | None = None


def mono_family() -> str:
    """Geist Mono when installed, otherwise the generic monospace family."""
    global _mono_family
    if _mono_family is None:
        fams = {f.get_name() for f in PangoCairo.FontMap.get_default().list_families()}
        _mono_family = "Geist Mono" if "Geist Mono" in fams else "monospace"
    return _mono_family


def is_dark() -> bool:
    return Adw.StyleManager.get_default().get_dark()


def colours() -> dict:
    return PALETTE["dark" if is_dark() else "light"]


def mono_font(size_pt: float, weight: str = "") -> Pango.FontDescription:
    return Pango.FontDescription.from_string(f"{mono_family()} {weight} {size_pt}".strip())


class Styles:
    """Base stylesheet plus a dark overlay swapped with the system scheme."""

    def __init__(self) -> None:
        display = Gdk.Display.get_default()
        self.base = Gtk.CssProvider()
        css = (HERE / "style.css").read_text().replace("@MONO@", mono_family())
        self.base.load_from_string(css)
        self.dark = Gtk.CssProvider()
        self.dark.load_from_path(str(HERE / "style-dark.css"))
        prio = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        Gtk.StyleContext.add_provider_for_display(display, self.base, prio)
        # popover surface fix: must outrank ~/.config/gtk-4.0/gtk.css (see the file)
        self.popover = Gtk.CssProvider()
        self.popover.load_from_path(str(HERE / "style-popover.css"))
        self.popover_dark = Gtk.CssProvider()
        self.popover_dark.load_from_path(str(HERE / "style-popover-dark.css"))
        self._pop_prio = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
        Gtk.StyleContext.add_provider_for_display(display, self.popover, self._pop_prio)
        self._dark_on = False
        self._display, self._prio = display, prio
        sm = Adw.StyleManager.get_default()
        sm.connect("notify::dark", lambda *_: self.sync())
        self.sync()

    def sync(self) -> None:
        want = is_dark()
        if want and not self._dark_on:
            Gtk.StyleContext.add_provider_for_display(self._display, self.dark, self._prio + 1)
            Gtk.StyleContext.add_provider_for_display(self._display, self.popover_dark,
                                                      self._pop_prio + 1)
        elif not want and self._dark_on:
            Gtk.StyleContext.remove_provider_for_display(self._display, self.dark)
            Gtk.StyleContext.remove_provider_for_display(self._display, self.popover_dark)
        self._dark_on = want
