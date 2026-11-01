# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Shared building blocks for the monitor pages (dashboard cards, detail pages)."""
from __future__ import annotations

from gi.repository import Adw, Gtk, Pango

from ..charts import Chart, Line
from ..viewmodels import nb
from ..widgets import label
from .base import Page

WINDOW_S = 120.0


class MonitorPage(Page):
    """A live page fed by the window's LiveHub. Built when first shown."""

    is_monitor_page = True
    rate_s = 0.5  # sampling interval while this page is visible

    def __init__(self, win) -> None:
        super().__init__(win)
        self.hub = win.hub
        self.charts: list[Gtk.Widget] = []
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                            margin_top=22, margin_bottom=28, margin_start=16, margin_end=16)
        clamp = Adw.Clamp(maximum_size=1200, tightening_threshold=1000, child=self.body)
        self.scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.set_child(self.scroller)

    def refresh(self) -> None:
        self.update()

    def on_shown(self) -> None:
        super().on_shown()
        self.update()

    def on_sample(self, hub) -> None:
        self.update()

    def update(self) -> None:  # pragma: no cover - overridden
        pass

    def redraw(self) -> None:
        for c in self.charts:
            c.queue_draw()

    # -- builders ---------------------------------------------------------------------
    def header(self, title: str, subtitle: str = "") -> tuple[Gtk.Label, Gtk.Label, Gtk.Box]:
        box = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        t = label(title, ("hero-heading",), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        s = label(subtitle, ("section-caption",), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        titles.append(t)
        titles.append(s)
        box.append(titles)
        pills = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(pills)
        self.body.append(box)
        return t, s, pills

    def chart(self, lines: list[Line], **kw) -> Chart:
        kw["height"] = int(kw.get("height", 120) * 1.3)  # roomier than the first cut
        c = Chart(self.hub, lines, **kw)
        self.charts.append(c)
        return c

    def panel(self, title: str, child: Gtk.Widget | None = None,
              suffix: Gtk.Widget | None = None) -> Gtk.Box:
        p = Panel(title, suffix)
        if child is not None:
            p.append(child)
        self.body.append(p)
        return p


class Panel(Gtk.Box):
    """A rounded card with a title row."""

    def __init__(self, title: str, suffix: Gtk.Widget | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("tile")
        self.add_css_class("monitor-panel")
        head = Gtk.Box(spacing=8)
        self.title = label(title, ("panel-title",), hexpand=True, wrap=True,
                           wrap_mode=Pango.WrapMode.WORD_CHAR)
        head.append(self.title)
        self.suffix_box = Gtk.Box(spacing=8)
        head.append(self.suffix_box)
        if suffix is not None:
            self.suffix_box.append(suffix)
        self.append(head)


class StatGrid(Gtk.FlowBox):
    """Caption/value pairs that reflow with the width."""

    def __init__(self, min_per_line: int = 2, max_per_line: int = 4) -> None:
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                         min_children_per_line=min_per_line, max_children_per_line=max_per_line,
                         column_spacing=10, row_spacing=10)
        self.cells: dict[str, Gtk.Label] = {}
        self.boxes: dict[str, Gtk.Widget] = {}

    def add(self, key: str, caption: str, tooltip: str | None = None) -> Gtk.Label:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("stat-cell")
        wrap = {"wrap": True, "wrap_mode": Pango.WrapMode.WORD_CHAR}  # never cut a value off
        cap = label(caption, ("stat-caption",), **wrap)
        val = label("–", ("stat-cell-value",), **wrap)
        box.append(cap)
        box.append(val)
        if tooltip:
            box.set_tooltip_text(tooltip)
        self.append(box)
        box.get_parent().set_focusable(False)
        self.cells[key] = val
        self.boxes[key] = box.get_parent()
        return val

    def set(self, key: str, text: str, tooltip: str | None = None) -> None:
        lab = self.cells.get(key)
        if lab is None:
            return
        text = nb(text)
        if lab.get_label() != text:
            lab.set_label(text)
        if tooltip is not None:
            self.boxes[key].set_tooltip_text(tooltip)

    def show(self, key: str, visible: bool) -> None:
        if key in self.boxes:
            self.boxes[key].set_visible(visible)


def note(text: str, css: str = "monitor-note") -> Gtk.Label:
    lab = label(text, (css,), wrap=True)
    lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return lab


def set_text(lab: Gtk.Label, text: str) -> None:
    text = nb(text)
    if lab.get_label() != text:
        lab.set_label(text)
