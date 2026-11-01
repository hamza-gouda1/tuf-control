# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..widgets import label


class Page(Adw.Bin):
    """A content page. `refresh()` reloads data off-thread; telemetry is pushed in."""

    key = ""
    title = ""

    def __init__(self, win) -> None:
        super().__init__()
        self.win = win
        self.ctl = win.ctl
        self.loaded = False

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass

    def on_shown(self) -> None:
        if not self.loaded:
            self.loaded = True
            self.refresh()

    def on_telemetry(self, t) -> None:
        pass

    def cap(self, name: str) -> dict | None:
        return (self.win.caps or {}).get(name)


def a11y_name(widget: Gtk.Widget, text: str) -> Gtk.Widget:
    """Accessible name for a control that has no text of its own (switch, scale, radio)."""
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
    return widget


def prefs_page(*groups: Gtk.Widget) -> Adw.PreferencesPage:
    page = Adw.PreferencesPage()
    for g in groups:
        page.add(g)
    widen(page)
    return page


def widen(root: Gtk.Widget, maximum: int = 1100, tightening: int = 900) -> None:
    """AdwPreferencesPage clamps its content to 600 px; use the width the window has."""
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, Adw.Clamp):
            w.set_maximum_size(maximum)
            w.set_tightening_threshold(tightening)
            return
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()


def info_row(title: str, subtitle: str, icon: str = "dialog-information-symbolic") -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_subtitle_lines(0)
    row.add_prefix(Gtk.Image.new_from_icon_name(icon))
    return row


def value_row(title: str, subtitle: str = "",
              numeric: bool = True) -> tuple[Adw.ActionRow, Gtk.Label]:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    val = label("–", ("numeric",) if numeric else (), xalign=1.0)
    val.add_css_class("dim-label")
    row.add_suffix(val)
    return row, val


def pill(text: str, *css: str) -> Gtk.Label:
    lab = label(text, ("pill", *css), xalign=0.5)
    lab.set_valign(Gtk.Align.CENTER)
    return lab


def soon_badge() -> Gtk.Label:
    lab = label("COMING SOON", ("soon",), xalign=0.5)
    lab.set_valign(Gtk.Align.CENTER)
    return lab


def button(text: str, *css: str, icon: str | None = None, on_click=None) -> Gtk.Button:
    if icon:
        b = Gtk.Button(child=Adw.ButtonContent(label=text, icon_name=icon))
    else:
        b = Gtk.Button(label=text)
    for c in css:
        b.add_css_class(c)
    b.set_valign(Gtk.Align.CENTER)
    if on_click:
        b.connect("clicked", lambda *_: on_click())
    return b
