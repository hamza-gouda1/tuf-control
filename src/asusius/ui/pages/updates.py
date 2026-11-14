# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Updates: everything on this laptop that needs an update, grouped by where it comes from.

Built on first visit. The page reads the cached report (~/.cache/asusius/updates.json,
written by asusiusd's background check or by this page) and checks again in a child
process when the cache is older than the check interval. Nothing polls while the page is
hidden. Every install goes through the system's own service (PackageKit, snapd, flatpak,
fwupd) behind a confirmation; they ask polkit for a password when they need one.
"""
from __future__ import annotations

import time

from gi.repository import Adw, GLib, Gtk, Pango

from ...updates import actions
from ...updates.model import SOURCE_ORDER, SOURCE_TITLES, Report, UpdateItem, age_text, \
    human_size
from ...updates.progress import TICK_MS, RunState, fmt_duration, fmt_speed
from ...updates.verify import RESULT_TITLES
from ...automation.config import update_config
from ..tasks import run_bg
from ..widgets import label
from .base import Page, button, info_row, pill, prefs_page

esc = GLib.markup_escape_text
MAX_ROWS = 60          # per source; the rest is summarised
STALE_H = 6.0          # check on open when the cache is older than this
FIRMWARE_MIN_BATTERY = 30
SOURCE_ICONS = {"apt": "package-x-generic-symbolic", "snap": "application-x-addon-symbolic",
                "flatpak": "application-x-addon-symbolic",
                "firmware": "application-x-firmware-symbolic",
                "system": "system-reboot-symbolic", "repos": "folder-symbolic"}
BLOCKER_CSS = {"held": "dim", "held-dependency": "dim", "phased": "dim", "needs-new": "warn",
               "conflict": "err", "unknown": "dim"}
BLOCKER_SHORT = {"held": "Held", "held-dependency": "Held dep.", "phased": "Phased",
                 "needs-new": "Needs new", "conflict": "Conflict", "unknown": "Kept back"}
RESULT_CSS = {"installed": "ok", "restart": "ok", "pending": "warn", "failed": "err"}


def _check_and_mark() -> Report:
    from ...updates.check import check_in_child
    from ...updates.scheduler import mark_seen
    rep = check_in_child()
    mark_seen(rep)  # the owner is looking at it: the daemon will not announce these
    return rep


def _refresh_and_check() -> tuple[dict, Report]:
    out = actions.refresh_sources(list(actions.REFRESHABLE))
    return out, _check_and_mark()


def _load() -> tuple[Report | None, dict]:
    from ...updates.check import load_report
    from ...updates.settings import load_settings
    return load_report(), load_settings()


def _write_background_checks(enabled: bool, updater=update_config):
    """Persist the background-check toggle through the config layer's atomic update."""
    return updater(lambda cfg: cfg["updates"].__setitem__("enabled", bool(enabled)))


def item_subtitle(i: UpdateItem) -> str:
    parts = []
    if i.current or i.available:
        parts.append(f"{i.current or '?'} → {i.available or '?'}")
    if i.size:
        parts.append(human_size(i.size))
    if i.extra.get("held"):
        parts.append("held back")
    return " · ".join(parts)


def source_caption(src, now: float | None = None) -> str:
    bits = []
    ts = src.info.get("lists_refreshed_ts")
    if ts:
        bits.append(f"Package lists refreshed {age_text(ts, now)}")
    ts = src.info.get("metadata_ts")
    if ts:
        bits.append(f"Firmware metadata from {age_text(ts, now)}")
    if src.source == "firmware" and src.info.get("updatable") is not None:
        bits.append(f"{src.info['updatable']} updatable devices")
    return " · ".join(bits)


class UpdatesPage(Page):
    key, title = "updates", "Updates"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.report: Report | None = None
        self.busy = False
        self.dynamic: list[Gtk.Widget] = []
        self.last_dialog = None
        self.state: RunState | None = None
        self._tick_id = 0
        self._log_shown = 0
        self._prog_rows: dict[str, dict] = {}
        self.hist_filter: str | None = None
        self.last_run: dict | None = None
        self._history_runs: list[dict] | None = None
        self._history_busy = False
        self._auto_settings: dict | None = None

        # summary header
        self.hero_g = Adw.PreferencesGroup()
        hero = Gtk.Box(spacing=16, margin_bottom=4)
        self.hero_icon = Gtk.Image.new_from_icon_name("software-update-available-symbolic")
        self.hero_icon.set_pixel_size(48)
        self.hero_icon.add_css_class("update-hero-icon")
        hero.append(self.hero_icon)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
        self.hero_title = label("Updates", ("hero-heading",), wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.hero_sub = label("", ("section-caption",), wrap=True,
                              wrap_mode=Pango.WrapMode.WORD_CHAR)
        titles.append(self.hero_title)
        titles.append(self.hero_sub)
        hero.append(titles)
        btns = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner(visible=False)
        btns.append(self.spinner)
        self.refresh_btn = button("Refresh", icon="view-refresh-symbolic",
                                  on_click=self._refresh)
        self.refresh_btn.set_tooltip_text("Refresh package lists and firmware metadata, then "
                                          "check every source again")
        btns.append(self.refresh_btn)
        self.all_btn = button("Update all", "suggested-action", on_click=self._update_all)
        btns.append(self.all_btn)
        self.hero_box = hero
        hero.append(btns)
        self.btns = btns
        self.hero_g.add(hero)

        self.banner = Adw.Banner(revealed=False, button_label="Restart…")
        self.banner.connect("button-clicked", lambda *_: self._restart())

        self.tools_g = Adw.PreferencesGroup()
        sw = Adw.ActionRow(title="Open in Software",
                           subtitle="GNOME Software shows the same updates with its own "
                                    "installer, including offline updates")
        sw.add_prefix(Gtk.Image.new_from_icon_name("org.gnome.Software-symbolic"))
        sw.add_suffix(button("Open", "flat", icon="external-link-symbolic",
                             on_click=self._open_software))
        sw.set_sensitive(actions.software_available() or self.win.fake)
        self.sw_row = sw
        self.tools_g.add(sw)
        self.auto_row = Adw.ActionRow(title="Background checks", subtitle="")
        self.auto_row.set_subtitle_lines(0)
        self.auto_row.add_prefix(Gtk.Image.new_from_icon_name("alarm-symbolic"))
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                      tooltip_text="Enable or pause automatic update checks")
        self.auto_switch.update_property([Gtk.AccessibleProperty.LABEL],
                                         ["Background update checks"])
        self.auto_row.add_suffix(self.auto_switch)
        self.auto_row.set_activatable_widget(self.auto_switch)
        self.auto_switch.connect("notify::active", self._background_checks_changed)
        self.tools_g.add(self.auto_row)

        self.progress_g = Adw.PreferencesGroup(visible=False)
        self.progress_g.add(self._build_progress_panel())
        self.prefs = prefs_page(self.hero_g, self.progress_g, self.tools_g)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(self.prefs)
        self.prefs.set_vexpand(True)
        self.set_child(box)
        win.responsive(self._fit)

    # -- live progress while updates install ------------------------------------------------
    def _build_progress_panel(self) -> Gtk.Revealer:
        self.progress_rev = Gtk.Revealer(reveal_child=False)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6,
                       margin_bottom=10)
        card.add_css_class("card")
        head = Gtk.Box(spacing=8)
        self.prog_spinner = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER,
                                        halign=Gtk.Align.CENTER, margin_start=4, margin_end=4)
        head.append(self.prog_spinner)
        self.prog_title = label("Installing updates", ("section-title",), wrap=True,
                                hexpand=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        head.append(self.prog_title)
        self.prog_time = label("", ("numeric", "dim-label"))
        self.prog_time.set_valign(Gtk.Align.CENTER)
        head.append(self.prog_time)
        self.cancel_btn = button("Cancel", "flat", on_click=self._cancel_run)
        self.cancel_btn.set_valign(Gtk.Align.CENTER)
        self.cancel_btn.set_visible(False)
        head.append(self.cancel_btn)
        card.append(head)
        self.prog_auth = Gtk.Box(spacing=8)
        self.prog_auth.append(Gtk.Image.new_from_icon_name(
            "dialog-password-symbolic"))
        self.prog_auth.append(label("Waiting for your password (the system service is "
                                    "asking)…", ("dim-label",), wrap=True))
        card.append(self.prog_auth)
        self.prog_rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.append(self.prog_rows_box)
        self.prog_log = label("", ("prog-log",), wrap=True, selectable=True,
                              xalign=0.0, wrap_mode=Pango.WrapMode.WORD_CHAR)
        sw = Gtk.ScrolledWindow(height_request=150, vexpand=False)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_child(self.prog_log)
        card.append(sw)
        self.prog_log_sw = sw
        self.progress_rev.set_child(card)
        return self.progress_rev

    def _prog_row(self, method: str) -> None:
        """One live row per system service the run uses."""
        r: dict = {}
        box = Gtk.Box(spacing=10)
        icon = Gtk.Image.new_from_icon_name(
            SOURCE_ICONS.get("apt" if method.startswith("packagekit") else
                             ("flatpak" if method.startswith("flatpak") else method),
                             "emblem-synchronizing-symbolic"))
        icon.set_valign(Gtk.Align.START)
        icon.set_margin_top(2)
        box.append(icon)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        head = Gtk.Box(spacing=8)
        r["name"] = label(actions.METHOD_TITLES.get(method, method), ("body",), wrap=True,
                          hexpand=True)
        r["phase"] = label("Waiting…", ("dim-label",), wrap=True,
                           wrap_mode=Pango.WrapMode.WORD_CHAR)
        head.append(r["name"])
        mid.append(head)
        mid.append(r["phase"])
        r["bar"] = Gtk.ProgressBar(hexpand=True, show_text=False)
        r["bar"].add_css_class("prog-bar")
        mid.append(r["bar"])
        box.append(mid)
        self._prog_rows[method] = r
        self.prog_rows_box.append(box)

    def _start_run(self, methods: list[str]) -> None:
        self.state = RunState()
        self._log_shown = 0
        self._prog_rows = {}
        for child in list(self.prog_rows_box):
            self.prog_rows_box.remove(child)
        for m in methods:
            self._prog_row(m)
        self.prog_title.set_label("Installing updates")
        self.prog_spinner.start()
        self.prog_auth.set_visible(False)
        self.prog_time.set_label("0:00")
        self.progress_g.set_visible(True)
        self.cancel_btn.set_visible(True)
        self.cancel_btn.set_label("Cancel")
        self.cancel_btn.set_sensitive(False)
        self.prog_log.set_label("")
        self.progress_rev.set_reveal_child(True)
        if self._tick_id:
            GLib.source_remove(self._tick_id)
        self._tick_id = GLib.timeout_add(TICK_MS, self._tick)
        self._scroll_to_top()

    def _scroll_to_top(self) -> None:
        from gi.repository import Gtk
        pending = [self.prefs]
        while pending:
            w = pending.pop()
            if isinstance(w, Gtk.ScrolledWindow):
                w.get_vadjustment().set_value(0)
                return
            child = w.get_first_child()
            while child is not None:
                pending.append(child)
                child = child.get_next_sibling()

    def _tick(self) -> bool:
        st = self.state
        if st is None:
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        snap = st.snapshot()
        for m, r in self._prog_rows.items():
            s = dict(snap["sources"]).get(m, {})
            phase = s.get("phase", "Waiting")
            pkg = s.get("package") or ""
            bits = [phase]
            if pkg:
                bits.append(pkg)
            if s.get("speed"):
                bits.append(fmt_speed(s["speed"]))
            if s.get("remaining_s") is not None:
                bits.append(fmt_duration(s["remaining_s"]) + " left")
            if s.get("error"):
                bits.append("— " + str(s["error"])[:120])
            r["phase"].set_label(" · ".join(str(b) for b in bits))
            pct = s.get("percent")
            if s.get("done"):
                r["bar"].set_fraction(1.0 if not s.get("error") else 0.0)
            elif pct is None:
                r["bar"].pulse()
            else:
                r["bar"].set_fraction(pct / 100.0)
        lines = snap["log"]
        if snap["lines"] > self._log_shown:
            self.prog_log.set_label("\n".join(lines[-60:]))
            self._log_shown = snap["lines"]
            va = self.prog_log_sw.get_vadjustment()
            va.set_value(va.get_upper())
        self.prog_time.set_label(fmt_duration(snap["elapsed"]))
        self.prog_auth.set_visible(bool(snap["auth"]))
        self.cancel_btn.set_sensitive(bool(snap["cancellable"])
                                      and not snap["cancel_requested"])
        if snap["cancel_requested"]:
            self.cancel_btn.set_label("Cancelling…")
        if not snap["running"] and not snap["auth"]:
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _end_run(self, summary: str = "") -> None:
        if self._tick_id:
            source_id = self._tick_id
            self._tick()  # one last flush of the log and the bars
            GLib.source_remove(source_id)
            self._tick_id = 0
        self.prog_spinner.stop()
        self.progress_rev.set_reveal_child(False)
        self.progress_g.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.state = None

    def _cancel_run(self) -> None:
        if self.state is not None:
            self.state.cancel.set()
            self.cancel_btn.set_label("Cancelling…")
            self.cancel_btn.set_sensitive(False)

    def _fit(self, level: int) -> None:
        self._level = level
        self.hero_box.set_orientation(Gtk.Orientation.VERTICAL if level >= 3
                                      else Gtk.Orientation.HORIZONTAL)
        self.hero_icon.set_visible(level < 3)

    # -- data ----------------------------------------------------------------------------
    def on_shown(self) -> None:
        super().on_shown()
        if self.state is not None and not self._tick_id:
            self._tick()
            self._tick_id = GLib.timeout_add(TICK_MS, self._tick)

    def on_hidden(self) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def refresh(self) -> None:
        run_bg(_load, done=self._loaded_all, error=self.win.toast_error)
        self._refresh_history()

    def _loaded_all(self, data: tuple[Report | None, dict]) -> None:
        rep, auto_st = data
        self._auto_settings = auto_st
        self._loaded(rep)

    def _refresh_history(self) -> None:
        if self._history_busy:
            return
        from ...updates import history
        self._history_busy = True

        def loaded(runs):
            self._history_busy = False
            self._history_runs = runs
            self.render()

        def failed(exc):
            self._history_busy = False
            self._history_runs = []
            self.render()
            self.win.toast_error(exc)

        run_bg(history.read, done=loaded, error=failed)

    def _loaded(self, rep: Report | None) -> None:
        self.report = rep
        self.render()
        stale = rep is None or time.time() - rep.checked_ts > STALE_H * 3600
        if stale and not self.busy:
            self._check()

    def _check(self) -> None:
        self._set_busy(True, "Checking every source…")
        run_bg(_check_and_mark, done=self._checked, error=self._failed)

    def _checked(self, rep: Report) -> None:
        self._set_busy(False)
        self.report = rep
        self.render()
        self.win.set_updates_count(rep.total)

    def _failed(self, exc) -> None:
        self._set_busy(False)
        self.render()
        self.win.toast_error(exc)

    def _refresh(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Refreshing package lists and firmware metadata…")

        def done(res):
            out, rep = res
            errs = [f"{k}: {v[7:]}" for k, v in out.items() if str(v).startswith("error")]
            self._checked(rep)
            if errs:
                self.win.toast("Could not refresh " + "; ".join(errs)[:140])
            else:
                self.win.toast("Checked for updates" + (" (fake)" if self.win.fake else ""))
        run_bg(_refresh_and_check, done=done, error=self._failed)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        self.refresh_btn.set_sensitive(not busy)
        self.all_btn.set_sensitive(not busy and bool(self.report and self.report.updates()))
        for w in self.dynamic:
            if isinstance(w, Adw.PreferencesGroup):
                w.set_sensitive(not busy)
        if busy and text:
            self.hero_sub.set_label(text)

    # -- rendering ----------------------------------------------------------------------------
    def render(self) -> None:
        for w in self.dynamic:
            self.prefs.remove(w)
        self.dynamic = []
        rep = self.report
        self._render_auto()
        if rep is None:
            self.hero_title.set_label("Not checked yet")
            self.hero_sub.set_label("Checking needs no password and changes nothing.")
            self.hero_icon.set_from_icon_name("software-update-available-symbolic")
            self.all_btn.set_visible(False)
            self.banner.set_revealed(False)
            self._add(self._history_group())
            return
        n = rep.total
        failed_checks = sum(bool(src.error) for src in rep.sources)
        sec = rep.security_count
        if n:
            self.hero_title.set_label(f"{n} update{'s' if n != 1 else ''} available")
            self.hero_icon.set_from_icon_name("software-update-available-symbolic")
            self.hero_icon.remove_css_class("ok")
        elif failed_checks:
            self.hero_title.set_label("Update check incomplete")
            self.hero_icon.set_from_icon_name("dialog-warning-symbolic")
            self.hero_icon.remove_css_class("ok")
        else:
            self.hero_title.set_label("Everything is up to date")
            self.hero_icon.set_from_icon_name("emblem-ok-symbolic")
            self.hero_icon.add_css_class("ok")
        sub = [f"Checked {age_text(rep.checked_ts)}"]
        if failed_checks:
            sub.append(f"{failed_checks} source checks need attention; try Refresh again")
        if sec:
            sub.insert(0, f"{sec} security update{'s' if sec != 1 else ''}")
        parts = ", ".join(f"{SOURCE_TITLES.get(k, k).lower()} {v}"
                          for k, v in rep.counts().items())
        if parts:
            sub.append(parts)
        held = len(rep.waiting())
        if held:  # listed below but not counted as updates: say so up here too
            sub.append(f"{held} held back")
        if not self.busy:
            self.hero_sub.set_label(" · ".join(sub))
        self.all_btn.set_visible(bool(actions.plan(rep.updates())))
        self.all_btn.set_sensitive(not self.busy)
        if rep.reboot_required:
            self.banner.set_title("Restart to finish: " + ", ".join(rep.reboot_reasons[:3]))
        self.banner.set_revealed(rep.reboot_required)

        unavailable = []
        order = {s: i for i, s in enumerate(SOURCE_ORDER)}
        for src in sorted(rep.sources, key=lambda s: order.get(s.source, 99)):
            if not src.available:
                unavailable.append(src)
                continue
            if src.source == "system" and not src.items:
                continue
            self._add(self._source_group(src))
        if unavailable:
            g = Adw.PreferencesGroup(title="Not available",
                                     description="Sources this laptop does not have, or that "
                                                 "are turned off.")
            for src in unavailable:
                reason = ("Repository checks are turned off in the update settings."
                          if src.source == "repos" else
                          "This update service is unavailable or turned off. Check its "
                          "installation and try Refresh again.")
                ico = Gtk.Image.new_from_icon_name(
                    SOURCE_ICONS.get(src.source, "dialog-information-symbolic"))
                if src.reason:
                    ico.set_tooltip_text(src.reason)
                r = Adw.ActionRow(title=esc(src.title), subtitle=reason)
                r.set_subtitle_lines(0)
                r.add_prefix(ico)
                g.add(r)
            self._add(g)
        self._add(self._history_group())

    def _render_auto(self) -> None:
        st = getattr(self, "_auto_settings", None)
        if st is None:
            from ...updates.settings import load_settings
            st = load_settings()
            self._auto_settings = st
        self._syncing_auto = True
        self.auto_switch.set_active(bool(st.get("enabled", True)))
        self._syncing_auto = False
        if not st.get("enabled", True):
            text = "Off. Turn on automatic checks with the switch."
        else:
            text = (f"asusiusd checks at most every {st.get('interval_h', 6):g} hours"
                    + (", not on metered connections" if st.get("respect_metered", True)
                       else "")
                    + (", and notifies once per new update" if st.get("notify", True)
                       else ""))
        self.auto_row.set_subtitle(text)
        self.auto_switch.set_tooltip_text("Settings are stored in ~/.config/asusius/config.json")

    def _background_checks_changed(self, switch, _pspec) -> None:
        if getattr(self, "_syncing_auto", False):
            return
        enabled = switch.get_active()
        switch.set_sensitive(False)

        def done(_cfg):
            switch.set_sensitive(True)
            if isinstance(_cfg, dict) and "updates" in _cfg:
                self._auto_settings = _cfg["updates"]
            self._render_auto()

        def error(exc):
            switch.set_sensitive(True)
            self._render_auto()
            self.win.toast_error(exc)

        run_bg(_write_background_checks, enabled, done=done, error=error)

    def _add(self, g: Gtk.Widget) -> None:
        g.set_sensitive(not self.busy)
        self.prefs.add(g)
        self.dynamic.append(g)

    def _source_group(self, src) -> Adw.PreferencesGroup:
        items = src.items
        n_upd = sum(1 for i in items if i.counts)
        g = Adw.PreferencesGroup(title=esc(src.title), description=esc(source_caption(src)))
        head = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        if n_upd:
            head.append(pill(str(n_upd), "accent"))
        sec = sum(1 for i in items if i.security)
        if sec:
            head.append(pill(f"{sec} security", "err"))
        if actions.plan(items):
            btn = button("Update", "flat", on_click=lambda s=src: self._update_source(s))
            btn.set_tooltip_text(f"Install updates from {src.title}")
            head.append(btn)
        g.set_header_suffix(head)
        if src.error:
            warn_ico = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            warn_ico.set_tooltip_text(src.error)
            row = Adw.ActionRow(
                title="Could not check",
                subtitle="The update service did not return a complete list. Try Refresh again; "
                         "point at the warning icon for technical details.")
            row.set_subtitle_lines(0)
            row.add_prefix(warn_ico)
            g.add(row)
        if not items and not src.error:
            r = info_row("Up to date", "Nothing to install from here.", "emblem-ok-symbolic")
            g.add(r)
        ordered = sorted(items, key=lambda i: (not i.security, i.kind != "kernel",
                                               i.name.lower()))
        for i in ordered[:MAX_ROWS]:
            g.add(self._item_row(i))
        if len(items) > MAX_ROWS:
            g.add(info_row(f"and {len(items) - MAX_ROWS} more",
                           "All of them are included in Update.", "view-more-symbolic"))
        return g

    # -- history --------------------------------------------------------------------------------
    HIST_SOURCES = ("apt", "snap", "flatpak", "firmware", "system")

    def _history_group(self) -> Adw.PreferencesGroup:
        from ...updates import history
        all_runs = self._history_runs or []
        runs = all_runs
        if self.hist_filter:
            runs = [{**r, "items": [i for i in r.get("items") or []
                                   if i.get("source") == self.hist_filter]}
                    for r in runs
                    if any(i.get("source") == self.hist_filter for i in r.get("items") or [])]
        summary_runs = runs
        runs = runs[:15]
        g = Adw.PreferencesGroup(
            title="History",
            description="Every update run this app made, and updates it noticed from "
                        "elsewhere (unattended-upgrades, GNOME Software, a terminal).")
        filt = Gtk.DropDown(model=Gtk.StringList.new(
            ["All sources"] + [SOURCE_TITLES.get(s, s) for s in self.HIST_SOURCES]))
        filt.set_valign(Gtk.Align.CENTER)
        filt.set_focusable(True)   # GtkDropDown ships with focusable unset
        if self.hist_filter in self.HIST_SOURCES:
            filt.set_selected(1 + self.HIST_SOURCES.index(self.hist_filter))
        filt.update_property([Gtk.AccessibleProperty.LABEL], ["Filter history by source"])
        filt.connect("notify::selected", self._hist_filter_changed)
        g.set_header_suffix(filt)
        changes = history.changes([r for r in summary_runs
                                   if float(r.get("end") or r.get("start") or 0)
                                   >= time.time() - 86400])
        if changes:
            names = ", ".join(c.get("name") or "?" for c in changes[:6])
            more = len(changes) - 6
            g.add(info_row(f"Since yesterday: {len(changes)} updated",
                           names + (f" and {more} more" if more > 0 else ""),
                           "emblem-ok-symbolic"))
        if not runs:
            g.add(info_row("Loading history…" if self._history_runs is None else
                           "No runs recorded yet",
                           "Update runs from this app will be listed here, newest first.",
                           "document-open-recent-symbolic"))
            return g
        for r in runs:
            g.add(self._run_row(r))
        g.add(info_row("", "asusius updates history --since 7d shows the same list in a "
                           "terminal.", "dialog-information-symbolic"))
        return g

    def _hist_filter_changed(self, dd, _ps) -> None:
        i = dd.get_selected()
        self.hist_filter = None if i == 0 else self.HIST_SOURCES[i - 1]
        self.render()

    def _run_row(self, r: dict) -> Adw.ExpanderRow:
        from ...updates import history
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("end") or
                                                             r.get("start") or 0))
        trig = {"manual": "run from Asusius", "automatic": "automatic",
                "detected": "installed outside Asusius"}.get(r.get("trigger"),
                                                                 r.get("trigger") or "run")
        dur = f" · {r['duration_s']:g} s" if r.get("duration_s") else ""
        sub = f"{when} · {trig}{dur}"
        row = Adw.ExpanderRow(title=esc(history.run_title(r)), subtitle=esc(sub))
        row.set_subtitle_lines(0)
        row.set_tooltip_text(sub)
        results = r.get("items") or []
        n = {k: sum(1 for x in results if x.get("result") == k)
             for k in ("installed", "restart", "pending", "failed")}
        if n["failed"]:
            row.add_suffix(pill(f"{n['failed']} failed", "err"))
        elif n["pending"]:
            row.add_suffix(pill(f"{n['pending']} pending", "warn"))
        elif results:
            row.add_suffix(pill("OK", "ok"))
        if r.get("reboot"):
            row.add_suffix(pill("Restart", "warn"))
        for it in results:
            ver = f"{it.get('old') or '?'} → {it.get('new') or '?'}"
            text = ver + ((f"\n{it['reason']}" if it.get("reason") else ""))
            ir = Adw.ActionRow(title=esc(str(it.get("name") or "?")), subtitle=esc(text))
            ir.set_subtitle_lines(0)
            res = it.get("result")
            if res in RESULT_TITLES:
                ir.add_suffix(pill(RESULT_TITLES[res], RESULT_CSS.get(res, "dim")))
            row.add_row(ir)
        newest = self.last_run or {}
        if newest and float(r.get("start") or 0) == float(newest.get("start") or -1):
            row.set_expanded(True)
        return row

    def _item_row(self, i: UpdateItem) -> Gtk.Widget:
        bl = i.blocker
        sub = item_subtitle(i)
        row = Adw.ExpanderRow(title=esc(i.name), subtitle=esc(sub))
        row.set_subtitle_lines(0)
        row.set_tooltip_text(sub)
        level = getattr(self, "_level", 0)
        narrow = level >= 2
        compact = level >= 3
        if bl:
            row.add_suffix(pill(BLOCKER_SHORT[bl["blocker"]] if narrow else bl["title"],
                                BLOCKER_CSS.get(bl["blocker"], "dim")))
        if i.security and not compact:
            row.add_suffix(pill("Security", "err"))
        if i.needs_reboot and not compact:
            row.add_suffix(pill("Restart", "warn"))
        if i.kind in ("kernel", "driver", "bios", "ec") and not narrow:
            row.add_suffix(pill({"kernel": "Kernel", "driver": "Driver", "bios": "BIOS",
                                 "ec": "EC"}[i.kind], "gold"))
        details = []
        if compact:
            if i.security:
                details.append("Security update.")
            if i.needs_reboot:
                details.append("Restart needed after installing.")
        if i.description:
            details.append(i.description)
        if i.extra.get("details"):
            details.append(i.extra["details"])
        if i.extra.get("origin"):
            details.append(f"From {i.extra['origin']}")
        issues = [x for x in i.extra.get("issues") or [] if x not in "\n".join(details)]
        if issues:
            details.append("Fixes " + ", ".join(issues))
        if bl:
            details.append(bl["reason"])
        elif i.extra.get("held"):
            details.append("Held back by apt (phased rollout or a dependency change).")
        if i.extra.get("require_ac"):
            details.append("Needs the charger connected.")
        body = Adw.ActionRow(title="Details" if details else "No details",
                             subtitle=esc("\n".join(details)))
        body.set_subtitle_lines(0)
        body.add_css_class("property")
        fixable = bool(bl and bl.get("fixable"))
        if fixable:
            body.add_suffix(button("Install with dependencies", "suggested-action",
                                   on_click=lambda it=i: self._install_deps(it)))
        elif i.waiting:
            pass  # held by the owner or phased: never applied; the reason and the
                  # terminal command are right above
        elif i.apply_method not in ("reboot", "git", "none"):
            body.add_suffix(button("Update", on_click=lambda it=i: self._update_items([it])))
        elif i.apply_method == "reboot":
            body.add_suffix(button("Restart…", on_click=self._restart))
        row.add_row(body)
        if bl and bl.get("command"):
            cmd = Adw.ActionRow(title="In a terminal, this is what would take it:",
                                subtitle=esc(bl["command"]))
            cmd.set_subtitle_lines(0)
            cmd.add_css_class("property")
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
            copy.set_tooltip_text("Copy the command")
            copy.connect("clicked", lambda _b, t=bl["command"]: self._copy_command(t))
            cmd.add_suffix(copy)
            row.add_row(cmd)
        return row

    def _copy_command(self, text: str) -> None:
        try:
            self.get_clipboard().set(text)
        except (TypeError, AttributeError):
            from gi.repository import Gdk, GObject
            self.get_clipboard().set_content(
                Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))
        self.win.toast("Command copied")

    # -- actions ------------------------------------------------------------------------------
    def _open_software(self) -> None:
        try:
            self.win.toast(actions.open_software())
        except Exception as exc:
            self.win.toast_error(exc)

    def _update_all(self) -> None:
        if self.report:
            self._update_items(self.report.updates())

    def _update_source(self, src) -> None:
        self._update_items([i for i in src.items if i.counts])

    def _battery_block(self) -> str | None:
        t = self.win.telemetry()
        status = (t.battery_status or "").lower()
        on_ac = status in ("charging", "full", "not charging") or t.battery_status is None
        if not on_ac:
            return "Connect the charger first: firmware is only installed on AC power."
        if t.battery_percent is not None and t.battery_percent < FIRMWARE_MIN_BATTERY:
            return (f"The battery is at {t.battery_percent:.0f} %. Let it charge above "
                    f"{FIRMWARE_MIN_BATTERY} % before updating firmware.")
        return None

    def _notice(self, heading: str, body: str) -> None:
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present(self.win)
        self.last_dialog = self.win.last_dialog = dlg

    def _update_items(self, items: list[UpdateItem]) -> None:
        planned = actions.plan(items)
        if not planned or self.busy:
            return
        skipped = ""
        if "fwupd" in planned:
            block = self._battery_block()
            if block:  # firmware never starts on battery; the rest may go ahead
                items = [i for i in items if i.apply_method != "fwupd"]
                planned = actions.plan(items)
                if not planned:
                    self._notice("Firmware update not started", block)
                    return
                skipped = f"\n\nFirmware is skipped. {block}"
        body = actions.describe(items) + skipped
        firmware = "fwupd" in planned
        if firmware:
            body += "\n\n" + actions.FIRMWARE_WARNING
        body += ("\n\nEach system service asks for your password if it needs one. "
                 "This can take a while; you can keep using the laptop.")
        n = sum(len(v) for v in planned.values())
        self.win.confirm(f"Install {n} update{'s' if n != 1 else ''}?", body,
                         "Update firmware" if firmware else "Update",
                         lambda: self._apply(items), destructive=firmware)
        self.last_dialog = getattr(self.win, "last_dialog", None)

    def _apply(self, items: list[UpdateItem]) -> None:
        from ...updates import runner
        methods = list(actions.plan(items))
        if not methods:
            return
        self._set_busy(True, "Installing updates…")
        self._start_run(methods)

        def work():
            return runner.apply_run(items, self.state, trigger="manual",
                                    check_fn=_check_and_mark)

        run_bg(work, done=self._run_done, error=self._run_failed)

    def _install_deps(self, item: UpdateItem) -> None:
        from ...updates import runner
        names = actions.deps_names(item)
        body = (actions.describe_deps(item) + "\n\nPackageKit installs these at once; the "
                "system service asks for your password if it needs one.")
        n = len(names)
        self.win.confirm(f"Install {item.name} with {n - 1} new "
                         f"package{'s' if n != 2 else ''}?", body, "Install",
                         lambda: self._apply_deps(item))
        self.last_dialog = getattr(self.win, "last_dialog", None)

    def _apply_deps(self, item: UpdateItem) -> None:
        from ...updates import runner
        self._set_busy(True, "Installing with dependencies…")
        self._start_run(["packagekit-deps"])

        def work():
            return runner.deps_run(item, self.state, trigger="manual",
                                   check_fn=_check_and_mark)

        run_bg(work, done=self._run_done, error=self._run_failed)

    def _run_done(self, res: dict) -> None:
        self._end_run()
        self._set_busy(False)
        self.report = res["report"]
        self.last_run = res["run"]
        self._refresh_history()
        self.render()
        self.win.set_updates_count(self.report.total if self.report else 0)
        self.win.toast(res["summary"] + (" (fake)" if self.win.fake else ""))

    def _run_failed(self, exc) -> None:
        self._end_run()
        self._failed(exc)

    def _restart(self) -> None:
        def go():
            try:
                self.win.toast(actions.restart())
            except Exception as exc:
                self.win.toast_error(exc)
        self.win.confirm("Restart now?", "GNOME asks to close your apps first; save your "
                         "work. Pending kernel, driver and firmware updates finish during "
                         "the restart.", "Restart…", go)
