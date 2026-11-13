# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

import importlib
import os
from collections.abc import Iterator, Mapping

from gi.repository import Adw, Gio, GLib, Gtk

from ..controller import FAKE_ROOT_ENV
from ..core import Capability, Profile, Telemetry
from .live import LiveHub
from .pages.base import Page
from .tasks import run_bg
from .viewmodels import PROFILE_INFO, PROFILE_ORDER, battery_icon, humanize_error, \
    power_state_text
from .widgets import label

PARK_AFTER_S = 30  # a hidden monitor detail page is destroyed after this long
PROFILE_EVERY = 2  # ticks between profile reads (the header chip follows outside changes)

# (key, sidebar title, icon, module, class); pages are imported and built on first visit
MONITOR = [
    ("dashboard", "Dashboard", "view-grid-symbolic", "dashboard", "DashboardPage"),
    ("processes", "Processes", "view-list-bullet-symbolic", "processes", "ProcessesPage"),
    ("services", "Services", "system-run-symbolic", "services", "ServicesPage"),
    ("cpu", "Processor", "computer-symbolic", "cpu", "CpuPage"),
    ("memory", "Memory", "media-flash-symbolic", "memory", "MemoryPage"),
    ("dgpu", "NVIDIA graphics", "applications-graphics-symbolic", "gpu_monitor",
     "NvidiaGpuPage"),
    ("igpu", "Intel graphics", "video-display-symbolic", "gpu_monitor", "IntelGpuPage"),
    ("fanmon", "Fans & temps", "weather-windy-symbolic", "fanmon", "FanMonitorPage"),
    ("storage", "Storage", "drive-harddisk-solidstate-symbolic", "storage", "StoragePage"),
    ("network", "Network", "network-transmit-receive-symbolic", "network", "NetworkPage"),
]
CONTROL = [
    ("performance", "Performance", "power-profile-performance-symbolic", "performance",
     "PerformancePage"),
    ("fans", "Fan curves", "document-edit-symbolic", "fans", "FansPage"),
    ("gpu", "GPU mode", "application-x-firmware-symbolic", "gpu", "GpuPage"),
    ("display", "Display", "preferences-desktop-display-symbolic", "display", "DisplayPage"),
    ("gestures", "Touchpad & gestures", "input-touchpad-symbolic", "gestures", "GesturesPage"),
    ("battery", "Battery", "battery-level-80-symbolic", "battery", "BatteryPage"),
    ("keyboard", "Keyboard", "input-keyboard-symbolic", "keyboard", "KeyboardPage"),
    ("automation", "Automation", "media-playlist-repeat-symbolic", "automation",
     "AutomationPage"),
    ("agent", "Agent access", "channel-secure-symbolic", "agent_access", "AgentAccessPage"),
    ("updates", "Updates", "software-update-available-symbolic", "updates", "UpdatesPage"),
    ("about", "About", "help-about-symbolic", "misc", "AboutPage"),
]
NAV = [("Monitor", MONITOR), ("Control", CONTROL)]
ENTRIES = {e[0]: e for _s, items in NAV for e in items}


class LazyPages(Mapping):
    """win.pages[key] builds the page on first access; iteration lists every key."""

    def __init__(self, win) -> None:
        self.win = win
        self.built: dict[str, Page] = {}

    def __getitem__(self, key: str) -> Page:
        page = self.built.get(key)
        if page is None:
            if key not in ENTRIES:
                raise KeyError(key)
            page = self.win._build(key)
        return page

    def __iter__(self) -> Iterator[str]:
        return iter(ENTRIES)

    def __len__(self) -> int:
        return len(ENTRIES)

    def get_built(self, key: str) -> Page | None:
        return self.built.get(key)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, ctl) -> None:
        super().__init__(application=app, title="Asusius",
                         default_width=1180, default_height=800)
        self.set_size_request(360, 480)
        self.ctl = ctl
        self.caps: dict | None = None
        self.profile: Profile | None = None
        self.fake = bool(os.environ.get(FAKE_ROOT_ENV))
        self._profile_busy = False
        self._profile_reading = False
        self.current: str | None = None
        self.hub = LiveHub(os.environ.get(FAKE_ROOT_ENV) or None)
        self.hub.listeners.append(self._on_sample)
        _load_monitor_css()

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=210, max_sidebar_width=250,
                                             sidebar_width_fraction=0.22)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # sidebar
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.rows: dict[str, Gtk.ListBoxRow] = {}
        for section, items in NAV:
            for i, (key, title, icon, _m, _c) in enumerate(items):
                row = Gtk.ListBoxRow()
                row.key = key
                row.section = section if i == 0 else None
                box = Gtk.Box(spacing=12)
                box.append(Gtk.Image.new_from_icon_name(icon))
                box.append(label(title, hexpand=key == "updates"))
                if key == "updates":  # count of pending updates, from the cached check
                    self.updates_badge = label("", ("nav-badge", "numeric"), xalign=0.5,
                                               visible=False, valign=Gtk.Align.CENTER)
                    box.append(self.updates_badge)
                row.set_child(box)
                self.list.append(row)
                self.rows[key] = row
        self.list.set_header_func(self._row_header)
        self.list.connect("row-selected", self._row_selected)
        self.list.connect("row-activated", lambda *_: self.split.set_show_content(True))
        side_tv = Adw.ToolbarView()
        side_head = Adw.HeaderBar()
        brand = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        brand.add_css_class("sidebar-brand")
        logo = Gtk.Image.new_from_icon_name(app.get_application_id())
        logo.set_pixel_size(22)
        brand.append(logo)
        brand.append(label("Asusius", ("brand-title",)))
        side_head.set_title_widget(brand)
        side_tv.add_top_bar(side_head)
        self.side_scroll = Gtk.ScrolledWindow(child=self.list,
                                              hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_tv.set_content(self.side_scroll)
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=18, margin_end=12,
                       margin_bottom=12, margin_top=6)
        self.mode_label = label("Fake hardware (safe)" if self.fake else "", ("sidebar-mode",))
        foot.append(self.mode_label)
        side_tv.add_bottom_bar(foot)
        self.split.set_sidebar(Adw.NavigationPage(title="Asusius", child=side_tv))

        # content
        # not homogeneous: a hidden page must not set the window's minimum width
        self.stack = Gtk.Stack(css_classes=["content-area"], transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=140, hhomogeneous=False,
                               vhomogeneous=False)
        self.pages = LazyPages(self)
        self.content_tv = Adw.ToolbarView()
        head = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Dashboard")
        head.set_title_widget(self.title)
        head.pack_end(self._profile_chip())
        head.pack_end(self._power_indicator())
        self.content_tv.add_top_bar(head)
        self.content_tv.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Dashboard", child=self.content_tv)
        self.split.set_content(self.content_page)

        # one breakpoint is active at a time (the last that matches): each repeats the
        # collapse of the one before it. Registered widest first.
        self.mid = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        self.add_breakpoint(self.mid)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        self.bp720 = bp
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)
        self.compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        self.compact.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(self.compact)
        self.responders: list = []  # fn(level): 0 wide, 1 <= 900, 2 <= 640, 3 <= 460 sp
        self.connect("notify::current-breakpoint", lambda *_: self._responsive())
        self.tight = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 460sp"))
        self.tight.add_setter(self.split, "collapsed", True)
        self.tight.add_setter(self.pw_box, "visible", False)
        self.tight.add_setter(self.chip_label, "visible", False)
        self.add_breakpoint(self.tight)

        self.list.select_row(self.rows["dashboard"])
        self._updates_sig = None
        self._updates_ticks = 0
        self._poll_updates_badge()
        self._load_caps()  # reads the profile when done (the controller probes once)
        self.connect("close-request", self._on_close)
        self.connect("notify::suspended", lambda *_: self._sync_running())
        self.connect("notify::visible", lambda *_: self._sync_running())
        self.connect("map", lambda *_: self._sync_running())
        self.connect("unmap", lambda *_: self._sync_running())
        self.hub.start()

    # -- pages ------------------------------------------------------------------------
    def _build(self, key: str) -> Page:
        _k, _title, _icon, mod, cls = ENTRIES[key]
        module = importlib.import_module(f".pages.{mod}", __package__)
        page = getattr(module, cls)(self)
        self.pages.built[key] = page
        self.stack.add_named(page, key)
        if key == "performance":
            self.responsive(lambda lv: page.cards.set_orientation(
                Gtk.Orientation.VERTICAL if lv == 3 else Gtk.Orientation.HORIZONTAL))
            if self.profile is not None:
                page.set_profile(self.profile)
        elif key == "processes":
            self.responsive(page.fit_columns)
        elif key in ("battery", "dgpu", "igpu"):
            self.responsive(page.fit_width)
        elif key == "fans":
            self.responsive(page.fit_width)
            if self.profile is not None:
                page.on_profile(self.profile)
        return page

    def level(self) -> int:
        """Width class: 0 wide, 1 up to 900 sp, 2 up to 640 sp, 3 up to 460 sp (phone-like)."""
        bp = self.get_current_breakpoint()
        return 3 if bp is self.tight else 2 if bp is self.compact else (
            1 if bp is not None else 0)

    def responsive(self, fn) -> None:
        """Call fn(level) now and whenever the width class changes. (Breakpoint setters
        added after a breakpoint became active are not applied, so pages built while the
        window is narrow would keep their wide layout.)"""
        self.responders.append(fn)
        fn(self.level())

    def _responsive(self) -> None:
        lv = self.level()
        for fn in list(self.responders):
            fn(lv)

    def _park(self, key: str) -> bool:
        """Destroy a monitor page nobody is looking at; it is rebuilt (quickly) on return.
        The history lives in the shared store, so nothing is lost."""
        page = self.pages.get_built(key)
        if page is not None and key != self.current:
            self.responders = [f for f in self.responders if getattr(
                f, "__self__", None) is not page]
            self.stack.remove(page)
            del self.pages.built[key]
            GLib.timeout_add_seconds(1, self._trim)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _trim() -> bool:
        import gc

        from .lean import trim
        gc.collect()
        trim()
        return GLib.SOURCE_REMOVE

    def _row_header(self, row, _before) -> None:
        if getattr(row, "section", None):
            if row.get_header() is None:
                h = label(row.section.upper(), ("sidebar-section",))
                row.set_header(h)
        else:
            row.set_header(None)

    def _row_selected(self, _lb, row) -> None:
        if row is None:
            return
        old = self.pages.get_built(self.current) if self.current else None
        page = self.pages[row.key]
        if old is not None and old is not page:
            if hasattr(old, "on_hidden"):
                old.on_hidden()
            if getattr(old, "is_monitor_page", False) and old.key != "dashboard":
                GLib.timeout_add_seconds(PARK_AFTER_S, self._park, old.key)
            GLib.timeout_add_seconds(2, self._trim)
        self.current = row.key
        GLib.idle_add(self._reveal_row, row)
        self.stack.set_visible_child_name(row.key)
        self.title.set_title(page.title)
        self.title.set_tooltip_text(page.title)
        self.content_page.set_title(page.title)
        live = getattr(page, "is_monitor_page", False)
        self.hub.set_interval(getattr(page, "rate_s", 1.0) if live else 1.0)
        if self.caps is not None or live:
            page.on_shown()

    def _reveal_row(self, row) -> bool:
        """Keep the selected sidebar row on screen (a short window scrolls the list)."""
        vp = self.side_scroll.get_child()
        if row.get_mapped() and vp is not None and hasattr(vp, "scroll_to"):
            vp.scroll_to(row, None)
        return GLib.SOURCE_REMOVE

    def show_page(self, key: str) -> None:
        row = self.rows.get(key)
        if row is not None:
            self.list.select_row(row)
            self.split.set_show_content(True)

    # -- running state -------------------------------------------------------------------
    def _sync_running(self) -> None:
        """Sample only while the window can be seen."""
        visible = self.get_visible() and self.get_mapped() and not self.get_property("suspended")
        if visible and not self.hub.running:
            self.hub.start()
        elif not visible and self.hub.running:
            self.hub.stop()

    # -- data --------------------------------------------------------------------------------
    def _load_caps(self) -> None:
        def done(caps):
            self.caps = caps
            self._read_profile()
            for page in self.pages.built.values():
                if page.loaded:
                    page.refresh()
            if self.current:
                self.pages[self.current].on_shown()
            if not caps["profile"]["available"]:
                self.chip.set_sensitive(False)
                self.chip_label.set_label("Profile unavailable")

        run_bg(self.ctl.capabilities, done=done, error=self.toast_error)

    def _read_profile(self) -> None:
        if self._profile_reading:
            return
        self._profile_reading = True

        def done(res):
            self._profile_reading = False
            if res is not None and not self._profile_busy:
                self.set_profile_state(res)

        def read():
            try:
                return self.ctl.read(Capability.PROFILE)[0]
            except Exception:  # profile is optional for the header
                return None

        run_bg(read, done=done, error=lambda e: setattr(self, "_profile_reading", False))

    def telemetry(self) -> Telemetry:
        """The old Telemetry model, built from the shared sampler (pages still use it)."""
        st, info = self.hub.store, self.hub.info
        b = info.get("battery") or {}
        dg = info.get("dgpu") or {}
        awake = dg.get("state") == "awake"
        fans = {}
        for key, lab in (info.get("sensors") or {}).get("fans", []):
            v = st.last(f"fan.{key}")
            if v is not None:
                fans[lab] = int(v)
        return Telemetry(
            cpu_temp_c=st.last("temp.cpu"), fan_rpm=fans,
            gpu_temp_c=st.last("dgpu.temp") if awake else None,
            gpu_power_w=st.last("dgpu.power") if awake else None,
            gpu_util_pct=st.last("dgpu.util") if awake else None,
            battery_power_w=st.last("bat.power"), battery_status=b.get("status"),
            battery_percent=b.get("percent"), profile=self.profile,
            cpu_power_w=st.last("power.cpu"), gpu_runtime=dg.get("runtime"))

    # -- updates badge ---------------------------------------------------------------------
    def set_updates_count(self, n: int | None) -> None:
        self.updates_badge.set_label(str(n) if n else "")
        self.updates_badge.set_visible(bool(n))
        self.updates_badge.set_tooltip_text(f"{n} updates available" if n else None)

    def _poll_updates_badge(self) -> None:
        """stat() the update cache (written by asusiusd or the Updates page); read it
        only when it changed."""
        from ..updates.check import cache_path
        try:
            st = cache_path().stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig == self._updates_sig:
            return
        self._updates_sig = sig

        def read():
            from ..updates.check import load_report
            rep = load_report()
            return rep.total if rep else None
        run_bg(read, done=self.set_updates_count, error=lambda e: None)

    def _on_sample(self, hub) -> None:
        self._updates_ticks += 1
        if self._updates_ticks % 30 == 0:
            self._poll_updates_badge()
        if hub.ticks % PROFILE_EVERY == 0 and self.caps is not None:
            self._read_profile()
        t = self.telemetry()
        self.on_telemetry(t)
        if self.current:
            page = self.pages.get_built(self.current)
            if page is not None and getattr(page, "is_monitor_page", False) and page.loaded:
                page.on_sample(hub)

    def on_telemetry(self, t) -> None:
        self.pw_icon.set_from_icon_name(battery_icon(t.battery_percent, t.battery_status))
        self.pw_label.set_label(power_state_text(t.battery_status, t.battery_percent))
        self.pw_box.set_tooltip_text(t.battery_status or "On AC power")
        for page in self.pages.built.values():
            page.on_telemetry(t)

    # -- header widgets ---------------------------------------------------------------------
    def _profile_chip(self) -> Gtk.Widget:
        act = Gio.SimpleAction.new_stateful("profile", GLib.VariantType.new("s"),
                                            GLib.Variant("s", ""))
        act.connect("change-state", self._profile_action)
        self.add_action(act)
        self.profile_action = act
        menu = Gio.Menu()
        for p in PROFILE_ORDER:
            menu.append(PROFILE_INFO[p][0], f"win.profile::{p.value}")
        box = Gtk.Box(spacing=8)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("chip-dot")
        box.append(dot)
        self.chip_icon = Gtk.Image.new_from_icon_name("power-profile-balanced-symbolic")
        box.append(self.chip_icon)
        self.chip_label = label("Profile")
        box.append(self.chip_label)
        btn = Gtk.MenuButton(child=box, menu_model=menu, tooltip_text="Power profile",
                             always_show_arrow=False)
        btn.add_css_class("profile-chip")
        btn.add_css_class("flat")
        self.chip = btn
        return btn

    def _power_indicator(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.add_css_class("power-indicator")
        self.pw_icon = Gtk.Image.new_from_icon_name("battery-level-50-symbolic")
        self.pw_label = label("", ("numeric",))
        box.append(self.pw_icon)
        box.append(self.pw_label)
        self.pw_box = box
        return box

    # -- profile -------------------------------------------------------------------------------
    def set_profile_state(self, prof: Profile, confirmed: bool = True) -> None:
        if prof is self.profile:
            return
        self.profile = prof
        self.profile_action.set_state(GLib.Variant("s", prof.value))
        title, _sub, icon = PROFILE_INFO[prof]
        self.chip_label.set_label(title)
        self.chip_icon.set_from_icon_name(icon)
        perf = self.pages.get_built("performance")
        if perf is not None:
            perf.set_profile(prof)
        fans = self.pages.get_built("fans")
        if confirmed and fans is not None:  # fan curves are read per active profile
            fans.on_profile(prof)

    def _profile_action(self, _act, value) -> None:
        self.request_profile(Profile(value.get_string()))

    def request_profile(self, prof: Profile) -> None:
        if prof is self.profile or self._profile_busy:
            return
        old = self.profile
        self._profile_busy = True
        self.set_profile_state(prof, confirmed=False)

        def done(_res):
            self._profile_busy = False
            fans = self.pages.get_built("fans")
            if fans is not None:
                fans.on_profile(prof)
            self.toast(f"{PROFILE_INFO[prof][0]} profile on")

        def fail(exc):
            self._profile_busy = False
            if old is not None:
                self.set_profile_state(old)
            self.toast_error(exc)

        run_bg(self.ctl.set_profile, prof, done=done, error=fail)

    # -- feedback ---------------------------------------------------------------------------------
    def _toast_text(self, text: str, narrow_chars: int = 34, wide_chars: int = 90) -> str:
        """Toasts never wrap: a title wider than the window makes the overlay stick out,
        so long ones are cut (the full result is always in the page). An unallocated
        window counts as narrow."""
        w = self.get_width()
        limit = wide_chars if w >= 520 else narrow_chars
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

    def toast(self, text: str, undo=None) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(self._toast_text(text)), timeout=3)
        if undo:
            t.set_button_label("Undo")
            t.connect("button-clicked", lambda *_: undo())
        self.toasts.add_toast(t)

    def toast_error(self, exc: BaseException) -> None:
        if os.environ.get("ASUSIUS_DEBUG"):
            import traceback
            traceback.print_exception(exc)
        t = Adw.Toast(title=GLib.markup_escape_text(
            self._toast_text(humanize_error(exc))), timeout=6)
        self.toasts.add_toast(t)

    def run_write(self, fn, ok_text: str, after=None, on_error=None) -> None:
        def done(res):
            self.toast(ok_text + (" (fake hardware)" if self.fake else ""))
            if after:
                after(res)

        def fail(exc):
            self.toast_error(exc)
            if on_error:
                on_error()

        run_bg(fn, done=done, error=fail)

    def confirm(self, heading: str, body: str, confirm_label: str, on_yes,
                destructive: bool = False) -> None:
        if self.fake:
            body += "\n\nFake hardware: only the test tree changes."
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", confirm_label)
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                    else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: on_yes() if r == "ok" else None)
        dlg.present(self)
        self.last_dialog = dlg

    def _on_close(self, *_):
        self.hub.close()
        return False


_css_loaded = False


def _load_monitor_css() -> None:
    """monitor.css: styles for the monitor pages, kept apart from the shared style.css."""
    global _css_loaded
    if _css_loaded:
        return
    _css_loaded = True
    from pathlib import Path

    from gi.repository import Gdk

    from .theme import mono_family
    css = (Path(__file__).with_name("monitor.css")).read_text().replace("@MONO@",
                                                                        mono_family())
    css += "\n" + Path(__file__).with_name("system.css").read_text()  # updates, services
    prov = Gtk.CssProvider()
    prov.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
