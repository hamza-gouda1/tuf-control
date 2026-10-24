"""Updates: everything on this laptop that needs an update, grouped by where it comes from.

Built on first visit. The page reads the cached report (~/.cache/tuf-control/updates.json,
written by tuf-controld's background check or by this page) and checks again in a child
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


def _check_and_mark() -> Report:
    from ...updates.check import check_in_child
    from ...updates.scheduler import mark_seen
    rep = check_in_child()
    mark_seen(rep)  # the owner is looking at it: the daemon will not announce these
    return rep


def _refresh_and_check() -> tuple[dict, Report]:
    out = actions.refresh_sources(list(actions.REFRESHABLE))
    return out, _check_and_mark()


def _load() -> Report | None:
    from ...updates.check import is_checking, load_report
    rep = load_report()
    if rep is not None:
        rep.checking = is_checking()  # type: ignore[attr-defined]
    return rep


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
        self.tools_g.add(self.auto_row)

        self.prefs = prefs_page(self.hero_g, self.tools_g)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(self.prefs)
        self.prefs.set_vexpand(True)
        self.set_child(box)
        win.responsive(self._fit)

    def _fit(self, level: int) -> None:
        self.hero_box.set_orientation(Gtk.Orientation.VERTICAL if level >= 3
                                      else Gtk.Orientation.HORIZONTAL)
        self.hero_icon.set_visible(level < 3)

    # -- data ----------------------------------------------------------------------------
    def refresh(self) -> None:
        run_bg(_load, done=self._loaded, error=self.win.toast_error)

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
            return
        n = rep.total
        sec = rep.security_count
        if n:
            self.hero_title.set_label(f"{n} update{'s' if n != 1 else ''} available")
            self.hero_icon.set_from_icon_name("software-update-available-symbolic")
            self.hero_icon.remove_css_class("ok")
        else:
            self.hero_title.set_label("Everything is up to date")
            self.hero_icon.set_from_icon_name("emblem-ok-symbolic")
            self.hero_icon.add_css_class("ok")
        sub = [f"Checked {age_text(rep.checked_ts)}"]
        if sec:
            sub.insert(0, f"{sec} security update{'s' if sec != 1 else ''}")
        parts = ", ".join(f"{SOURCE_TITLES.get(k, k).lower()} {v}"
                          for k, v in rep.counts().items())
        if parts:
            sub.append(parts)
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
                r = info_row(esc(src.title), esc(src.reason[:1].upper() + src.reason[1:]),
                             SOURCE_ICONS.get(src.source, "dialog-information-symbolic"))
                g.add(r)
            self._add(g)
        # keep the tools group last
        self.prefs.remove(self.tools_g)
        self.prefs.add(self.tools_g)

    def _render_auto(self) -> None:
        from ...updates.settings import load_settings
        st = load_settings()
        if not st.get("enabled", True):
            text = "Off. Turn on with \"updates\": {\"enabled\": true} in the config."
        else:
            text = (f"tuf-controld checks at most every {st.get('interval_h', 6):g} hours"
                    + (", not on metered connections" if st.get("respect_metered", True)
                       else "")
                    + (", and notifies once per new update" if st.get("notify", True)
                       else ""))
        self.auto_row.set_subtitle(text)

    def _add(self, g: Gtk.Widget) -> None:
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
            head.append(button("Update", "flat", on_click=lambda s=src: self._update_source(s)))
        g.set_header_suffix(head)
        if src.error:
            g.add(info_row("Could not check", esc(src.error), "dialog-warning-symbolic"))
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

    def _item_row(self, i: UpdateItem) -> Gtk.Widget:
        row = Adw.ExpanderRow(title=esc(i.name), subtitle=esc(item_subtitle(i)))
        row.set_subtitle_lines(1)
        if i.security:
            row.add_suffix(pill("Security", "err"))
        if i.needs_reboot:
            row.add_suffix(pill("Restart", "warn"))
        if i.kind in ("kernel", "driver", "bios", "ec"):
            row.add_suffix(pill({"kernel": "Kernel", "driver": "Driver", "bios": "BIOS",
                                 "ec": "EC"}[i.kind], "gold"))
        details = []
        if i.description:
            details.append(i.description)
        if i.extra.get("details"):
            details.append(i.extra["details"])
        if i.extra.get("origin"):
            details.append(f"From {i.extra['origin']}")
        issues = [x for x in i.extra.get("issues") or [] if x not in "\n".join(details)]
        if issues:
            details.append("Fixes " + ", ".join(issues))
        if i.extra.get("held"):
            details.append("Held back by apt (phased rollout or a dependency change); "
                           "Update installs it anyway through PackageKit.")
        if i.extra.get("require_ac"):
            details.append("Needs the charger connected.")
        body = Adw.ActionRow(title="Details" if details else "No details",
                             subtitle=esc("\n".join(details)))
        body.set_subtitle_lines(0)
        body.add_css_class("property")
        if i.apply_method not in ("reboot", "git", "none"):
            body.add_suffix(button("Update", on_click=lambda it=i: self._update_items([it])))
        elif i.apply_method == "reboot":
            body.add_suffix(button("Restart…", on_click=self._restart))
        row.add_row(body)
        return row

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
        self._set_busy(True, "Installing updates…")

        def work():
            out = actions.apply(items)
            return out, _check_and_mark()

        def done(res):
            out, rep = res
            self._checked(rep)
            errs = [v for v in out.values() if str(v).startswith("error")]
            self.win.toast(errs[0][7:][:160] if errs else "Updates installed" +
                           (" (fake)" if self.win.fake else ""))
        run_bg(work, done=done, error=self._failed)

    def _restart(self) -> None:
        def go():
            try:
                self.win.toast(actions.restart())
            except Exception as exc:
                self.win.toast_error(exc)
        self.win.confirm("Restart now?", "GNOME asks to close your apps first; save your "
                         "work. Pending kernel, driver and firmware updates finish during "
                         "the restart.", "Restart…", go)
