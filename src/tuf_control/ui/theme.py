"""Palette, CSS loading and colours for Cairo drawing."""
from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo

HERE = Path(__file__).resolve().parent
BURGUNDY = (0x8E / 255, 0x2A / 255, 0x44 / 255)
GOLD = (0xC9 / 255, 0xAE / 255, 0x86 / 255)

# Cairo palette per scheme: line, fill, grid, text-dim
PALETTE = {
    "dark": {"line": (0.86, 0.45, 0.55), "fill": (0.56, 0.16, 0.27),
             "grid": (1, 0.94, 0.92, 0.07),
             "dim": (0.95, 0.90, 0.88, 0.55), "gold": GOLD, "point": (0.96, 0.91, 0.88)},
    "light": {"line": (0.56, 0.16, 0.27), "fill": (0.56, 0.16, 0.27),
              "grid": (0.2, 0.08, 0.1, 0.08),
              "dim": (0.17, 0.10, 0.12, 0.55), "gold": (0.62, 0.50, 0.32), "point": (1, 1, 1)},
}

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
        self._dark_on = False
        self._display, self._prio = display, prio
        sm = Adw.StyleManager.get_default()
        sm.connect("notify::dark", lambda *_: self.sync())
        self.sync()

    def sync(self) -> None:
        want = is_dark()
        if want and not self._dark_on:
            Gtk.StyleContext.add_provider_for_display(self._display, self.dark, self._prio + 1)
        elif not want and self._dark_on:
            Gtk.StyleContext.remove_provider_for_display(self._display, self.dark)
        self._dark_on = want
