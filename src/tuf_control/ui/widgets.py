"""Custom widgets: sparkline, stat tile, fan-curve canvas, keyboard preview."""
from __future__ import annotations

import math
import time

from gi.repository import Adw, Gtk, Pango, PangoCairo

from . import theme
from .viewmodels import (
    CurveGeometry,
    FanCurveModel,
    History,
    pwm_to_pct,
    smooth_path,
    smooth_values,
)
from ..core import FanCurve, KbdEffect
from .viewmodels import preview_colour


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
        layout.set_font_description(theme.mono_font(8.5))
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
            layout.set_font_description(theme.mono_font(9, "Semi-Bold"))
            layout.set_text(f"{t}° · {pwm_to_pct(p):.0f}%", -1)
            tw, th = layout.get_pixel_size()
            bx = min(max(x - tw / 2 - 8, g.left), w - g.right - tw - 16)
            by = y - th - 22 if y - th - 22 > 2 else y + 14
            _round_rect(cr, bx, by, tw + 16, th + 8, 7)
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
    """Animated approximation of the hardware effect on a stylised key grid."""

    COLS, ROWS = 15, 5

    def __init__(self) -> None:
        super().__init__()
        self.effect = KbdEffect(0)
        self.brightness = 3
        self.add_css_class("kbd-preview")
        self.set_content_height(132)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self._tick = None
        self.t0 = time.monotonic()
        self.fixed_t: float | None = None
        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop())

    def _start(self):
        if self._tick is None and not Adw.StyleManager.get_default().get_high_contrast():
            self._tick = self.add_tick_callback(lambda *_: (self.queue_draw(), True)[1])

    def _stop(self):
        if self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None

    def set_effect(self, effect: KbdEffect, brightness: int) -> None:
        self.effect, self.brightness = effect, brightness
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        t = self.fixed_t if self.fixed_t is not None else time.monotonic() - self.t0
        dark = theme.is_dark()
        _round_rect(cr, 0, 0, w, h, 14)
        cr.set_source_rgb(*((0.07, 0.05, 0.06) if dark else (0.16, 0.12, 0.13)))
        cr.fill()
        pad, gap = 14.0, 5.0
        kw = (w - 2 * pad - gap * (self.COLS - 1)) / self.COLS
        kh = (h - 2 * pad - gap * (self.ROWS - 1)) / self.ROWS
        for row in range(self.ROWS):
            for col in range(self.COLS):
                x = pad + col * (kw + gap)
                y = pad + row * (kh + gap)
                ww = kw
                if row == self.ROWS - 1 and 4 <= col <= 9:  # space bar
                    if col != 4:
                        continue
                    ww = kw * 6 + gap * 5
                pos = (x + ww / 2) / w
                if self.effect.direction in (2, 3):
                    pos = (y + kh / 2) / h
                r, g, b = preview_colour(self.effect, t, pos, self.brightness)
                _round_rect(cr, x, y, ww, kh, 4)
                cr.set_source_rgb(0.12 + r * 0.88, 0.10 + g * 0.88, 0.11 + b * 0.88)
                cr.fill_preserve()
                cr.set_source_rgba(1, 1, 1, 0.06)
                cr.set_line_width(1)
                cr.stroke()
