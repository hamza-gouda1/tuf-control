# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Custom widgets: sparkline, stat tile, fan-curve canvas, keyboard preview."""
from __future__ import annotations

import math
import time

from gi.repository import Adw, GLib, Gtk, Pango, PangoCairo

from . import theme
from .viewmodels import (
    CurveGeometry,
    bubble_rect,
    FanCurveModel,
    History,
    pwm_to_pct,
    smooth_path,
    smooth_values,
)
from ..core import FanCurve, KbdEffect
from .viewmodels import FrameGate, is_animated, preview_colour


def label(text: str = "", css: tuple[str, ...] = (), xalign: float = 0.0, **kw) -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in css:
        lab.add_css_class(c)
    return lab


class Sparkline(Gtk.DrawingArea):
    """Smooth area graph over a History window; redraws on demand."""

    def __init__(self, history: History, floor: float | None = 0.0, min_span: float = 10.0,
                 height: int = 92) -> None:
        super().__init__()
        self.history, self.floor, self.min_span = history, floor, min_span
        self.add_css_class("sparkline")
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.now: float | None = None  # fixed clock for screenshots/tests

    def _draw(self, _area, cr, w, h):
        pal = theme.colours()
        now = self.now if self.now is not None else time.monotonic()
        pts = smooth_values(self.history.normalized(now))
        # baseline grid: three faint lines
        cr.set_line_width(1)
        cr.set_source_rgba(*pal["grid"])
        for k in (0.25, 0.5, 0.75):
            y = round(h * k) + 0.5
            cr.move_to(0, y)
            cr.line_to(w, y)
        cr.stroke()
        if len(pts) < 2:
            return
        lo, hi = self.history.bounds(self.floor, self.min_span)
        pad = 3.0
        xy = [(x * w, pad + (1 - (v - lo) / (hi - lo)) * (h - 2 * pad)) for x, v in pts]
        segs = smooth_path(xy)

        def path():
            cr.move_to(*xy[0])
            for s in segs:
                cr.curve_to(*s)

        import cairo
        path()
        cr.line_to(xy[-1][0], h)
        cr.line_to(xy[0][0], h)
        cr.close_path()
        grad = cairo.LinearGradient(0, 0, 0, h)
        r, g, b = pal["fill"]
        grad.add_color_stop_rgba(0, r, g, b, 0.38 if theme.is_dark() else 0.22)
        grad.add_color_stop_rgba(1, r, g, b, 0.0)
        cr.set_source(grad)
        cr.fill()
        path()
        cr.set_source_rgb(*pal["line"])
        cr.set_line_width(2.0)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.stroke()
        # live point
        x, y = xy[-1]
        cr.set_source_rgb(*pal["line"])
        cr.arc(min(x, w - 3.5), y, 3.2, 0, 2 * math.pi)
        cr.fill()


class Tile(Gtk.Box):
    """Dashboard card: title, big tabular number with unit, sub line, optional graph."""

    def __init__(self, title: str, icon: str, unit: str, history: History | None = None,
                 floor: float | None = 0.0, min_span: float = 10.0) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("tile")
        self.set_hexpand(True)
        head = Gtk.Box(spacing=8)
        img = Gtk.Image.new_from_icon_name(icon)
        img.add_css_class("tile-icon")
        head.append(img)
        head.append(label(title, ("tile-title",), hexpand=True))
        self.badge = label("", ("pill",))
        self.badge.set_visible(False)
        head.append(self.badge)
        self.append(head)
        row = Gtk.Box(spacing=6, margin_top=6)
        self.value = label("–", ("stat-value",))
        self.value.set_valign(Gtk.Align.BASELINE_FILL if hasattr(Gtk.Align, "BASELINE_FILL")
                              else Gtk.Align.BASELINE)
        self.unit = label(unit, ("stat-unit",))
        self.unit.set_valign(Gtk.Align.END)
        row.append(self.value)
        row.append(self.unit)
        self.extra = Gtk.Box(spacing=18, halign=Gtk.Align.END, hexpand=True,
                             valign=Gtk.Align.END)
        row.append(self.extra)
        self.append(row)
        self.sub = label("", ("stat-sub",), ellipsize=Pango.EllipsizeMode.END)
        self.append(self.sub)
        self.graph = Sparkline(history, floor, min_span) if history is not None else None
        if self.graph:
            self.append(self.graph)

    def add_mini(self, caption: str) -> Gtk.Label:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        val = label("–", ("mini-value", "numeric"), xalign=1.0)
        box.append(val)
        box.append(label(caption, ("mini-label",), xalign=1.0))
        self.extra.append(box)
        return val

    def set_badge(self, text: str | None, css: str = "") -> None:
        self.badge.set_visible(bool(text))
        self.badge.set_label(text or "")
        for c in ("accent", "gold"):
            self.badge.remove_css_class(c)
        if css:
            self.badge.add_css_class(css)


class FanCurveCanvas(Gtk.DrawingArea):
    """Drag the 8 points of a FanCurveModel. Calls on_change() after each edit."""

    def __init__(self, on_change=None) -> None:
        super().__init__()
        self.model: FanCurveModel | None = None
        self.on_change = on_change
        self.active: int | None = None
        self.hover: int | None = None
        self.live_temp: float | None = None
        self.set_content_height(300)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_focusable(True)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self._origin = (0.0, 0.0)

    def set_model(self, model: FanCurveModel | None) -> None:
        self.model = model
        self.active = 0 if model else None
        self.queue_draw()

    def geometry(self) -> CurveGeometry:
        return CurveGeometry(self.get_width(), self.get_height())

    def _hit(self, x, y) -> int | None:
        if not self.model:
            return None
        g = self.geometry()
        t, p = g.to_data(x, y)
        return self.model.nearest(t, p, 16.0, g.scale)

    def _set_hover(self, idx):
        if idx != self.hover:
            self.hover = idx
            self.set_cursor_from_name("grab" if idx is not None else None)
            self.queue_draw()

    def _motion(self, _c, x, y):
        self._set_hover(self._hit(x, y))

    def _begin(self, gesture, x, y):
        idx = self._hit(x, y)
        if idx is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self.grab_focus()
        self.active = idx
        self._origin = (x, y)
        self.set_cursor_from_name("grabbing")
        self.queue_draw()

    def _update(self, _g, dx, dy):
        if self.model is None or self.active is None:
            return
        g = self.geometry()
        t, p = g.to_data(self._origin[0] + dx, self._origin[1] + dy)
        self.model.drag(self.active, t, p)
        self.queue_draw()
        if self.on_change:
            self.on_change()

    def _end(self, *_):
        self.set_cursor_from_name("grab" if self.hover is not None else None)

    def _key(self, _c, keyval, _code, _state):
        from gi.repository import Gdk
        if self.model is None or self.active is None:
            return False
        t, p = self.model.points[self.active]
        moves = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0),
                 Gdk.KEY_Up: (0, 255 / 100), Gdk.KEY_Down: (0, -255 / 100)}
        if keyval in (Gdk.KEY_Tab,):
            return False
        if keyval in (Gdk.KEY_bracketleft, Gdk.KEY_comma):
            self.active = max(0, self.active - 1)
        elif keyval in (Gdk.KEY_bracketright, Gdk.KEY_period):
            self.active = min(len(self.model.points) - 1, self.active + 1)
        elif keyval in moves:
            dt, dp = moves[keyval]
            self.model.drag(self.active, t + dt, p + dp)
            if self.on_change:
                self.on_change()
        else:
            return False
        self.queue_draw()
        return True

    def _draw(self, _area, cr, w, h):
        import cairo
        pal = theme.colours()
        g = CurveGeometry(w, h)
        lo_t, hi_t = FanCurve.TEMP_RANGE
        # grid + axis labels
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(theme.mono_font(9.5))
        cr.set_line_width(1)
        for pct in (0, 25, 50, 75, 100):
            _, y = g.to_px(lo_t, pct * 255 / 100)
            cr.set_source_rgba(*pal["grid"])
            cr.move_to(g.left, round(y) + 0.5)
            cr.line_to(w - g.right, round(y) + 0.5)
            cr.stroke()
            cr.set_source_rgba(*pal["dim"])
            layout.set_text(f"{pct}%", -1)
            tw, th = layout.get_pixel_size()
            cr.move_to(g.left - tw - 8, y - th / 2)
            PangoCairo.show_layout(cr, layout)
        for temp in range(30, hi_t + 1, 20):
            x, _ = g.to_px(temp, 0)
            cr.set_source_rgba(*pal["grid"])
            cr.move_to(round(x) + 0.5, g.top)
            cr.line_to(round(x) + 0.5, h - g.bottom)
            cr.stroke()
            cr.set_source_rgba(*pal["dim"])
            layout.set_text(f"{temp}°", -1)
            tw, _ = layout.get_pixel_size()
            cr.move_to(x - tw / 2, h - g.bottom + 8)
            PangoCairo.show_layout(cr, layout)
        if self.live_temp is not None:
            x, _ = g.to_px(max(lo_t, min(hi_t, self.live_temp)), 0)
            cr.set_source_rgba(*pal["gold"], 0.8)
            cr.set_dash([3, 4])
            cr.move_to(round(x) + 0.5, g.top)
            cr.line_to(round(x) + 0.5, h - g.bottom)
            cr.stroke()
            cr.set_dash([])
        if not self.model:
            return
        pts = [g.to_px(t, p) for t, p in self.model.points]
        # area under the curve (step-free linear: firmware interpolates linearly)
        cr.move_to(g.left, pts[0][1])
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(w - g.right, pts[-1][1])
        cr.line_to(w - g.right, h - g.bottom)
        cr.line_to(g.left, h - g.bottom)
        cr.close_path()
        grad = cairo.LinearGradient(0, g.top, 0, h - g.bottom)
        r, gg, b = pal["fill"]
        alpha = 0.32 if theme.is_dark() else 0.16
        if not self.model.enabled:
            alpha *= 0.4
        grad.add_color_stop_rgba(0, r, gg, b, alpha)
        grad.add_color_stop_rgba(1, r, gg, b, 0.02)
        cr.set_source(grad)
        cr.fill()
        cr.move_to(g.left, pts[0][1])
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(w - g.right, pts[-1][1])
        line = pal["line"] if self.model.enabled else pal["dim"][:3]
        cr.set_source_rgb(*line)
        cr.set_line_width(2.2)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
        for i, (x, y) in enumerate(pts):
            rad = 7.0 if i in (self.active, self.hover) else 5.5
            cr.arc(x, y, rad, 0, 2 * math.pi)
            cr.set_source_rgb(*pal["point"])
            cr.fill_preserve()
            cr.set_line_width(2.4 if i == self.active else 2.0)
            cr.set_source_rgb(*(pal["gold"] if i == self.active else line))
            cr.stroke()
        if self.active is not None:
            t, p = self.model.points[self.active]
            x, y = pts[self.active]
            layout.set_font_description(theme.mono_font(10.5, "Semi-Bold"))
            layout.set_text(f"{t}° · {pwm_to_pct(p):.0f}%", -1)
            tw, th = layout.get_pixel_size()
            bx, by, bw, bh = bubble_rect(x, y, tw, th, w, h, g.left, g.right)
            _round_rect(cr, bx, by, bw, bh, 7)
            cr.set_source_rgba(*pal["line"], 0.95)
            cr.fill()
            cr.set_source_rgb(1, 0.97, 0.96)
            cr.move_to(bx + 8, by + 4)
            PangoCairo.show_layout(cr, layout)


def _round_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


class KeyboardPreview(Gtk.DrawingArea):
    """Animated approximation of the hardware effect on a stylised key grid.

    Static effects are drawn once (on change, resize or theme switch). Animated ones are
    redrawn from one ~15 fps timer (10 when frames are slow) that exists only while the
    widget is mapped and the window is visible. Key geometry is cached per size and the
    colour is computed once per frame (once per column/row for the rainbow wave)."""

    COLS, ROWS = 15, 5

    def __init__(self) -> None:
        super().__init__()
        self.effect = KbdEffect(0)
        self.brightness = 3
        self.breathe = "alternate"  # how Breathe looks on this keyboard (preview_colour)
        self.add_css_class("kbd-preview")
        self.set_content_height(132)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.gate = FrameGate()
        self._timer: int | None = None
        self._timer_ms = 0
        self._geom: tuple[tuple[int, int], list] | None = None
        self.t0 = time.monotonic()
        self.fixed_t: float | None = None
        self._root = None
        self._root_ids: list[int] = []
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._on_unmap())
        self._style_id = None

    # -- animation ------------------------------------------------------------------------
    def _on_map(self) -> None:
        if self._style_id is None:
            self._style_id = Adw.StyleManager.get_default().connect(
                "notify::dark", lambda *_: self.queue_draw())
        self.queue_draw()
        root = self.get_root()
        if root is not None and root is not self._root:
            self._drop_root()
            self._root = root
            self._root_ids = [root.connect("notify::suspended", lambda *_: self._sync()),
                              root.connect("notify::visible", lambda *_: self._sync())]
        self._sync()

    def _on_unmap(self) -> None:
        if self._style_id is not None:
            Adw.StyleManager.get_default().disconnect(self._style_id)
            self._style_id = None
        self._drop_root()
        self._sync()

    def _drop_root(self) -> None:
        if self._root is not None:
            for i in self._root_ids:
                self._root.disconnect(i)
        self._root, self._root_ids = None, []

    def _shown(self) -> bool:
        r = self._root
        return bool(self.get_mapped() and r is not None and r.get_visible()
                    and not r.get_property("suspended"))

    def _sync(self) -> None:
        want = self._shown() and is_animated(self.effect) \
            and not Adw.StyleManager.get_default().get_high_contrast()
        if want and self._timer is None:
            self.queue_draw()
            self._arm()
        elif not want and self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _arm(self) -> None:
        self._timer_ms = round(self.gate.interval * 1000)
        self._timer = GLib.timeout_add(self._timer_ms, self._on_timer)

    def _on_timer(self) -> bool:
        if self.gate.due(time.monotonic()):
            self.queue_draw()
        if round(self.gate.interval * 1000) != self._timer_ms:  # slow frames: 15 -> 10 fps
            self._arm()
            return False
        return True

    def set_effect(self, effect: KbdEffect, brightness: int, breathe: str = "alternate") -> None:
        if effect == self.effect and brightness == self.brightness and breathe == self.breathe:
            return  # dirty flag: same colours, same frame
        self.effect, self.brightness, self.breathe = effect, brightness, breathe
        self.gate.wake()
        self.queue_draw()
        self._sync()

    # -- drawing ----------------------------------------------------------------------------
    def _layout(self, w: int, h: int) -> list:
        if self._geom is None or self._geom[0] != (w, h):
            pad, gap = 14.0, 5.0
            kw = (w - 2 * pad - gap * (self.COLS - 1)) / self.COLS
            kh = (h - 2 * pad - gap * (self.ROWS - 1)) / self.ROWS
            keys = []
            for row in range(self.ROWS):
                for col in range(self.COLS):
                    x, y, ww = pad + col * (kw + gap), pad + row * (kh + gap), kw
                    if row == self.ROWS - 1 and 4 <= col <= 9:  # space bar
                        if col != 4:
                            continue
                        ww = kw * 6 + gap * 5
                    keys.append((x, y, ww, kh, (x + ww / 2) / w, (y + kh / 2) / h))
            self._geom = ((w, h), keys)
        return self._geom[1]

    def _draw(self, _a, cr, w, h):
        start = time.perf_counter()
        t = self.fixed_t if self.fixed_t is not None else time.monotonic() - self.t0
        dark = theme.is_dark()
        _round_rect(cr, 0, 0, w, h, 14)
        cr.set_source_rgb(*((0.07, 0.05, 0.06) if dark else (0.16, 0.12, 0.13)))
        cr.fill()
        eff, bri = self.effect, self.brightness
        wave = eff.mode == 3  # only the rainbow wave depends on the key position
        vertical = eff.direction in (2, 3)
        cache: dict[float, tuple] = {}
        flat = None if wave else preview_colour(eff, t, 0.0, bri, self.breathe)
        cr.set_line_width(1)
        for x, y, ww, kh, px, py in self._layout(w, h):
            if wave:
                pos = round(py if vertical else px, 3)
                col = cache.get(pos)
                if col is None:
                    col = cache[pos] = preview_colour(eff, t, pos, bri)
            else:
                col = flat
            _round_rect(cr, x, y, ww, kh, 4)
            cr.set_source_rgb(0.12 + col[0] * 0.88, 0.10 + col[1] * 0.88, 0.11 + col[2] * 0.88)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.06)
            cr.stroke()
        self.gate.add_cost(time.perf_counter() - start)


class EffectSwatch(KeyboardPreview):
    """A small rounded swatch showing the hardware effect's colour over time (Breathe fades
    between both colours here). Same timer rules as the keyboard preview: it only runs
    while mapped, visible and animated."""

    def __init__(self) -> None:
        super().__init__()
        self.remove_css_class("kbd-preview")
        self.set_content_height(28)
        self.set_content_width(56)
        self.set_hexpand(False)
        self.set_valign(Gtk.Align.CENTER)

    def _draw(self, _a, cr, w, h):
        start = time.perf_counter()
        t = self.fixed_t if self.fixed_t is not None else time.monotonic() - self.t0
        col = preview_colour(self.effect, t, 0.0, max(1, self.brightness), self.breathe)
        _round_rect(cr, 0.5, 0.5, w - 1, h - 1, 8)
        cr.set_source_rgb(*col)
        cr.fill_preserve()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
        cr.set_line_width(1)
        cr.stroke()
        self.gate.add_cost(time.perf_counter() - start)


class PaletteSwatch(Gtk.DrawingArea):
    """Two colours split on the diagonal: the face of a palette button."""

    def __init__(self, a, b) -> None:
        super().__init__(content_width=30, content_height=20)
        self.a, self.b = a, b
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, w, h):
        _round_rect(cr, 0.5, 0.5, w - 1, h - 1, 5)
        cr.save()
        cr.clip_preserve()
        cr.set_source_rgb(*(c / 255 for c in self.a))
        cr.fill()
        cr.move_to(w, 0)
        cr.line_to(w, h)
        cr.line_to(0, h)
        cr.close_path()
        cr.set_source_rgb(*(c / 255 for c in self.b))
        cr.fill()
        cr.restore()
        _round_rect(cr, 0.5, 0.5, w - 1, h - 1, 5)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
        cr.set_line_width(1)
        cr.stroke()


def palette_buttons(palettes, on_pick) -> Gtk.Box:
    """A row of flat two-colour buttons; on_pick(index) when one is pressed."""
    box = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
    for i, (name, a, b) in enumerate(palettes):
        btn = Gtk.Button(child=PaletteSwatch(a, b), tooltip_text=name)
        btn.add_css_class("flat")
        btn.add_css_class("palette-button")
        btn.update_property([Gtk.AccessibleProperty.LABEL], [f"{name} palette"])
        btn.connect("clicked", lambda _b, i=i: on_pick(i))
        box.append(btn)
    return box
