from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..controller import FAKE_ROOT_ENV
from ..core import Capability, Profile
from .pages.base import Page
from .pages.battery import BatteryPage
from .pages.dashboard import DashboardPage
from .pages.display import DisplayPage
from .pages.fans import FansPage
from .pages.gpu import GpuPage
from .pages.keyboard import KeyboardPage
from .pages.misc import AboutPage, AutomationPage
from .pages.performance import PerformancePage
from .tasks import run_bg
from .viewmodels import PAGES, PROFILE_INFO, PROFILE_ORDER, battery_icon, humanize_error, \
    power_state_text
from .widgets import label

POLL_MS = 1000
PAGE_CLASSES = [DashboardPage, PerformancePage, FansPage, GpuPage, DisplayPage, BatteryPage,
                KeyboardPage, AutomationPage, AboutPage]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, ctl) -> None:
        super().__init__(application=app, title="TUF Control",
                         default_width=1180, default_height=800)
        self.set_size_request(360, 480)
        self.ctl = ctl
        self.caps: dict | None = None
        self.profile: Profile | None = None
        self.fake = bool(os.environ.get(FAKE_ROOT_ENV))
        self._polling = False
        self._profile_busy = False

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=210, max_sidebar_width=250,
                                             sidebar_width_fraction=0.22)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # sidebar
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        for key, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(label(title))
            row.set_child(box)
            self.list.append(row)
        self.list.connect("row-selected", self._row_selected)
        self.list.connect("row-activated", lambda *_: self.split.set_show_content(True))
        side_tv = Adw.ToolbarView()
        side_head = Adw.HeaderBar()
        brand = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        brand.add_css_class("sidebar-brand")
        logo = Gtk.Image.new_from_icon_name(app.get_application_id())
        logo.set_pixel_size(22)
        brand.append(logo)
        brand.append(label("TUF Control", ("brand-title",)))
        side_head.set_title_widget(brand)
        side_tv.add_top_bar(side_head)
        side_tv.set_content(Gtk.ScrolledWindow(child=self.list,
                                               hscrollbar_policy=Gtk.PolicyType.NEVER))
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=18, margin_end=12,
                       margin_bottom=12, margin_top=6)
        self.mode_label = label("Fake hardware (safe)" if self.fake else "", ("sidebar-mode",))
        foot.append(self.mode_label)
        side_tv.add_bottom_bar(foot)
        self.split.set_sidebar(Adw.NavigationPage(title="TUF Control", child=side_tv))

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=140)
        self.pages: dict[str, Page] = {}
        for cls in PAGE_CLASSES:
            page = cls(self)
            self.pages[cls.key] = page
            self.stack.add_named(page, cls.key)
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

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)
        tight = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 460sp"))
        tight.add_setter(self.split, "collapsed", True)
        tight.add_setter(self.pw_label, "visible", False)
        tight.add_setter(self.chip_label, "visible", False)
        tight.add_setter(self.pages["performance"].cards, "orientation",
                         Gtk.Orientation.VERTICAL)
        self.add_breakpoint(tight)

        self.list.select_row(self.list.get_row_at_index(0))
        self._load_caps()
        self._poll()
        self._timer = GLib.timeout_add(POLL_MS, self._poll)
        self.connect("close-request", self._on_close)

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

    # -- navigation --------------------------------------------------------------------------
    def _row_selected(self, _lb, row) -> None:
        if row is None:
            return
        page = self.pages[row.key]
        self.stack.set_visible_child_name(row.key)
        self.title.set_title(page.title)
        self.content_page.set_title(page.title)
        if self.caps is not None:
            page.on_shown()

    def show_page(self, key: str) -> None:
        for i, (k, _t, _i) in enumerate(PAGES):
            if k == key:
                self.list.select_row(self.list.get_row_at_index(i))
                self.split.set_show_content(True)

    # -- data --------------------------------------------------------------------------------
    def _load_caps(self) -> None:
        def done(caps):
            self.caps = caps
            for page in self.pages.values():
                if page.loaded:
                    page.refresh()
            self.pages[self.stack.get_visible_child_name()].on_shown()
            if not caps["profile"]["available"]:
                self.chip.set_sensitive(False)
                self.chip_label.set_label("Profile unavailable")

        run_bg(self.ctl.capabilities, done=done, error=self.toast_error)

    def _poll(self) -> bool:
        if self._polling:
            return GLib.SOURCE_CONTINUE
        self._polling = True

        def done(res):
            self._polling = False
            t = res[0]
            self.on_telemetry(t)

        def fail(exc):
            self._polling = False

        run_bg(self.ctl.read, Capability.TELEMETRY, done=done, error=fail)
        return GLib.SOURCE_CONTINUE

    def on_telemetry(self, t) -> None:
        if t.profile is not None and not self._profile_busy:
            self.set_profile_state(t.profile)
        self.pw_icon.set_from_icon_name(battery_icon(t.battery_percent, t.battery_status))
        self.pw_label.set_label(power_state_text(t.battery_status, t.battery_percent))
        self.pw_box.set_tooltip_text(t.battery_status or "On AC power")
        for page in self.pages.values():
            page.on_telemetry(t)

    # -- profile -------------------------------------------------------------------------------
    def set_profile_state(self, prof: Profile, confirmed: bool = True) -> None:
        if prof is self.profile:
            return
        self.profile = prof
        self.profile_action.set_state(GLib.Variant("s", prof.value))
        title, _sub, icon = PROFILE_INFO[prof]
        self.chip_label.set_label(title)
        self.chip_icon.set_from_icon_name(icon)
        self.pages["performance"].set_profile(prof)
        if confirmed:  # fan curves are read per active profile: wait for the write
            self.pages["fans"].on_profile(prof)

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
            self.pages["fans"].on_profile(prof)
            self.toast(f"{PROFILE_INFO[prof][0]} profile on")

        def fail(exc):
            self._profile_busy = False
            if old is not None:
                self.set_profile_state(old)
            self.toast_error(exc)

        run_bg(self.ctl.set_profile, prof, done=done, error=fail)

    # -- feedback ---------------------------------------------------------------------------------
    def toast(self, text: str, undo=None) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=3)
        if undo:
            t.set_button_label("Undo")
            t.connect("button-clicked", lambda *_: undo())
        self.toasts.add_toast(t)

    def toast_error(self, exc: BaseException) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(humanize_error(exc)), timeout=6)
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
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        return False
