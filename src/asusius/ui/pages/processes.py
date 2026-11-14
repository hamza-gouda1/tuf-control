# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Processes and apps: a virtualised Gtk.ColumnView over a reusable row model.

The walk runs only while this page is visible (every 2 s, on the sampler's tick).
Rows are plain GObjects reused between passes; only rows on screen have widgets
(ColumnView recycles them), and cell text is pushed straight into the bound labels.
Sorting and grouping are done in Python, not with GTK sorters, to keep it cheap.
"""
from __future__ import annotations

import os
import signal

from gi.repository import Gio, GLib, GObject, Gtk, Pango

from ...telemetry import fmt
from ...telemetry.procs import alive, kill
from ..widgets import label
from ..viewmodels import (OWN_APP_ID, OWN_APP_KEYS, column_totals, desktop_index,
                          norm_app_key, signal_refusal)
from .monitor_base import MonitorPage, set_text

COLUMNS = [  # key, title, width chars, tooltip
    ("name", "Name", 0, None),
    ("pid", "PID", 6, None),
    ("cpu", "CPU", 6, "Share of the whole processor (all logical CPUs = 100 %)"),
    ("mem", "Memory", 8, "Private memory: resident minus shared pages (statm)"),
    ("swap", "Swap", 8, "Swapped-out memory of the process (VmSwap in /proc/PID/status)"),
    ("disk", "Drive", 9, "Read + write at the storage layer, per second (your processes only)"),
    ("gpu", "GPU", 5, "Busiest engine on the iGPU (DRM fdinfo) or dGPU"),
    ("vram", "GPU memory", 9, "dGPU memory"),
]
SORT_KEYS = {
    "name": lambda r: r.name.lower(),
    "pid": lambda r: r.pid or 0,
    "cpu": lambda r: r.cpu or 0.0,
    "mem": lambda r: r.mem or 0,
    "swap": lambda r: r.swap or 0,
    "disk": lambda r: (r.io_r or 0) + (r.io_w or 0),
    "gpu": lambda r: r.gpu or 0.0,
    "vram": lambda r: r.vram or 0,
}


def _attach_header_tooltip(header) -> None:
    """Attach the explanatory column text to its visible title label."""
    title = header.get_text().split("\n")[0]
    for _key, column_title, _chars, tip in COLUMNS:
        if column_title == title:
            if tip:
                header.set_tooltip_text(tip)
            return


SYSTEM_KEY = "__system__"


def _ancestors(w):
    p = w.get_parent()
    while p is not None:
        yield p
        p = p.get_parent()


def _sum_opt(values) -> int | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


class Row(GObject.Object):
    __gtype_name__ = "AsusiusProcRow"

    def __init__(self, key, kind: str) -> None:
        super().__init__()
        self.key, self.kind = key, kind
        self.name = ""
        self.icon = None
        self.pid = None
        self.start = None
        self.uid = -1
        self.cmd = ""
        self.cpu = self.gpu = None
        self.mem = self.vram = 0
        self.swap = None
        self.io_r = self.io_w = None
        self.count = 1
        self.children: Gio.ListStore | None = None
        self.child_rows: list[Row] = []
        self.procs: list = []
        self.cells: dict[str, Gtk.Widget] = {}

    def text(self, col: str) -> str:
        if col == "name":
            return self.name + (f"  ({self.count})" if self.kind == "app" and self.count > 1
                                else "")
        if col == "pid":
            return str(self.pid) if self.pid else ""
        if col == "cpu":
            return fmt.pct(self.cpu, 1) if self.cpu is not None else "–"
        if col == "mem":
            return fmt.bytes_(self.mem) if self.mem else "–"
        if col == "swap":
            return fmt.bytes_(self.swap) if self.swap is not None else "–"
        if col == "disk":
            if self.io_r is None and self.io_w is None:
                return "–"
            return fmt.rate((self.io_r or 0) + (self.io_w or 0))
        if col == "gpu":
            return fmt.pct(self.gpu, 0) if self.gpu else ""
        if col == "vram":
            return fmt.bytes_(self.vram) if self.vram else ""
        return ""


class ProcessesPage(MonitorPage):
    key, title = "processes", "Processes"
    rate_s = 1.0

    def __init__(self, win) -> None:
        super().__init__(win)
        # this page manages its own layout: no clamp, the table fills the page
        self.set_child(None)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16,
                        margin_start=16, margin_end=16, margin_bottom=12)
        bar = Gtk.Box(spacing=8)
        self.mode = {}
        first = None
        seg = Gtk.Box()
        seg.add_css_class("linked")
        for key, text in (("apps", "Apps"), ("all", "All processes")):
            b = Gtk.ToggleButton(label=text)
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._mode_changed, key)
            seg.append(b)
            self.mode[key] = b
        bar.append(seg)
        self.search = Gtk.SearchEntry(placeholder_text="Filter by name, command or PID",
                                      hexpand=True,
                                      tooltip_text="Filter by name, command or PID")
        self.search.connect("search-changed", lambda *_: self._rebuild(force=True))
        bar.append(self.search)
        outer.append(bar)
        self.end_btn = Gtk.Button(label="Stop", sensitive=False,
                                  tooltip_text="Ask the selected app or process to quit (SIGTERM)")
        self.end_btn.connect("clicked", lambda *_: self._end_task(signal.SIGTERM))
        self.kill_btn = Gtk.Button(label="Force Stop", sensitive=False,
                                   tooltip_text="Kill the selected app or process (SIGKILL)")
        self.kill_btn.add_css_class("destructive-action")
        self.kill_btn.connect("clicked", lambda *_: self._end_task(signal.SIGKILL))
        sum_row = Gtk.Box(spacing=8)
        self.summary = label("", ("section-caption", "numeric"), hexpand=True, wrap=True,
                             wrap_mode=Pango.WrapMode.WORD_CHAR)
        sum_row.append(self.summary)
        sum_row.append(self.end_btn)  # a second row: the filter keeps its width when narrow
        sum_row.append(self.kill_btn)
        outer.append(sum_row)

        self.root_store = Gio.ListStore(item_type=Row)
        self.tree = Gtk.TreeListModel.new(self.root_store, False, False, self._children)
        self.sel = Gtk.SingleSelection(model=self.tree, autoselect=False, can_unselect=True)
        self.sel.connect("notify::selected", lambda *_: self._selection_changed())
        cv = Gtk.ColumnView(model=self.sel, show_row_separators=False, reorderable=False)
        cv.add_css_class("proc-table")
        self.cv = cv
        self.cols = {}
        for key, title, chars, tip in COLUMNS:
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
        cv.sort_by_column(self.cols["cpu"], Gtk.SortType.DESCENDING)
        sw = Gtk.ScrolledWindow(child=cv, vexpand=True)
        sw.add_css_class("proc-frame")
        outer.append(sw)
        self.note = label("CPU is a share of all logical CPUs. Disk I/O is only readable for "
                          "your own processes. Network use per process needs root (eBPF) "
                          "and is not shown.", ("monitor-note",), wrap=True)
        outer.append(self.note)
        self.set_child(outer)

        self.rows: dict = {}
        self.apps: dict[str, tuple[str, object] | None] = {}  # app id -> (name, gicon)
        self._dindex: dict | None = None
        self._dindex_busy = False
        self._quit_timer = 0
        self._aligned = False
        self.view = "apps"
        self._gen = -1
        self._order: list = []
        self.mode["apps"].set_active(True)
        cv.get_sorter().connect("changed", lambda *_: self._rebuild(force=True))

    def fit_columns(self, level: int) -> None:
        """Fewer columns as the window narrows (Drive, Swap and GPU memory up to 900 sp; PID and GPU too
        up to 460 sp) so the table never needs a horizontal scrollbar."""
        hide = ({"disk", "vram", "swap"} if level >= 1 else set()) | (
            {"pid", "gpu"} if level >= 3 else set())
        for key, col in self.cols.items():
            col.set_visible(key not in hide)

    # -- visibility --------------------------------------------------------------------
    def on_shown(self) -> None:
        self._warm_desktop_index()
        self.hub.want_processes(True)
        super().on_shown()

    def on_hidden(self) -> None:
        """Release everything: the walk stops and the rows are dropped."""
        if self._quit_timer:
            GLib.source_remove(self._quit_timer)
            self._quit_timer = 0
        self.hub.want_processes(False)
        self.root_store.remove_all()
        self.rows.clear()
        self._order = []
        self._gen = -1

    # -- factories -----------------------------------------------------------------------
    def _setup(self, _f, item, key, chars):
        if key == "name":
            exp = Gtk.TreeExpander()
            box = Gtk.Box(spacing=8)
            img = Gtk.Image(pixel_size=16)
            lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            box.append(img)
            box.append(lab)
            exp.set_child(box)
            exp.img, exp.lab = img, lab
            item.set_child(exp)
        else:
            lab = Gtk.Label(xalign=1.0, width_chars=chars)
            lab.add_css_class("numeric")
            item.set_child(lab)

    def _bind(self, _f, item, key):
        tree_row = item.get_item()
        row = tree_row.get_item()
        w = item.get_child()
        if key == "name":
            w.set_list_row(tree_row)
            if row.icon is not None:
                w.img.set_from_gicon(row.icon)
                w.img.set_visible(True)
            else:
                w.img.set_visible(row.kind == "app")
                w.img.set_from_icon_name("application-x-executable-symbolic")
            w.lab.set_label(row.text("name"))
            w.set_tooltip_text(row.cmd or row.text("name"))
            row.cells["name"] = w.lab
        else:
            w.set_label(row.text(key))
            row.cells[key] = w

    def _unbind(self, _f, item, key):
        tree_row = item.get_item()
        row = tree_row.get_item() if tree_row is not None else None
        if row is not None:
            row.cells.pop(key, None)

    def _children(self, item):
        return item.children if item.kind == "app" else None

    # -- data ---------------------------------------------------------------------------
    def _app_info(self, app_id: str, launched: bool = True):
        """(name, icon) from the .desktop file; without one, only a unit that was itself
        started as an app (app-*.scope/.service) counts, so session slices stay out."""
        if app_id in self.apps:
            found = self.apps[app_id]
            return found if found is None or found[1] is not None or launched else None
        found = None
        nk = norm_app_key(app_id)
        if nk in OWN_APP_KEYS:  # this app, whatever launcher or scope name started it
            try:
                info = Gio.DesktopAppInfo.new(OWN_APP_ID + ".desktop")
            except TypeError:
                info = None
            found = (info.get_name() if info else "Asusius",
                     info.get_icon() if info else Gio.ThemedIcon.new(OWN_APP_ID))
            self.apps[app_id] = found
            return found
        for cand in (app_id, app_id.split(".")[-1] if "." in app_id else None,
                     app_id.lower()):
            if not cand:
                continue
            try:
                info = Gio.DesktopAppInfo.new(cand + ".desktop")
            except TypeError:
                info = None
            if info is not None:
                found = (info.get_name(), info.get_icon())
                break
        if found is None:  # by StartupWMClass, executable name or a differently spelled id
            info = self._desktop_index().get(nk)
            if info is not None:
                found = (info.get_name(), info.get_icon())
        for d in ("/etc/xdg/autostart", os.path.expanduser("~/.config/autostart")):
            if found is None and os.path.exists(f"{d}/{app_id}.desktop"):
                info = Gio.DesktopAppInfo.new_from_filename(f"{d}/{app_id}.desktop")
                if info is not None:
                    found = (info.get_name(), info.get_icon())
        if found is None and not launched:
            return None
        if found is None:  # launched as an app (systemd scope) but no .desktop file found
            base = app_id.split(".")[-1] if "." in app_id else app_id
            found = (base.replace("-", " ").replace("_", " ").strip().title() or app_id, None)
        self.apps[app_id] = found
        return found

    def _warm_desktop_index(self) -> None:
        """Pre-index installed .desktop entries off the main thread."""
        if self._dindex is not None or self._dindex_busy:
            return
        self._dindex_busy = True

        def build():
            return desktop_index(
                (info, info.get_id() or "", info.get_startup_wm_class(),
                 info.get_string("Exec") or "")
                for info in Gio.AppInfo.get_all()
                if isinstance(info, Gio.DesktopAppInfo))

        def done(idx):
            self._dindex_busy = False
            if self._dindex is None:
                self._dindex = idx
                self.apps.clear()
                if self.hub.procs:
                    self._rebuild(force=True)

        from ..tasks import run_bg
        run_bg(build, done=done, error=lambda _e: setattr(self, "_dindex_busy", False))

    def _desktop_index(self) -> dict:
        """Return the cached desktop index, warming in background if not ready yet."""
        if self._dindex is None:
            self._warm_desktop_index()
            return {}
        return self._dindex

    def _mode_changed(self, b, key):
        if b.get_active() and key != self.view:
            self.view = key
            self.root_store.remove_all()
            self.rows.clear()
            self._order = []
            self._rebuild(force=True)

    def update(self) -> None:
        if self.hub.procs_gen != self._gen:
            self._rebuild()

    def _sort(self):
        sorter = self.cv.get_sorter()
        col = sorter.get_primary_sort_column()
        key = next((k for k, c in self.cols.items() if c == col), "cpu")
        desc = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        return SORT_KEYS[key], desc

    def _proc_row(self, p) -> Row:
        key = (p.pid, p.start)
        r = self.rows.get(key)
        if r is None:
            r = self.rows[key] = Row(key, "proc")
            r.pid, r.start, r.uid = p.pid, p.start, p.uid
            r.name = f"[{p.name}]" if p.kthread else p.name
            r.cmd = p.cmd
        r.cpu, r.mem, r.io_r, r.io_w = p.cpu, p.mem, p.io_read, p.io_write
        r.swap = p.swap
        r.gpu, r.vram = p.gpu, p.vram
        r.procs = [p]
        return r

    def _rebuild(self, force: bool = False) -> None:
        self._gen = self.hub.procs_gen
        procs = self.hub.procs
        if not procs:
            set_text(self.summary, "Reading processes…")
            return
        q = self.search.get_text().strip().lower()
        keyf, desc = self._sort()
        by_pid = {p.pid: p for p in procs}
        total_cpu = sum(p.cpu or 0 for p in procs)
        live = set()
        if self.view == "all":
            rows = []
            for p in procs:
                if q and q not in p.name.lower() and q not in p.cmd.lower() \
                        and q != str(p.pid):
                    continue
                r = self._proc_row(p)
                live.add(r.key)
                rows.append(r)
            rows.sort(key=keyf, reverse=desc)
            set_text(self.summary, f"{len(procs)} processes · "
                     f"{sum(p.threads for p in procs)} threads · CPU {total_cpu:.1f} %")
        else:
            groups: dict[str, list] = {}
            for p in procs:
                app = p.app
                hops, cur = 0, p
                while app is None and hops < 8 and cur.ppid in by_pid and cur.ppid > 1:
                    cur = by_pid[cur.ppid]
                    app = cur.app
                    hops += 1
                if app is None:
                    continue
                groups.setdefault(app, []).append(p)
            rows = []
            n_procs = 0
            assigned: set[int] = set()
            for app, members in groups.items():
                info = self._app_info(app, any((m.unit or "").startswith("app-")
                                               for m in members))
                if info is None:
                    continue
                name = info[0]
                if q and q not in name.lower() and q not in app.lower() and not any(
                        q in m.name.lower() or q == str(m.pid) for m in members):
                    continue
                r = self.rows.get(app)
                if r is None:
                    r = self.rows[app] = Row(app, "app")
                    r.name, r.icon = name, info[1]
                    r.children = Gio.ListStore(item_type=Row)
                    r.cmd = app
                live.add(app)
                n_procs += self._fill_group(r, members, keyf, desc, live)
                assigned.update(m.pid for m in members)
                rows.append(r)
            rows.sort(key=keyf, reverse=desc)
            others = [p for p in procs if p.pid not in assigned and not p.kthread and (
                not q or q in p.name.lower() or q in p.cmd.lower() or q == str(p.pid))]
            if others:  # everything that is not an app, in one group at the bottom
                r = self.rows.get(SYSTEM_KEY)
                if r is None:
                    r = self.rows[SYSTEM_KEY] = Row(SYSTEM_KEY, "app")
                    r.name, r.cmd = "System processes", "Services and background processes"
                    r.children = Gio.ListStore(item_type=Row)
                live.add(SYSTEM_KEY)
                self._fill_group(r, others, keyf, desc, live)
                r.count = len(others)
                rows.append(r)
            n_apps = len(rows) - (1 if others else 0)
            set_text(self.summary, f"{n_apps} apps · {n_procs} of {len(procs)} processes · "
                     f"CPU {total_cpu:.1f} %")
        for k in [k for k in self.rows if k not in live]:
            del self.rows[k]
        if force or [id(r) for r in rows] != self._order:
            self._splice(rows)
        for r in self.rows.values():
            for col, w in r.cells.items():
                text = r.text(col)
                if w.get_label() != text:
                    w.set_label(text)
        self._update_titles(total_cpu)

    def _update_titles(self, total_cpu: float) -> None:
        """Column headers carry the machine-wide figure under the name, like Mission
        Center ("CPU / 4%"): CPU, memory, swap, drive busy, GPU busy, VRAM used."""
        st, info = self.hub.store, self.hub.info
        mem = info.get("mem") or {}
        tot = mem.get("total") or 0
        memp = 100.0 * (tot - (mem.get("available") or 0)) / tot if tot else None
        swp = 100.0 * (mem.get("swap_used") or 0) / mem["swap_total"] \
            if mem.get("swap_total") else None
        disks = (info.get("disk") or {}).get("disks") or {}
        busy = [b for b in (st.last(f"disk.{n}.busy") for n in disks) if b is not None]
        gpus = [g for g in (st.last("igpu.util"), st.last("dgpu.util")) if g is not None]
        q = (info.get("dgpu") or {}).get("q") or {}
        vram = 100.0 * q["vram_used"] / q["vram_total"] \
            if q.get("vram_used") is not None and q.get("vram_total") else None
        tots = column_totals(total_cpu, memp, swp, max(busy) if busy else None,
                             max(gpus) if gpus else None, vram)
        for key, title, _c, _t in COLUMNS:
            want = f"{title}\n{tots[key]}" if tots.get(key) else title
            col = self.cols[key]
            if col.get_title() != want:
                col.set_title(want)
        if not self._aligned:
            self._aligned = True
            GLib.idle_add(self._align_headers)

    def _align_headers(self) -> bool:
        """Numeric columns right-align their cells; their header text follows."""
        stack = [self.cv]
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label) and any(
                    a.__gtype__.name == "GtkColumnViewTitle" for a in _ancestors(w)):
                title = w.get_text().split("\n")[0]
                _attach_header_tooltip(w)
                if title != "Name":
                    w.set_xalign(1.0)
                    w.set_justify(Gtk.Justification.RIGHT)
                    w.set_hexpand(True)
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        return GLib.SOURCE_REMOVE

    def _fill_group(self, r: Row, members, keyf, desc, live) -> int:
        """Sum a group's member processes into its row; returns how many there are."""
        kids = [self._proc_row(m) for m in members]
        for k in kids:
            live.add(k.key)
        kids.sort(key=keyf, reverse=desc)
        r.count = len(kids)
        r.cpu = sum(k.cpu or 0 for k in kids)
        r.mem = sum(k.mem for k in kids)
        r.swap = _sum_opt(k.swap for k in kids)
        ios = [k for k in kids if k.io_r is not None]
        r.io_r = sum(k.io_r for k in ios) if ios else None
        r.io_w = sum(k.io_w or 0 for k in ios) if ios else None
        r.gpu = min(100.0, sum(k.gpu or 0 for k in kids)) or None
        r.vram = sum(k.vram or 0 for k in kids)
        r.uid = kids[0].uid if all(k.uid == kids[0].uid for k in kids) else -1
        r.procs = [k.procs[0] for k in kids]
        if [id(k) for k in kids] != [id(k) for k in r.child_rows]:
            r.child_rows = kids
            r.children.splice(0, r.children.get_n_items(), kids)
        return len(kids)

    def _splice(self, rows: list[Row]) -> None:
        selected = self._selected_row()
        expanded = set()
        for i in range(self.tree.get_n_items()):
            tr = self.tree.get_row(i)
            if tr is not None and tr.get_expanded():
                expanded.add(tr.get_item().key)
        self.root_store.splice(0, self.root_store.get_n_items(), rows)
        self._order = [id(r) for r in rows]
        i = 0
        while i < self.tree.get_n_items():
            tr = self.tree.get_row(i)
            if tr is not None and tr.get_item().key in expanded and not tr.get_expanded():
                tr.set_expanded(True)
            i += 1
        if selected is not None:
            for i in range(self.tree.get_n_items()):
                tr = self.tree.get_row(i)
                if tr is not None and tr.get_item().key == selected.key:
                    self.sel.set_selected(i)
                    break
            else:
                self.sel.set_selected(Gtk.INVALID_LIST_POSITION)

    # -- end task -----------------------------------------------------------------------
    def _selected_row(self) -> Row | None:
        tr = self.sel.get_selected_item()
        return tr.get_item() if tr is not None else None

    def _selection_changed(self) -> None:
        r = self._selected_row()
        mine = r is not None and r.uid == os.getuid() and self._refusal(r) is None
        for b in (self.end_btn, self.kill_btn):
            b.set_sensitive(bool(mine))
        why = None
        if r is not None and not mine:
            why = self._refusal(r) or "Owned by another user: stopping it needs root"
        self.end_btn.set_tooltip_text(why or "Ask the selected app or process to quit (SIGTERM)")
        self.kill_btn.set_tooltip_text(why or "Kill the selected app or process (SIGKILL)")

    def _refusal(self, r: Row) -> str | None:
        own = {os.getpid()}
        for p in r.procs:
            why = signal_refusal(p.pid, own)
            if why:
                return why
        return signal_refusal(r.pid, own)

    def _targets(self, r: Row) -> list[tuple[int, int]]:
        return [(p.pid, p.start) for p in r.procs if p.uid == os.getuid()]

    def _end_task(self, sig=signal.SIGTERM) -> None:
        r = self._selected_row()
        if r is None or self._refusal(r):
            return
        targets = self._targets(r)
        if not targets:
            return
        what = r.name if r.kind == "app" else f"{r.name} (PID {r.pid})"
        n = len(targets)
        who = f"its {n} processes" if n > 1 else "it"
        if sig == signal.SIGKILL:
            self.win.confirm(f"Force stop {what}?",
                             f"Kills {who} at once (SIGKILL). It gets no chance to save "
                             "anything.", "Force Stop",
                             lambda: self._signal(what, targets, signal.SIGKILL),
                             destructive=True)
            return
        self.win.confirm(f"Stop {what}?", f"Asks {who} to quit (SIGTERM). Unsaved work may "
                         "be lost.", "Stop", lambda: self._signal(what, targets, signal.SIGTERM),
                         destructive=True)

    def _signal(self, what: str, targets, sig) -> None:
        if self.hub.fake:  # fake PIDs could belong to real processes: never signal them
            self.win.toast(f"Fake hardware: {what} not signalled")
            return
        errors = []
        for pid, start in targets:
            try:
                kill(pid, start, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                errors.append(str(exc))
        if errors:
            self.win.toast(f"Could not end {what}: {errors[0]}")
            return
        if sig == signal.SIGKILL:
            self.win.toast(f"{what} killed")
            return
        self.win.toast(f"Asked {what} to quit")

        def check():
            self._quit_timer = 0
            left = [(p, s) for p, s in targets if alive(p, s)]
            if left:
                self.win.confirm(f"{what} is still running",
                                 "It did not quit within 3 seconds. Force it to stop "
                                 "(SIGKILL)? It gets no chance to save anything.",
                                 "Force quit", lambda: self._signal(what, left, signal.SIGKILL),
                                 destructive=True)
            return GLib.SOURCE_REMOVE

        if self._quit_timer:
            GLib.source_remove(self._quit_timer)
        self._quit_timer = GLib.timeout_add(3000, check)
