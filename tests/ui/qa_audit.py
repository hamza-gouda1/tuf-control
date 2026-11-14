# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Widget-tree audits of the UI QA lint (needs a display; used inside the harness).

`Auditor.audit(scope, root)` walks the mapped widgets below `root` and reports, through
the `emit(Finding)` callback: labels that are ellipsized or clipped, widgets that stick
out of their parent / the window / a scrolled viewport, unexpected horizontal
scrollbars, siblings that overlap, zero-size or too-small interactive widgets, icon-only
buttons without tooltip or accessible name, and (with a rendered PNG) text whose
contrast is too low. Focus order and focus-ring checks are in `focus_order` / `focus_ring`.
"""
from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from qa_core import (Finding, centred, classify_target, contrast_ratio, overflow_x,  # noqa: E402
                     overflow_y, overlap_area, required_ratio, text_colours)

# -- accessible labels set from Python (GTK exposes no getter) ------------------------------
# Keyed by the C object's address (hash(widget)), not by the Python wrapper: a widget that
# only its container refers to loses its wrapper, and a weak key would lose the label.
A11Y_LABELS: dict = {}


def install_a11y_spy() -> None:
    """Remember accessible labels that code sets through update_property()."""
    orig = Gtk.Accessible.update_property
    if getattr(orig, "_qa_spy", False):
        return

    def spy(self, props, values):
        try:
            for p, v in zip(props, values):
                if p == Gtk.AccessibleProperty.LABEL and v:
                    A11Y_LABELS[hash(self)] = str(v)
        except TypeError:
            pass
        return orig(self, props, values)
    spy._qa_spy = True
    Gtk.Accessible.update_property = spy


def a11y_label(w) -> str:
    return A11Y_LABELS.get(hash(w), "")


# -- helpers --------------------------------------------------------------------------------
def children(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def rect_in(w, root):
    ok, r = w.compute_bounds(root)
    if not ok:
        return None
    return (r.get_x(), r.get_y(), r.get_width(), r.get_height())


def _norm(text: str) -> str:
    return re.sub(r"\d+([.,]\d+)*", "#", text)[:34]


def label_text(w) -> str:
    if isinstance(w, Gtk.Label):
        return w.get_text()
    return ""


def toast_rects(root, win):
    """Mapped toasts occlude page text during their transient fade animation."""
    if not root.get_mapped():
        return []
    if root.__gtype__.name == "AdwToastWidget":
        rect = rect_in(root, win)
        return [rect] if rect is not None else []
    return [rect for child in children(root) for rect in toast_rects(child, win)]


def desc(w) -> str:
    """A stable description: Type.css-classes 'text' (digits collapsed to #), with the
    nearest named ancestor so two identical-looking widgets can be told apart."""
    name = type(w).__name__
    classes = [c for c in w.get_css_classes() if c not in ("background", "view")]
    s = name + ("." + ".".join(classes[:3]) if classes else "")
    t = ""
    if isinstance(w, Gtk.Label):
        t = w.get_text()
    elif isinstance(w, Gtk.Button):
        t = w.get_label() or w.get_icon_name() or ""
        if not t and isinstance(w.get_child(), Gtk.Image):
            t = w.get_child().get_icon_name() or ""
    elif isinstance(w, Gtk.MenuButton):
        t = w.get_label() or w.get_icon_name() or ""
    elif isinstance(w, Gtk.Image):
        t = w.get_icon_name() or ""
    elif isinstance(w, Adw.PreferencesRow):
        t = w.get_title()
    if not t:
        t = w.get_tooltip_text() or ""
    if t:
        s += f" '{_norm(t)}'"
    # nearest ancestor carrying text, for context
    p = w.get_parent()
    for _ in range(6):
        if p is None:
            break
        if isinstance(p, Adw.PreferencesRow) and p.get_title():
            s += f" in '{_norm(p.get_title())}'"
            break
        if isinstance(p, Adw.PreferencesGroup) and p.get_title():
            s += f" in group '{_norm(p.get_title())}'"
            break
        p = p.get_parent()
    return s


def chain(w, n: int = 4) -> str:
    """Type.css-class of the n nearest ancestors (to find which node a style hits)."""
    out = []
    for a in list(ancestors(w))[:n]:
        cl = [c for c in a.get_css_classes() if c not in ("background",)]
        out.append(type(a).__name__ + ("." + cl[0] if cl else ""))
    return " < ".join(out)


def ancestors(w):
    p = w.get_parent()
    while p is not None:
        yield p
        p = p.get_parent()


def has_ancestor(w, *types) -> bool:
    return any(isinstance(a, types) for a in ancestors(w))


def has_toast(w) -> bool:
    return any(a.__gtype__.name == "AdwToastWidget" for a in ancestors(w))


def tooltip_of(w) -> str:
    """Own tooltip, or the nearest ancestor's (rows put one tooltip on the whole row)."""
    for x in (w, *ancestors(w)):
        t = x.get_tooltip_text() or x.get_tooltip_markup()
        if t:
            return t
        if isinstance(x, (Gtk.ScrolledWindow, Gtk.Window)):
            break
    return ""


INTERACTIVE = (Gtk.Button, Gtk.Switch, Gtk.CheckButton, Gtk.Scale, Gtk.DropDown,
               Gtk.MenuButton, Gtk.SpinButton, Gtk.SearchEntry, Gtk.Entry)
# containers whose children may legitimately overlap or leave the parent
NO_PARENT_CHECK = (Gtk.ScrolledWindow, Gtk.Overlay, Gtk.Popover, Gtk.Fixed, Gtk.ListView,
                   Gtk.ColumnView, Gtk.GridView, Adw.Clamp, Adw.Carousel, Gtk.Scrollbar,
                   Gtk.DrawingArea, Gtk.Window, Adw.Dialog, Adw.ToastOverlay,
                   Adw.NavigationSplitView, Adw.OverlaySplitView, Adw.Bin)
NO_SIBLING_CHECK = (Gtk.Overlay, Gtk.Stack, Gtk.Fixed, Gtk.FlowBox, Gtk.ListBox,
                    Gtk.ScrolledWindow, Gtk.Viewport, Gtk.Popover, Gtk.ListView,
                    Gtk.ColumnView, Gtk.Revealer)
ICONISH = (Gtk.Image,)


def is_icon_only(w) -> bool:
    if isinstance(w, Gtk.MenuButton):
        if w.get_label():
            return False
        child = w.get_child()
        if isinstance(child, Gtk.Image) or (child is None and w.get_icon_name()):
            return True
        return child is None and not w.get_label() and not isinstance(
            w.get_child(), Gtk.Label)
    if isinstance(w, Gtk.Button) and not isinstance(w, Gtk.MenuButton):
        if w.get_label():
            return False
        child = w.get_child()
        if isinstance(child, Adw.ButtonContent):
            return not child.get_label()
        return isinstance(child, Gtk.Image) or (child is None and bool(w.get_icon_name()))
    return False


class Auditor:
    def __init__(self, win, emit, combo_flags: dict | None = None) -> None:
        self.win = win
        self.emit = emit
        self.flags = combo_flags or {}

    def f(self, kind, sev, scope, w, msg) -> None:
        self.emit(Finding(kind, sev, scope, desc(w), msg))

    # -- the walk ---------------------------------------------------------------------------
    def audit(self, scope: str, root, skip=(), clip=None, png=None,
              structural: bool = True) -> int:
        """Audit `root` and everything mapped below it; returns the widget count."""
        win = self.win
        wr = (0.0, 0.0, float(win.get_width()), float(win.get_height()))
        n = [0]
        labels: list = []

        def visit(w, parent, pr, viewport):
            if not w.get_mapped() or w in skip:
                return
            if isinstance(w, Gtk.Popover):
                return
            r = rect_in(w, win)
            if r is None:
                return
            n[0] += 1
            if structural and w.__gtype__.name not in ("GtkGizmo", "GtkBuiltinIcon"):
                self.check_widget(scope, w, r, parent, pr, viewport, wr)
            if isinstance(w, Gtk.Label) and w.get_text().strip() and w.is_sensitive():
                labels.append((w, r, viewport))
            vp = viewport
            if isinstance(w, Gtk.ScrolledWindow):
                vp = r
                if structural:
                    self.check_scrolled(scope, w)
            for c in children(w):
                visit(c, w, r, vp)

        start_parent = root.get_parent()
        visit(root, start_parent, rect_in(start_parent, win) if start_parent else wr, clip)
        if structural:
            self.check_siblings(scope, root, skip)
        if png is not None:
            self.check_contrast(scope, labels, png)
        return n[0]

    # -- per widget -------------------------------------------------------------------------
    def check_widget(self, scope, w, r, parent, pr, viewport, wr) -> None:
        x, y, ww, hh = r
        interactive = isinstance(w, INTERACTIVE)
        inner_btn = isinstance(w, Gtk.Button) and has_ancestor(
            w, Gtk.SpinButton, Gtk.Entry, Gtk.SearchEntry, Gtk.Scrollbar, Gtk.Scale, Gtk.MenuButton,
            Adw.ComboRow, Adw.SpinRow, Adw.EntryRow, Gtk.DropDown, Gtk.ColorDialogButton,
            Gtk.FontDialogButton, Adw.Banner, Adw.ToastOverlay, Adw.ExpanderRow, Gtk.Popover,
            Adw.WindowTitle)
        if isinstance(w, Gtk.ToggleButton) and isinstance(w.get_parent(), Gtk.MenuButton):
            inner_btn = True
        # inside the window?
        if viewport is None and (x < wr[0] - 1.5 or y < wr[1] - 1.5 or x + ww > wr[2] + 1.5
                                 or y + hh > wr[3] + 1.5) and (ww > 0 and hh > 0) \
                and not isinstance(w, (Adw.Dialog, Gtk.Window)):
            if not has_ancestor(w, Adw.Dialog, Adw.ToastOverlay):
                self.f("outside-window", "fail", scope, w,
                       f"allocated {x:.0f},{y:.0f} {ww:.0f}x{hh:.0f} outside the "
                       f"{wr[2]:.0f}x{wr[3]:.0f} window")
        # sticking out of the parent / viewport
        if parent is not None and pr is not None and not isinstance(parent, NO_PARENT_CHECK):
            ox = overflow_x(r, pr)
            if ox > 2 and ww > 0:
                self.f("overflows-parent", "fail", scope, w,
                       f"sticks out of its parent {type(parent).__name__} by {ox:.0f}px "
                       f"horizontally (child {ww:.0f}px, parent {pr[2]:.0f}px)")
            elif isinstance(parent, (Gtk.Box, Gtk.CenterBox, Gtk.Grid)):
                oy = overflow_y(r, pr)
                if oy > 2 and hh > 0:
                    self.f("overflows-parent", "warn", scope, w,
                           f"sticks out of its parent {type(parent).__name__} by {oy:.0f}px "
                           "vertically")
        if viewport is not None and not isinstance(w, (Gtk.Viewport, Gtk.ScrolledWindow)):
            ox = overflow_x(r, viewport, tol=2.5)
            if ox > 2 and ww > 0:
                self.f("clipped-by-viewport", "fail", scope, w,
                       f"{ox:.0f}px of a {ww:.0f}px wide widget is cut off by the scrolled "
                       f"window ({viewport[2]:.0f}px wide)")
        if isinstance(w, Gtk.Label):
            self.check_label(scope, w, r)
        if interactive and not inner_btn:
            self.check_interactive(scope, w, r)
        if isinstance(w, Gtk.Image) and w.get_pixel_size() == 0:
            pass

    def check_label(self, scope, lab, r) -> None:
        text = lab.get_text()
        if not text.strip():
            return
        if has_ancestor(lab, Adw.EntryRow):
            return  # libadwaita's floating title: scaled by an animation, not ours to size
        w, h = r[2], r[3]
        layout = lab.get_layout()
        if layout is not None and layout.is_ellipsized():
            tip = tooltip_of(lab) or (
                "toast" if has_toast(lab) else "")  # libadwaita truncates toast titles itself
            if tip:
                self.f("ellipsized", "info", scope, lab,
                       f"'{text[:50]}' is ellipsized at {w:.0f}px but has a tooltip")
            else:
                self.f("ellipsized", "fail", scope, lab,
                       f"'{text[:50]}' is ellipsized at {w:.0f}px with no tooltip showing "
                       "the full text")
            return
        mn, _nat, _a, _b = lab.measure(Gtk.Orientation.HORIZONTAL, -1)
        if w + 1.0 < mn and lab.get_ellipsize() == Pango.EllipsizeMode.NONE:
            self.f("clipped-label", "fail", scope, lab,
                   f"'{text[:50]}' needs {mn}px but is allocated {w:.0f}px")
            return
        lh = layout.get_pixel_size()[1] if layout is not None else 0
        if lh > h + 2.0 and not lab.get_lines() > 0:
            self.f("clipped-label", "fail", scope, lab,
                   f"'{text[:50]}' is {lh}px tall but is allocated {h:.0f}px")

    def check_interactive(self, scope, w, r) -> None:
        _x, _y, ww, hh = r
        if ww < 1 or hh < 1:
            self.f("zero-size", "fail", scope, w, f"interactive widget is {ww:.0f}x{hh:.0f}")
            return
        if isinstance(w, (Gtk.Scale,)):
            # the track is thin on purpose; the slider (and the row) are the target
            hh = max(hh, 24) if w.get_orientation() == Gtk.Orientation.HORIZONTAL else hh
            ww = max(ww, 24) if w.get_orientation() == Gtk.Orientation.VERTICAL else ww
        in_row = has_ancestor(w, Adw.PreferencesRow, Adw.ActionRow, Gtk.ListBoxRow)
        sev = classify_target(ww, hh)
        if isinstance(w, (Gtk.Switch, Gtk.CheckButton)) and in_row and sev == "warn":
            sev = None  # the whole activatable row is the touch target
        if isinstance(w, (Gtk.Entry, Gtk.SearchEntry)) and sev == "warn":
            sev = None
        if sev:
            kind = "small-target" if sev == "fail" else "touch-target"
            self.f(kind, sev, scope, w,
                   f"{ww:.0f}x{hh:.0f}px (min {24 if sev == 'fail' else 32}px)")
        if isinstance(w, (Gtk.CheckButton, Gtk.Switch, Gtk.Scale, Gtk.ToggleButton, Gtk.DropDown,
                          Gtk.SpinButton)) and not is_icon_only(w) \
                and not has_ancestor(w, Adw.SwitchRow, Adw.ComboRow, Adw.SpinRow,
                                     Adw.EntryRow, Adw.ExpanderRow, Gtk.SpinButton, Gtk.ColorDialogButton):
            named = (isinstance(w, (Gtk.CheckButton, Gtk.ToggleButton)) and w.get_label()) \
                or isinstance(w, Gtk.ToggleButton) and w.get_child() is not None \
                or a11y_label(w) or (isinstance(w, Gtk.CheckButton) and w.get_child() is not None)
            if not named:
                self.f("unlabelled-control", "fail", scope, w,
                       "control has no text and no accessible label (a screen reader "
                       "announces it without a name)")
        if is_icon_only(w):
            if not (w.get_tooltip_text() or w.get_tooltip_markup()):
                self.f("icon-button-tooltip", "fail", scope, w,
                       "icon-only button has no tooltip")
            if not a11y_label(w):
                self.f("icon-button-name", "fail", scope, w,
                       "icon-only button has no accessible label (set it with "
                       "update_property(LABEL))")

    def check_scrolled(self, scope, sw) -> None:
        if sw.get_policy()[0] == Gtk.PolicyType.NEVER:
            return
        adj = sw.get_hadjustment()
        if adj.get_upper() > adj.get_page_size() + 1.5 and sw.get_mapped():
            self.f("hscroll", "fail", scope, sw,
                   f"horizontal scrollbar appears (content {adj.get_upper():.0f}px in "
                   f"{adj.get_page_size():.0f}px)")

    def check_siblings(self, scope, root, skip) -> None:
        win = self.win

        def visit(w):
            if not w.get_mapped() or w in skip or isinstance(w, Gtk.Popover):
                return
            if isinstance(w, (Gtk.Box, Gtk.CenterBox, Gtk.Grid)) and not isinstance(
                    w, NO_SIBLING_CHECK):
                kids = [(c, rect_in(c, win)) for c in children(w)
                        if c.get_mapped() and not isinstance(c, Gtk.Popover)]
                kids = [(c, r) for c, r in kids if r and r[2] > 2 and r[3] > 2]
                for i in range(len(kids)):
                    for j in range(i + 1, len(kids)):
                        a, ra = kids[i]
                        b, rb = kids[j]
                        area = overlap_area(ra, rb)
                        # linked button groups share their 1 px borders on purpose
                        ow = min(ra[0] + ra[2], rb[0] + rb[2]) - max(ra[0], rb[0])
                        oh = min(ra[1] + ra[3], rb[1] + rb[3]) - max(ra[1], rb[1])
                        if area > 12 and min(ow, oh) > 2.5:
                            self.f("overlap", "fail", scope, a,
                                   f"overlaps sibling {desc(b)} by {area:.0f}px²")
            for c in children(w):
                visit(c)
        visit(root)

    def audit_blank(self, scope, page, png, texts: dict) -> None:
        """Record what a page says; warn when it draws (almost) nothing."""
        r = rect_in(page, self.win)
        if r is None:
            return
        found = []

        def visit(w):
            if not w.get_mapped():
                return
            if isinstance(w, Gtk.Label) and w.get_text().strip():
                found.append(w.get_text().strip().replace("\n", " ")[:70])
            for c in children(w):
                visit(c)
        visit(page)
        texts[scope] = found[:14]
        box = (max(0, int(r[0])), max(0, int(r[1])), min(png.size[0], int(r[0] + r[2])),
               min(png.size[1], int(r[1] + r[3])))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            return
        crop = png.crop(box).convert("RGB")
        cols = crop.getcolors(maxcolors=1 << 20) or []
        total = (box[2] - box[0]) * (box[3] - box[1])
        top = max((c for c, _ in cols), default=total)
        if len(found) < 2 or top / total > 0.995:
            self.emit(Finding("blank-page", "fail", scope, "page",
                              f"page draws almost nothing ({len(found)} labels, "
                              f"{100 * (1 - top / total):.1f}% non-background pixels)"))

    # -- contrast ---------------------------------------------------------------------------
    def check_contrast(self, scope, labels, png) -> None:
        """`png` is a PIL image of the whole window (1 px = 1 logical px)."""
        W, H = png.size
        occluders = toast_rects(self.win, self.win)
        for lab, r, vp in labels:
            if any(overlap_area(r, box) > 0 for box in occluders):
                continue  # this label is hidden by the transient toast surface
            if has_toast(lab):
                continue  # toasts fade in and out: mid-animation the surface is
                          # translucent and pixel sampling measures the blend, not
                          # the style; geometry checks already cover toasts
            if vp is not None:  # only text fully inside the visible part of a scrolled area
                if (r[1] < vp[1] - 1 or r[1] + r[3] > vp[1] + vp[3] + 1
                        or r[0] < vp[0] - 1 or r[0] + r[2] > vp[0] + vp[2] + 1):
                    continue
            if r[0] < -1 or r[1] < -1 or r[0] + r[2] > W + 1 or r[1] + r[3] > H + 1:
                continue  # partly off the window: reported by the geometry checks
            x0, y0 = max(0, int(r[0])), max(0, int(r[1]))
            x1, y1 = min(W, int(r[0] + r[2])), min(H, int(r[1] + r[3]))
            if x1 - x0 < 3 or y1 - y0 < 3:
                continue
            if x0 >= W or y0 >= H:
                continue
            crop = png.crop((x0, y0, x1, y1)).convert("RGB")
            cols = crop.getcolors(maxcolors=1 << 16)
            if cols is None:
                continue  # a gradient / photograph behind the text: not measurable
            tc = text_colours(cols)
            if tc is None:
                continue
            bg, fg = tc
            ratio = contrast_ratio(fg, bg)
            fd = lab.get_pango_context().get_font_description()
            size_px = fd.get_size() / Pango.SCALE
            if not fd.get_size_is_absolute():
                size_px *= 96 / 72
            bold = int(fd.get_weight()) >= 600
            need = required_ratio(size_px, bold)
            if ratio < need - 0.05:
                sev = "fail" if ratio < need - 0.5 else "warn"
                self.f("contrast", sev, scope, lab,
                       f"{ratio:.2f}:1 (needs {need}:1) fg #{fg[0]:02x}{fg[1]:02x}{fg[2]:02x} "
                       f"on #{bg[0]:02x}{bg[1]:02x}{bg[2]:02x} [{chain(lab)}]")


# -- window-level checks ---------------------------------------------------------------------
def min_width_culprits(root, limit: int, n: int = 3) -> list:
    """The deepest widgets under `root` whose own minimum width exceeds `limit`: what
    actually forces the page wider than the window."""
    out = []

    def visit(w):
        if not w.get_visible():
            return 0
        mn = w.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        deeper = False
        if mn > limit:
            for c in children(w):
                if visit(c) > limit:
                    deeper = True
            if not deeper and w.get_child_visible():
                out.append((mn, desc(w)))
        return mn
    visit(root)
    return [f"{d} needs {mn}px" for mn, d in sorted(out, reverse=True)[:n]]


def check_min_width(emit, win, scope: str, want_w: int, want_h: int, page=None) -> None:
    """The visible page (with the header and sidebar logic around it) must fit the window.
    AdwApplicationWindow keeps its requested size and clips a child that needs more, so
    the content's own minimum width is what is compared with the window width."""
    w = win.get_width()
    mn = win.toasts.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    if mn > w + 1:
        why = ""
        if page is not None:
            why = " Widest: " + "; ".join(min_width_culprits(page, w - 40))
        emit(Finding("min-width", "fail", scope, "window",
                     f"content needs at least {mn}px but the window is {w}px wide: the "
                     f"right edge is cut off.{why}"))
    if not (win.is_maximized() or win.is_fullscreen()) and w > want_w + 1:
        emit(Finding("min-width", "fail", scope, "window",
                     f"window is {w}px wide but {want_w}px was requested"))


def focus_order(emit, win, scope: str, root, expected) -> list:
    """Keyboard focus sanity, from the widget tree (the headless compositor never gives the
    window keyboard focus, and GtkListBox then refuses to step off a row, so Tab cannot
    be driven; see the module docs). Every interactive widget must be focusable all the
    way up (no `can-focus: false` ancestor), and the Tab chain (tree order) must read
    top to bottom: a widget that comes later in the chain but sits clearly above the
    previous one is a jumping focus order."""
    seq = []
    for w in expected:
        r = rect_in(w, win)
        if r is None or isinstance(w, Gtk.Editable):  # entries hand focus to their Text
            continue
        chain = w.get_can_focus() and w.get_focusable() and all(
            a.get_can_focus() for a in ancestors(w))
        row_ok = any(isinstance(a, Gtk.ListBoxRow) and a.get_focusable() and a.get_can_focus()
                     for a in ancestors(w))  # AdwSwitchRow: the row takes focus for its switch
        if not chain and not row_ok:
            emit(Finding("focus-unreachable", "fail", scope, desc(w),
                         "interactive widget cannot take keyboard focus"))
        if not has_ancestor(w, Adw.HeaderBar):  # header bars sit outside the reading order
            seq.append((w, r))
    prev = None
    for w, r in seq:
        if prev is not None:
            pw, pr = prev
            # reading order: a later stop may not sit more than a row above the previous
            # one, except that the page scrolled (different scrolled content)
            if r[1] + r[3] < pr[1] - 8 and not r[0] > pr[0] + pr[2] + 8:
                emit(Finding("focus-order", "warn", scope, desc(w),
                             f"Tab reaches this after {desc(pw)} although it is {pr[1] - r[1]:.0f}px "
                             "higher on screen"))
        prev = (w, r)
    return [w for w, _r in seq]


def interactive_widgets(root) -> list:
    out = []

    def visit(w):
        if not w.get_mapped() or isinstance(w, Gtk.Popover):
            return
        if isinstance(w, INTERACTIVE) and w.get_sensitive() \
                and not isinstance(w.get_parent(), Gtk.MenuButton) \
                and not has_ancestor(w, Gtk.SpinButton, Gtk.Entry, Gtk.SearchEntry):
            out.append(w)
        for c in children(w):
            visit(c)
    visit(root)
    return out


def dialog_geometry(emit, win, scope: str, dlg, content) -> None:
    """A dialog sits inside the window, centred (or a bottom sheet when narrow)."""
    wr = (0.0, 0.0, float(win.get_width()), float(win.get_height()))
    r = rect_in(content, win)
    if r is None:
        return
    if r[0] < -1 or r[1] < -1 or r[0] + r[2] > wr[2] + 1 or r[1] + r[3] > wr[3] + 1:
        emit(Finding("dialog-bounds", "fail", scope, type(dlg).__name__,
                     f"dialog {r[2]:.0f}x{r[3]:.0f} at {r[0]:.0f},{r[1]:.0f} is outside the "
                     f"{wr[2]:.0f}x{wr[3]:.0f} window"))
    narrow = wr[2] < 450
    if not narrow and not centred(r, wr, tol=40):
        emit(Finding("dialog-centre", "warn", scope, type(dlg).__name__,
                     f"dialog at {r[0]:.0f},{r[1]:.0f} {r[2]:.0f}x{r[3]:.0f} is not centred "
                     f"in the {wr[2]:.0f}x{wr[3]:.0f} window"))
    if r[2] > wr[2] * 0.95 and not narrow:
        emit(Finding("dialog-size", "warn", scope, type(dlg).__name__,
                     f"dialog is {r[2]:.0f}px wide in a {wr[2]:.0f}px window"))


def check_header(emit, win) -> None:
    """Profile chip, battery readout, title and window buttons share one bar: they must
    not overlap each other or leave the window."""
    items = {"profile chip": win.chip, "battery": win.pw_box, "title": win.title}
    stack = [win.content_tv]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.WindowControls) and w.get_mapped():
            items.setdefault("window controls", w)
        stack.extend(children(w))
    rects = {}
    for k, w in items.items():
        if w.get_mapped():
            r = rect_in(w, win)
            if r and r[2] > 1:
                rects[k] = r
    ww = win.get_width()
    names = list(rects)
    for i, a in enumerate(names):
        ra = rects[a]
        if ra[0] < -1 or ra[0] + ra[2] > ww + 1:
            emit(Finding("header-bounds", "fail", "shell", a,
                         f"{a} at {ra[0]:.0f}..{ra[0] + ra[2]:.0f} is outside the {ww}px window"))
        for b in names[i + 1:]:
            if overlap_area(ra, rects[b]) > 4:
                emit(Finding("header-overlap", "fail", "shell", f"{a} / {b}",
                             f"{a} {ra[0]:.0f}+{ra[2]:.0f} overlaps {b} "
                             f"{rects[b][0]:.0f}+{rects[b][2]:.0f}"))
    for lab in _labels(win.title):
        if lab.get_layout() is not None and lab.get_layout().is_ellipsized():
            emit(Finding("header-title", "warn", "shell", "title",
                         f"page title '{lab.get_text()}' is ellipsized in the header"))


def _labels(root):
    out = []
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.Label) and w.get_mapped():
            out.append(w)
        stack.extend(children(w))
    return out
