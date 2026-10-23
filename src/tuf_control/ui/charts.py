"""Cairo chart widgets drawn straight from the telemetry Store's ring buffers.

Charts redraw only when a sample arrives (1-2 Hz) or the pointer moves over them; they
draw nothing while unmapped. Point buffers are reused between frames.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cairo
from gi.repository import Gtk, Pango, PangoCairo

from . import theme
from .chartmath import bezier_controls, nice_range, nice_step, segments, window_points

NAN = math.nan

# extra series colours per scheme (wine palette neighbours)
SERIES = {
    "dark": {"primary": None, "gold": None, "slate": (0.56, 0.68, 0.84),
             "sage": (0.55, 0.76, 0.62), "mauve": (0.76, 0.58, 0.82), "rose": (0.95, 0.62, 0.55),
             "muted": (0.62, 0.55, 0.57)},
    "light": {"primary": None, "gold": None, "slate": (0.24, 0.38, 0.58),
              "sage": (0.20, 0.47, 0.32), "mauve": (0.50, 0.30, 0.58), "rose": (0.78, 0.33, 0.25),
              "muted": (0.52, 0.46, 0.48)},
}


def colour(name: str) -> tuple[float, float, float]:
    pal = theme.colours()
    if name == "primary":
        return pal["line"]
    if name == "gold":
        return pal["gold"]
    return SERIES["dark" if theme.is_dark() else "light"][name]


@dataclass
class Line:
    key: str
    label: str = ""
    color: str = "primary"
    fill: bool = True
    dash: bool = False
    scale: float = 1.0  # value multiplier before plotting (e.g. bytes -> GiB)


_FONT_OPTS = cairo.FontOptions()
_FONT_OPTS.set_antialias(cairo.ANTIALIAS_GRAY)
_FONT_OPTS.set_hint_style(cairo.HINT_STYLE_SLIGHT)


def make_layout(cr):
    layout = PangoCairo.create_layout(cr)
    PangoCairo.context_set_font_options(layout.get_context(), _FONT_OPTS)
    return layout


def _text(cr, layout, text, x, y, rgba, align="left", valign="top"):
    layout.set_text(text, -1)
    w, h = layout.get_pixel_size()
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    if valign == "middle":
        y -= h / 2
    elif valign == "bottom":
        y -= h
    cr.set_source_rgba(*rgba)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return w, h


class Chart(Gtk.DrawingArea):
    """Time-series area chart: grid, y labels, time axis, current value, hover readout.

    compact=True is the dashboard card style (labels inside the plot, no hover);
    otherwise a left gutter holds the y labels and hovering shows exact samples.
    """

    def __init__(self, hub, lines: list[Line], window_s: float = 120.0,
                 y_fixed: tuple[float, float] | None = None, floor: float | None = 0.0,
                 min_span: float = 1.0, fmt=None, stacked: bool = False,
                 compact: bool = False, height: int = 120, use_max: bool = True) -> None:
        super().__init__()
        self.hub, self.lines = hub, lines
        self.window_s = window_s
        self.y_fixed, self.floor, self.min_span = y_fixed, floor, min_span
        self.fmt = fmt or (lambda v: f"{v:g}")
        self.stacked, self.compact, self.use_max = stacked, compact, use_max
        self.set_content_height(height)
        self.set_hexpand(True)
        self.add_css_class("chart")
        self.set_draw_func(self._draw)
        self._xs: list[list[float]] = [[] for _ in lines]
        self._ys: list[list[float]] = [[] for _ in lines]
        self.hover_x: float | None = None
        if not compact:
            mo = Gtk.EventControllerMotion()
            mo.connect("motion", self._motion)
            mo.connect("leave", self._leave)
            self.add_controller(mo)

    def _motion(self, _c, x, _y):
        self.hover_x = x
        self.queue_draw()

    def _leave(self, *_):
        self.hover_x = None
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        store = self.hub.store
        dark = theme.is_dark()
        pal = theme.colours()
        layout = make_layout(cr)
        layout.set_font_description(theme.mono_font(7.5 if self.compact else 8.0))
        now = store.last_t
        t1 = now if now is not None else 0.0
        t0 = t1 - self.window_s
        interval = self.hub.interval_s
        # collect points
        idx = store.index_range(t0 - interval) if now is not None else range(0)
        lo, hi = math.inf, -math.inf
        acc = None
        for li, line in enumerate(self.lines):
            arr = store.series.get(line.key)
            xs, ys = self._xs[li], self._ys[li]
            if arr is None:
                xs.clear()
                ys.clear()
                continue
            window_points(store.times, arr, idx, store.slot, t0, t1, 1.0, xs, ys,
                          bucket_px=1.0 / max(2.0, w / 1.5), use_max=self.use_max,
                          interval=interval)
            if line.scale != 1.0:
                for i, v in enumerate(ys):
                    ys[i] = v * line.scale
            if self.stacked:
                if acc is None:
                    acc = list(ys)
                else:
                    n = min(len(acc), len(ys))
                    for i in range(n):
                        a, v = acc[i], ys[i]
                        acc[i] = (0.0 if a != a else a) + (0.0 if v != v else v) \
                            if (a == a or v == v) else NAN
                        ys[i] = acc[i]
            for v in ys:
                if v == v:
                    if v < lo:
                        lo = v
                    if v > hi:
                        hi = v
        target = 3 if self.compact else max(2, min(4, int((h - 28) / 34)))
        if self.y_fixed:
            ymin, ymax = self.y_fixed
            if hi > ymax:  # never clip a real value
                ymin, ymax, step = nice_range(ymin, hi, self.floor, self.min_span)
            else:
                step = nice_step(ymax - ymin, target)
        else:
            ymin, ymax, step = nice_range(lo if lo != math.inf else 0.0,
                                          hi if hi != -math.inf else 0.0,
                                          self.floor, self.min_span, target)
        ticks = []
        v = ymin
        while v <= ymax + step * 1e-6 and len(ticks) < 12:
            ticks.append(v)
            v = ymin + step * len(ticks)
        texts = [self.fmt(t) for t in ticks]
        lab_w = 0
        for t in texts:
            layout.set_text(t, -1)
            lab_w = max(lab_w, layout.get_pixel_size()[0])
        if self.compact:
            gl, gr, gt, gb = 0.0, lab_w + 8.0, 6.0, 16.0
        else:
            gl, gr, gt, gb = max(30.0, lab_w + 10.0), 8.0, 8.0, 20.0
        pw, ph = max(1.0, w - gl - gr), max(1.0, h - gt - gb)

        def px_y(v):
            return gt + (1 - (v - ymin) / (ymax - ymin)) * ph

        # grid + y labels
        cr.set_line_width(1)
        dim = pal["dim"]
        label_rgba = (*dim[:3], dim[3] * (0.85 if self.compact else 1.0))
        for t, text in zip(ticks, texts):
            y = round(px_y(t)) + 0.5
            cr.set_source_rgba(*pal["grid"])
            cr.move_to(gl, y)
            cr.line_to(gl + pw, y)
            cr.stroke()
            if self.compact:
                _text(cr, layout, text, w, y, label_rgba, "right", "middle")
            else:
                _text(cr, layout, text, gl - 6, y, label_rgba, "right", "middle")
        if ticks and ticks[-1] < ymax - step * 0.05:  # unlabelled top edge (e.g. RAM size)
            y = round(px_y(ymax)) + 0.5
            cr.set_source_rgba(*pal["grid"])
            cr.set_dash([2, 3])
            cr.move_to(gl, y)
            cr.line_to(gl + pw, y)
            cr.stroke()
            cr.set_dash([])
        # time axis
        ty = gt + ph + (2 if self.compact else 5)
        for frac, lab in ((0.0, f"−{int(self.window_s)} s"),
                         (0.5, f"−{int(self.window_s / 2)} s"),
                          (1.0, "now")):
            x = gl + frac * pw
            if not self.compact and 0 < frac < 1:
                cr.set_source_rgba(*pal["grid"])
                cr.move_to(round(x) + 0.5, gt)
                cr.line_to(round(x) + 0.5, gt + ph)
                cr.stroke()
            _text(cr, layout, lab, x, ty, label_rgba,
                  "left" if frac == 0 else ("right" if frac == 1 else "center"))
        # series, back to front so the first line is drawn on top
        cr.save()
        cr.rectangle(gl, gt - 2, pw + 1, ph + 4)
        cr.clip()
        order = list(range(len(self.lines)))
        if self.stacked:
            order.reverse()
        else:
            order.reverse()
        base_y = px_y(max(ymin, 0.0)) if ymin < 0 < ymax else gt + ph
        for li in order:
            line = self.lines[li]
            xs, ys = self._xs[li], self._ys[li]
            if not xs:
                continue
            rgb = colour(line.color)
            for a, b in segments(xs, ys):
                pxs = [gl + x * pw for x in xs[a:b]]
                if b - a == 1:
                    cr.set_source_rgb(*rgb)
                    cr.arc(pxs[0], px_y(ys[a]), 1.8, 0, 2 * math.pi)
                    cr.fill()
                    continue
                vals = ys[a:b]
                curves = bezier_controls(pxs, vals, 0, len(pxs), px_y)

                def path():
                    cr.move_to(pxs[0], px_y(vals[0]))
                    for c in curves:
                        cr.curve_to(*c)

                if line.fill:
                    path()
                    if self.stacked and li > 0:  # fill down to the series below
                        below = self._ys[li - 1][a:b]
                        bx = pxs
                        for i in range(len(bx) - 1, -1, -1):
                            bv = below[i] if i < len(below) else 0.0
                            cr.line_to(bx[i], px_y(bv if bv == bv else 0.0))
                    else:
                        cr.line_to(pxs[-1], base_y)
                        cr.line_to(pxs[0], base_y)
                    cr.close_path()
                    grad = cairo.LinearGradient(0, gt, 0, gt + ph)
                    top = (0.42 if dark else 0.26) if not self.stacked else (0.55 if dark
                                                                             else 0.38)
                    grad.add_color_stop_rgba(0, *rgb, top)
                    grad.add_color_stop_rgba(1, *rgb, top * (0.25 if not self.stacked else 0.7))
                    cr.set_source(grad)
                    cr.fill()
                path()
                cr.set_source_rgb(*rgb)
                cr.set_line_width(1.6 if self.compact else 1.8)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                if line.dash:
                    cr.set_dash([4, 3])
                cr.stroke()
                cr.set_dash([])
        cr.restore()
        # live point on the newest sample of the first line
        if self._xs and self._xs[0]:
            xs, ys = self._xs[0], self._ys[0]
            if ys[-1] == ys[-1]:
                x, y = gl + xs[-1] * pw, px_y(ys[-1])
                cr.set_source_rgb(*colour(self.lines[0].color))
                cr.arc(min(x, gl + pw - 2.5), y, 2.8, 0, 2 * math.pi)
                cr.fill()
        if self.hover_x is not None and not self.compact and now is not None:
            self._draw_hover(cr, layout, store, gl, gt, pw, ph, t0, px_y, pal)

    def _draw_hover(self, cr, layout, store, gl, gt, pw, ph, t0, px_y, pal):
        x = min(max(self.hover_x, gl), gl + pw)
        t = t0 + (x - gl) / pw * self.window_s
        # nearest sample
        best, best_d = None, math.inf
        for k in store.index_range(t - 3 * self.hub.interval_s):
            s = store.slot(k)
            d = abs(store.times[s] - t)
            if d < best_d:
                best, best_d = s, d
            elif store.times[s] > t:
                break
        if best is None or best_d > 2 * self.hub.interval_s:
            return
        sx = gl + (store.times[best] - t0) / self.window_s * pw
        cr.set_source_rgba(*pal["dim"][:3], 0.5)
        cr.set_line_width(1)
        cr.move_to(round(sx) + 0.5, gt)
        cr.line_to(round(sx) + 0.5, gt + ph)
        cr.stroke()
        rows = [f"{store.last_t - store.times[best]:.0f} s ago"]
        for line in self.lines:
            arr = store.series.get(line.key)
            v = arr[best] * line.scale if arr is not None else NAN
            rows.append(f"{line.label + ' ' if line.label else ''}"
                        f"{self.fmt(v) if v == v else '–'}")
        layout.set_text("\n".join(rows), -1)
        tw, th = layout.get_pixel_size()
        bx = sx + 10 if sx + 10 + tw + 12 < gl + pw else sx - tw - 22
        by = gt + 4
        _round_rect(cr, bx, by, tw + 12, th + 8, 6)
        cr.set_source_rgba(*((0.12, 0.08, 0.09, 0.92) if theme.is_dark() else
                             (1, 1, 1, 0.95)))
        cr.fill_preserve()
        cr.set_source_rgba(*pal["grid"][:3], 0.4)
        cr.stroke()
        cr.set_source_rgba(*pal["dim"][:3], 1.0)
        cr.move_to(bx + 6, by + 4)
        PangoCairo.show_layout(cr, layout)


def _round_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


class CoreGrid(Gtk.DrawingArea):
    """All logical CPUs in one widget: label, P/E kind, usage graph, %, MHz and core temp."""

    CELL_W, CELL_H, GAP = 168, 74, 8

    def __init__(self, hub, cpus: list[int], topo: dict, window_s: float = 60.0) -> None:
        super().__init__()
        self.hub, self.cpus, self.topo, self.window_s = hub, cpus, topo, window_s
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.connect("resize", self._resize)
        self._xs: list[float] = []
        self._ys: list[float] = []
        self._cols = 4
        self.set_content_height(self._height(4))

    def _height(self, cols: int) -> int:
        rows = math.ceil(len(self.cpus) / max(1, cols))
        return rows * (self.CELL_H + self.GAP) - self.GAP

    def _resize(self, _a, w, _h):
        cols = max(1, (w + self.GAP) // (self.CELL_W + self.GAP))
        if cols != self._cols:
            self._cols = cols
            self.set_content_height(self._height(cols))

    def _draw(self, _a, cr, w, h):
        store = self.hub.store
        pal = theme.colours()
        dark = theme.is_dark()
        cols = self._cols
        cw = (w - self.GAP * (cols - 1)) / cols
        ch = self.CELL_H
        lay = make_layout(cr)
        small = theme.mono_font(7.5)
        big = theme.mono_font(10.5, "Semi-Bold")
        now = store.last_t
        t1 = now or 0.0
        t0 = t1 - self.window_s
        idx = list(store.index_range(t0 - 2)) if now is not None else []
        line = colour("primary")
        ecol = colour("gold")
        card = (1, 1, 1, 0.035) if dark else (0.2, 0.08, 0.1, 0.035)
        for n, cpu in enumerate(self.cpus):
            r, c = divmod(n, cols)
            x0 = c * (cw + self.GAP)
            y0 = r * (ch + self.GAP)
            _round_rect(cr, x0, y0, cw, ch, 10)
            cr.set_source_rgba(*card)
            cr.fill()
            t = self.topo.get(cpu, {})
            kind = t.get("kind") or ""
            rgb = ecol if kind == "E" else line
            # graph area: lower 40 px
            gx, gy, gw, gh = x0 + 1, y0 + ch - 34, cw - 2, 32
            arr = store.series.get(f"cpu.{cpu}")
            if arr is not None and idx:
                window_points(store.times, arr, idx, store.slot, t0, t1, gw, self._xs, self._ys,
                              bucket_px=2.0, interval=self.hub.interval_s)
                for a, b in segments(self._xs, self._ys):
                    if b - a < 2:
                        continue
                    pts = [(gx + self._xs[i], gy + gh - min(100.0, self._ys[i]) / 100 * gh)
                           for i in range(a, b)]
                    cr.move_to(pts[0][0], gy + gh)
                    for p in pts:
                        cr.line_to(*p)
                    cr.line_to(pts[-1][0], gy + gh)
                    cr.close_path()
                    cr.set_source_rgba(*rgb, 0.30 if dark else 0.20)
                    cr.fill()
                    cr.move_to(*pts[0])
                    for p in pts[1:]:
                        cr.line_to(*p)
                    cr.set_source_rgba(*rgb, 0.95)
                    cr.set_line_width(1.2)
                    cr.stroke()
            # text
            lay.set_font_description(small)
            label = f"CPU {cpu}"
            _text(cr, lay, label, x0 + 10, y0 + 7, pal["dim"])
            if kind:
                tag = f"{kind}-core" + (f" · T{t.get('thread', 0)}" if kind == "P" else "")
                _text(cr, lay, tag, x0 + cw - 9, y0 + 7, (*rgb, 0.9), "right")
            u = store.last(f"cpu.{cpu}")
            lay.set_font_description(big)
            fg = (0.95, 0.91, 0.89, 1) if dark else (0.16, 0.10, 0.12, 1)
            _text(cr, lay, f"{u:.0f}%" if u is not None else "–", x0 + 10, y0 + 20, fg)
            f = store.last(f"cpu.freq.{cpu}")
            core = t.get("core_id")
            tc = store.last(f"temp.core.{core}") if core is not None else None
            lay.set_font_description(small)
            sub = (f"{f / 1000:.2f} GHz" if f else "–") + (f" · {tc:.0f}°" if tc else "")
            _text(cr, lay, sub, x0 + cw - 9, y0 + 24, pal["dim"], "right")


class CompositionBar(Gtk.DrawingArea):
    """A horizontal 100 % bar split into coloured parts: [(fraction, colour name)]."""

    def __init__(self, height: int = 14) -> None:
        super().__init__()
        self.parts: list[tuple[float, str]] = []
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_parts(self, parts: list[tuple[float, str]]) -> None:
        self.parts = parts
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        pal = theme.colours()
        _round_rect(cr, 0, 0, w, h, h / 2)
        cr.set_source_rgba(*pal["grid"][:3], 0.12)
        cr.fill_preserve()
        cr.clip()
        x = 0.0
        for frac, name in self.parts:
            if frac <= 0:
                continue
            ww = frac * w
            cr.rectangle(x, 0, ww, h)
            rgb = colour(name)
            cr.set_source_rgba(*rgb, 0.9)
            cr.fill()
            x += ww


class Meter(Gtk.DrawingArea):
    """Thin usage meter (0-1) for table rows."""

    def __init__(self, width: int = 120, height: int = 6) -> None:
        super().__init__()
        self.value = 0.0
        self.colour = "primary"
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_value(self, v: float, colour_name: str = "primary") -> None:
        self.value, self.colour = max(0.0, min(1.0, v)), colour_name
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        pal = theme.colours()
        _round_rect(cr, 0, 0, w, h, h / 2)
        cr.set_source_rgba(*pal["grid"][:3], 0.14)
        cr.fill()
        if self.value > 0:
            _round_rect(cr, 0, 0, max(h, w * self.value), h, h / 2)
            cr.set_source_rgb(*colour(self.colour))
            cr.fill()


def legend(items: list[tuple[str, str]]) -> Gtk.Box:
    """A row of colour swatches with labels."""
    box = Gtk.Box(spacing=14)
    box.add_css_class("chart-legend")
    for text, name in items:
        sw = Gtk.DrawingArea(content_width=10, content_height=10, valign=Gtk.Align.CENTER)

        def draw(_a, cr, w, h, name=name):
            _round_rect(cr, 0, 0, w, h, 3)
            cr.set_source_rgb(*colour(name))
            cr.fill()
        sw.set_draw_func(draw)
        item = Gtk.Box(spacing=6)
        item.append(sw)
        lab = Gtk.Label(label=text, xalign=0)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        item.append(lab)
        box.append(item)
    return box
