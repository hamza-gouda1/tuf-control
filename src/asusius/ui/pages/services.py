# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Services: systemd units like Mission Center's Services tab (name, state, enabled, PID,
memory), for system or user units, with a filter, details and safe actions.

Built on first visit. While the page is visible it lists the units every 3 s (one
ListUnits call in a worker thread; PID and memory from the cgroup files); when hidden the
rows are dropped and nothing runs. Start/stop/restart/enable/disable go to systemd over
D-Bus behind a confirmation: user units on the session bus, system units on the system bus
where systemd asks polkit (the desktop's own password dialog). Core units are refused.
"""
from __future__ import annotations

from datetime import datetime

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ...services.backend import make_backend
from ...services.units import OPERATIONS, Unit, allowed_ops, counts, filter_units, sort_units
from ...telemetry import fmt
from ..tasks import run_bg
from ..widgets import label
from .base import Page, a11y_name

REFRESH_TICKS = 3  # window ticks (1 s on non-monitor pages) between listings
COLUMNS = [  # key, title, width chars
    ("name", "Name", 0),
    ("state", "State", 9),
    ("enabled", "Enabled", 9),
    ("pid", "PID", 7),
    ("memory", "Memory", 9),
]
STATES = [("all", "All states"), ("running", "Running"), ("failed", "Failed"),
          ("stopped", "Stopped"), ("enabled", "Enabled")]
KINDS = [("service", "Services"), ("timer", "Timers"), ("socket", "Sockets"), ("all", "All units")]
OP_LABELS = {"start": "Start", "stop": "Stop", "restart": "Restart", "enable": "Enable",
             "disable": "Disable"}


class Row(GObject.Object):
    __gtype_name__ = "AsusiusSvcRow"

    def __init__(self, unit: Unit) -> None:
        super().__init__()
        self.unit = unit
        self.cells: dict[str, Gtk.Widget] = {}

    def text(self, col: str) -> str:
        u = self.unit
        if col == "name":
            return u.name
        if col == "state":
            return u.state_text()
        if col == "enabled":
            return u.enabled_text()
        if col == "pid":
            return str(u.pid) if u.pid else ""
        if col == "memory":
            return fmt.bytes_(u.memory) if u.memory else ""
        return ""


class ServicesPage(Page):
    key, title = "services", "Services"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.scope = "system"
        self.backends = {}
        self.units: list[Unit] = []
        self.rows: dict[str, Row] = {}
        self._ticks = 0
        self._busy = False
        self._shown = False
        self.selected: Unit | None = None
        self.last_dialog = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16,
                        margin_start=16, margin_end=16, margin_bottom=12)
        # the search box on its own line: seg + both filters together are wider than the
        # 360 px minimum window
        self.search = Gtk.SearchEntry(placeholder_text="Filter by name, description or PID")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self._rebuild())
        outer.append(self.search)
        bar = Gtk.Box(spacing=8)
        seg = Gtk.Box()
        seg.add_css_class("linked")
        self.scope_btn = {}
        first = None
        for key, text in (("system", "System"), ("user", "User")):
            b = Gtk.ToggleButton(label=text, tooltip_text=f"{text} units")
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._scope_changed, key)
            seg.append(b)
            self.scope_btn[key] = b
        bar.append(seg)
        self.kind = Gtk.DropDown(model=Gtk.StringList.new([t for _k, t in KINDS]),
                                 valign=Gtk.Align.CENTER, hexpand=True)
        self.kind.set_focusable(True)   # GtkDropDown ships with focusable unset
        a11y_name(self.kind, "Unit type")
        self.kind.connect("notify::selected", lambda *_: self._rebuild())
        bar.append(self.kind)
        self.state = Gtk.DropDown(model=Gtk.StringList.new([t for _k, t in STATES]),
                                  valign=Gtk.Align.CENTER, hexpand=True)
        self.state.set_focusable(True)
        a11y_name(self.state, "State filter")
        self.state.connect("notify::selected", lambda *_: self._rebuild())
        bar.append(self.state)
        self.bar = bar
        outer.append(bar)
        self.summary = label("", ("section-caption", "numeric"), wrap=True)
        outer.append(self.summary)

        self.store = Gio.ListStore(item_type=Row)
        self.sel = Gtk.SingleSelection(model=self.store, autoselect=False, can_unselect=True)
        self.sel.connect("notify::selected", lambda *_: self._selection_changed())
        cv = Gtk.ColumnView(model=self.sel, show_row_separators=False, reorderable=False)
        cv.add_css_class("proc-table")
        self.cv = cv
        self.cols = {}
        for key, title, chars in COLUMNS:
            fac = Gtk.SignalListItemFactory()
            fac.connect("setup", self._setup, key, chars)
            fac.connect("bind", self._bind, key)
            fac.connect("unbind", self._unbind, key)
            col = Gtk.ColumnViewColumn(title=title, factory=fac, resizable=True)
            col.set_sorter(Gtk.CustomSorter.new(lambda *_: 0, None))
            if key == "name":
                col.set_expand(True)
            self.cols[key] = col
            cv.append_column(col)
        cv.sort_by_column(self.cols["name"], Gtk.SortType.ASCENDING)
        cv.get_sorter().connect("changed", lambda *_: self._rebuild())
        sw = Gtk.ScrolledWindow(child=cv, vexpand=True)
        sw.add_css_class("proc-frame")
        outer.append(sw)

        # details of the selected unit
        det = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        det.add_css_class("svc-details")
        top = Gtk.Box(spacing=10)
        self.d_title = label("", ("svc-details-title",), hexpand=True,
                             ellipsize=Pango.EllipsizeMode.MIDDLE)
        top.append(self.d_title)
        self.d_state = label("", ("svc-state",), xalign=0.5)
        self.d_state.set_valign(Gtk.Align.CENTER)
        top.append(self.d_state)
        det.append(top)
        self.d_desc = label("", (), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                            selectable=True)
        det.append(self.d_desc)
        self.d_meta = label("", ("section-caption", "numeric"), wrap=True,
                            wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        det.append(self.d_meta)
        acts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                           column_spacing=6, row_spacing=6, homogeneous=False)
        self.op_btns = {}
        for op in OPERATIONS:
            b = Gtk.Button(label=OP_LABELS[op])
            if op == "stop":
                b.add_css_class("destructive-action")
            b.connect("clicked", lambda _b, o=op: self._ask(o))
            acts.append(b)
            self.op_btns[op] = b
        det.append(acts)
        self.d_note = label("", ("section-caption",), wrap=True)
        det.append(self.d_note)
        self.details = det
        outer.append(det)
        self.set_child(outer)
        self.scope_btn["system"].set_active(True)
        win.responsive(self.fit_columns)

    def fit_columns(self, level: int) -> None:
        hide = ({"enabled"} if level >= 2 else set()) | ({"pid"} if level >= 3 else set())
        for key, col in self.cols.items():
            col.set_visible(key not in hide)
        self.kind.set_visible(level < 3)

    # -- visibility -------------------------------------------------------------------
    def backend(self):
        be = self.backends.get(self.scope)
        if be is None:
            be = self.backends[self.scope] = make_backend(self.scope)
        return be

    def on_shown(self) -> None:
        self._shown = True
        super().on_shown()
        if self.loaded:
            self.refresh()

    def on_hidden(self) -> None:
        """Drop the rows; nothing runs until the page is shown again."""
        self._shown = False
        self.store.remove_all()
        self.rows.clear()
        self.units = []

    def on_telemetry(self, _t) -> None:
        if not self._shown or self.win.current != self.key:
            return
        self._ticks += 1
        if self._ticks % REFRESH_TICKS == 0:
            self.refresh()

    # -- data ---------------------------------------------------------------------------
    def refresh(self, force_files: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        scope = self.scope
        be = self.backend()

        def done(units):
            self._busy = False
            if scope != self.scope or not self._shown:
                return
            self.units = units
            self._rebuild()

        def fail(exc):
            self._busy = False
            self.summary.set_label(f"The {scope} unit list could not be read from systemd. "
                                   "Try again by switching the scope or reopening this "
                                   "page.")

        run_bg(be.list_units, force_files, done=done, error=fail)

    def _scope_changed(self, b, key) -> None:
        if not b.get_active() or key == self.scope and self.units:
            return
        self.scope = key
        self.store.remove_all()
        self.rows.clear()
        self.units = []
        self.selected = None
        self.details.set_visible(False)
        if self.loaded:
            self.refresh(force_files=True)

    def _sort(self):
        sorter = self.cv.get_sorter()
        col = sorter.get_primary_sort_column()
        key = next((k for k, c in self.cols.items() if c == col), "name")
        return key, sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING

    def visible_units(self) -> list[Unit]:
        key, desc = self._sort()
        return sort_units(filter_units(self.units, self.search.get_text(),
                                       STATES[self.state.get_selected()][0],
                                       KINDS[self.kind.get_selected()][0]), key, desc)

    def _rebuild(self) -> None:
        units = self.visible_units()
        names = [u.name for u in units]
        old = [self.store.get_item(i).unit.name for i in range(self.store.get_n_items())]
        sel_name = self.selected.name if self.selected else None
        if names == old:  # same rows: update the bound cells in place
            for u in units:
                r = self.rows[u.name]
                r.unit = u
                for col, w in r.cells.items():
                    self._fill(w, r, col)
        else:
            self.rows = {u.name: self.rows.get(u.name) or Row(u) for u in units}
            for u in units:
                self.rows[u.name].unit = u
            self.store.splice(0, self.store.get_n_items(), [self.rows[n] for n in names])
            if sel_name in self.rows:
                self.sel.set_selected(names.index(sel_name))
            else:
                self.sel.set_selected(Gtk.INVALID_LIST_POSITION)
        c = counts(units)
        allc = counts(filter_units(self.units, kind=KINDS[self.kind.get_selected()][0]))
        shown = f"{c['total']} shown of {allc['total']}" if c["total"] != allc["total"] \
            else f"{allc['total']} units"
        self.summary.set_label(f"{shown} · {allc['running']} running · {allc['failed']} "
                               f"failed · {self.scope} units")
        if sel_name and sel_name in self.rows:
            self.selected = self.rows[sel_name].unit
            self._show_details(self.selected, fetch=False)

    # -- factories ----------------------------------------------------------------------
    def _setup(self, _f, item, key, chars):
        if key == "name":
            lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        elif key == "state":
            lab = Gtk.Label(xalign=0.5, width_chars=chars, halign=Gtk.Align.START)
            lab.add_css_class("svc-state")
        else:
            lab = Gtk.Label(xalign=1.0 if key in ("pid", "memory") else 0.0,
                            width_chars=chars)
            lab.add_css_class("numeric")
        item.set_child(lab)

    def _fill(self, w, row: Row, key: str) -> None:
        w.set_label(row.text(key))
        if key == "state":
            for t in ("ok", "err", "warn", "dim"):
                w.remove_css_class(t)
            w.add_css_class(row.unit.state_tone())
        elif key == "name":
            w.set_tooltip_text(row.unit.description or row.unit.name)

    def _bind(self, _f, item, key):
        row = item.get_item()
        w = item.get_child()
        self._fill(w, row, key)
        row.cells[key] = w

    def _unbind(self, _f, item, key):
        row = item.get_item()
        if row is not None:
            row.cells.pop(key, None)

    # -- details and actions -------------------------------------------------------------
    def _selection_changed(self) -> None:
        item = self.sel.get_selected_item()
        if item is None:
            self.selected = None
            self.details.set_visible(False)
            return
        if self.selected is None or self.selected.name != item.unit.name:
            self.selected = item.unit
            self._show_details(item.unit, fetch=True)

    def _show_details(self, u: Unit, fetch: bool) -> None:
        self.details.set_visible(True)
        self.d_title.set_label(u.name)
        self.d_state.set_label(u.state_text())
        for t in ("ok", "err", "warn", "dim"):
            self.d_state.remove_css_class(t)
        self.d_state.add_css_class(u.state_tone())
        self.d_desc.set_label(u.description or "No description")
        meta = [f"{u.active} ({u.sub})", u.enabled_text()]
        if u.pid:
            meta.append(f"PID {u.pid}")
        if u.memory:
            meta.append(fmt.bytes_(u.memory))
        if u.path:
            meta.append(u.path)
        if not fetch:
            meta.extend(getattr(self, "_extra_meta", []))
        self.d_meta.set_label(" · ".join(meta))
        ops = allowed_ops(u)
        notes = []
        for op, b in self.op_btns.items():
            why = ops[op]
            b.set_sensitive(why is None)
            b.set_tooltip_text(why or f"{OP_LABELS[op]} {u.name}")
        crit = ops["stop"]
        if crit and "core part" in crit:
            notes.append("Protected: " + crit + ".")
        if u.scope == "system" and any(v is None for v in ops.values()):
            notes.append("System units ask for your password (polkit).")
        self.d_note.set_label(" ".join(notes))
        self.d_note.set_visible(bool(notes))
        if fetch:
            self._extra_meta = []
            be = self.backend()

            def done(d, name=u.name):
                if not self.selected or self.selected.name != name:
                    return
                extra = []
                if d.get("main_pid") and d["main_pid"] != u.pid:
                    extra.append(f"Main PID {d['main_pid']}")
                if d.get("since_us"):
                    since = datetime.fromtimestamp(d["since_us"] / 1e6)
                    extra.append("since " + since.strftime("%a %d %b %H:%M"))
                if d.get("fragment") and d["fragment"] != u.path:
                    extra.append(d["fragment"])
                self._extra_meta = extra
                self._show_details(self.selected, fetch=False)
            run_bg(be.details, u, done=done, error=lambda e: None)

    def _ask(self, op: str) -> None:
        u = self.selected
        if u is None:
            return
        why = allowed_ops(u)[op]
        if why:
            self.win.toast(f"{u.name}: {why}")
            return
        effects = {
            "start": "starts it now.", "stop": "stops it now; anything that depends on it "
            "stops too.", "restart": "stops and starts it again.",
            "enable": "makes it start automatically (at boot, or at login for user units).",
            "disable": "stops it from starting automatically; it keeps running until stopped.",
        }
        body = f"{OP_LABELS[op]} {u.name} {effects[op]}"
        if u.scope == "system":
            body += "\n\nsystemd asks for your password."
        self.win.confirm(f"{OP_LABELS[op]} {u.name}?", body, OP_LABELS[op],
                         lambda: self._act(u, op), destructive=op in ("stop", "disable"))
        self.last_dialog = getattr(self.win, "last_dialog", None)

    def _act(self, u: Unit, op: str) -> None:
        be = self.backend()
        self.win.run_write(lambda: be.act(u, op), f"{OP_LABELS[op]}: {u.name}",
                           after=lambda _r: GLib.timeout_add(600, self._after_act))

    def _after_act(self) -> bool:
        self.refresh(force_files=True)
        return GLib.SOURCE_REMOVE
