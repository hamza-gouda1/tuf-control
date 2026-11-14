# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from dataclasses import dataclass

from gi.repository import Adw, GLib, Gio, Gtk

from ... import touchpad
from ..tasks import run_bg
from ..widgets import label
from .base import Page, a11y_name, button, info_row, prefs_page, value_row

ACTION_LABELS = {
    "none": "None",
    "overview": "Overview",
    "app-grid": "App grid",
    "show-desktop": "Show desktop",
    "switch-windows": "Switch window",
    "prev-workspace": "Previous workspace",
    "next-workspace": "Next workspace",
    "launch-app": "Launch an app",
    "prev-window": "Previous app",
    "next-window": "Next app",
    "volume-up": "Volume up",
    "volume-down": "Volume down",
    "play-pause": "Play or pause",
    "media-next": "Next song",
    "media-prev": "Previous song",
}

CLICK_LABELS = {"default": "Default", "none": "None", "areas": "Areas", "fingers": "Fingers"}

# (key, title, subtitle): a subtitle only where the gesture behaves differently
# from the rest, the wording follows the schema's own description
GESTURE_ROWS = (
    ("gesture-swipe3-down", "Three-finger swipe down",
     "Only fires on the desktop: while the overview is up the shell closes it itself"),
    ("gesture-swipe3-left", "Three-finger swipe left", None),
    ("gesture-swipe3-right", "Three-finger swipe right", None),
    ("gesture-hold4", "Four-finger tap",
     "Tap the touchpad with four fingers to run the action"),
    ("gesture-swipe4-up", "Four-finger swipe up", None),
    ("gesture-swipe4-down", "Four-finger swipe down", None),
    ("gesture-swipe4-left", "Four-finger swipe left", None),
    ("gesture-swipe4-right", "Four-finger swipe right", None),
    ("gesture-pinch-in", "Pinch in", "Fingers drawn together, the pinch shrinking"),
    ("gesture-pinch-out", "Pinch out", "Fingers spread apart, the pinch growing"),
)

SPEED_DEBOUNCE_MS = 700  # the slider settles, then one write (as the charge limit does)


@dataclass(frozen=True)
class SessionBanner:
    """Presentation state computed from ``touchpad.SessionInfo``."""

    ext_label: str
    show_enable: bool
    pad_applicable: bool
    gest_interactive: bool
    show_qs_group: bool
    err_visible: bool
    err_title: str = ""
    err_subtitle: str = ""


def classify_session_banner(info: touchpad.SessionInfo, schema_available: bool) -> SessionBanner:
    """Map desktop session and extension status to banner messaging and row states."""
    if info.is_other_desktop:
        return SessionBanner(
            ext_label="GNOME only",
            show_enable=False,
            pad_applicable=False,
            gest_interactive=False,
            show_qs_group=False,
            err_visible=True,
            err_title=f"GNOME Shell features unavailable on {info.desktop}",
            err_subtitle=(
                "Quick Settings and touchpad extension gestures require GNOME Shell on "
                "Wayland. Power profiles, fan curves, and battery limits still work "
                "normally on this desktop."
            ),
        )

    if not info.installed and not schema_available:
        return SessionBanner(
            ext_label="Not installed",
            show_enable=False,
            pad_applicable=True,
            gest_interactive=False,
            show_qs_group=False,
            err_visible=True,
            err_title="Gestures unavailable",
            err_subtitle=(
                "The asusius-quick shell extension is not installed. Run install.sh "
                "or install your distro package, then log out and back in if needed."
            ),
        )

    if info.installed and (not info.schema_compiled or not schema_available):
        path = info.extension_path or "the extension folder"
        return SessionBanner(
            ext_label="Schema missing",
            show_enable=False,
            pad_applicable=True,
            gest_interactive=False,
            show_qs_group=False,
            err_visible=True,
            err_title="Extension schema not compiled",
            err_subtitle=(
                f"Found extension files in {path}, but compiled GSettings schemas "
                "are missing. Compile them with glib-compile-schemas or reinstall."
            ),
        )

    x11_banner = (
        info.session_type == "x11",
        "Touchpad gestures require Wayland",
        "The Quick Settings switcher works on X11, while 3- and 4-finger touchpad "
        "gestures only run in a GNOME Wayland session.",
    )

    state = info.shell_state
    if state == "needs_relogin":
        return SessionBanner(
            ext_label="Relogin required",
            show_enable=False,
            pad_applicable=True,
            gest_interactive=False,
            show_qs_group=False,
            err_visible=True,
            err_title="Restart GNOME Shell session",
            err_subtitle=(
                "Extension files are installed, but GNOME Shell has not discovered "
                "them yet. Log out and back in, then open Asusius again."
            ),
        )
    if state == "inactive":
        return SessionBanner(
            ext_label="Disabled",
            show_enable=True,
            pad_applicable=True,
            gest_interactive=True,
            show_qs_group=True,
            err_visible=True,
            err_title="Shell extension is disabled",
            err_subtitle=(
                "Click Enable above to activate the asusius-quick GNOME Shell "
                "extension without leaving Asusius."
            ),
        )
    if state == "error":
        return SessionBanner(
            ext_label="Error",
            show_enable=False,
            pad_applicable=True,
            gest_interactive=False,
            show_qs_group=False,
            err_visible=True,
            err_title="Shell extension failed to load",
            err_subtitle=(
                info.detail
                or "Check journalctl for gnome-shell or run gnome-extensions info "
                   f"{touchpad.EXTENSION_UUID}."
            ),
        )
    if state == "out_of_date":
        return SessionBanner(
            ext_label="Out of date",
            show_enable=False,
            pad_applicable=True,
            gest_interactive=False,
            show_qs_group=False,
            err_visible=True,
            err_title="Shell version mismatch",
            err_subtitle=(
                "The installed extension metadata does not match this GNOME Shell "
                "version. Update Asusius or reinstall the extension."
            ),
        )

    if state == "active":
        label_text = "Active (X11)" if x11_banner[0] else "Active"
    else:
        label_text = "Installed (X11)" if x11_banner[0] else "Installed"
    return SessionBanner(
        ext_label=label_text,
        show_enable=False,
        pad_applicable=True,
        gest_interactive=True,
        show_qs_group=True,
        err_visible=x11_banner[0],
        err_title=x11_banner[1] if x11_banner[0] else "",
        err_subtitle=x11_banner[2] if x11_banner[0] else "",
    )


def _discover_apps(selected_id: str = "") -> list[tuple[str, str]]:
    """Enumerate launchable desktop apps on a worker thread, keeping any bound id."""
    apps: list[tuple[str, str]] = [("", "None")]
    seen: set[str] = set()
    for info in Gio.AppInfo.get_all():
        if not isinstance(info, Gio.DesktopAppInfo):
            continue
        app_id = info.get_id() or ""
        if not app_id or app_id in seen:
            continue
        if not info.should_show() and app_id != selected_id:
            continue
        seen.add(app_id)
        apps.append((app_id, info.get_name() or app_id))
    apps.sort(key=lambda a: (a[0] == "", a[1].casefold()))
    return apps


class GesturesPage(Page):
    key, title = "gestures", "Touchpad & gestures"

    def __init__(self, win) -> None:
        super().__init__(win)
        try:
            self.pad = touchpad.TouchpadSettings()
        except touchpad.SchemaMissing:  # the desktop schemas are part of every GNOME
            self.pad = None
        self.gest = touchpad.GestureSettings()
        self._quiet = False
        self._pending = None
        self._destroyed = False
        self._apps_seq = 0
        self._session_seq = 0
        self._session_info: touchpad.SessionInfo | None = None
        self._apps: list[tuple[str, str]] = [("", "None")]  # (desktop id, shown name)
        self._apps_loaded = False
        self.connect("destroy", self._on_destroy)

        # -- touchpad ------------------------------------------------------------------------
        self.panel = Adw.PreferencesGroup(title="Touchpad")
        self.on_row = Adw.SwitchRow(title="Touchpad")
        self.on_row.connect("notify::active", self._send_events)
        self.mouse_row = Adw.SwitchRow(
            title="Turn off when a mouse is connected",
            subtitle="The built-in touchpad sleeps while an external mouse is plugged in")
        self.mouse_row.connect("notify::active", self._send_events)
        self.tap_row = Adw.SwitchRow(title="Tap to click",
                                     subtitle="Tapping the touchpad acts as a button press")
        self.tap_row.connect("notify::active", self._tap)
        self.natural_row = Adw.SwitchRow(
            title="Natural scrolling",
            subtitle="Content moves the way a finger does, like on a phone")
        self.natural_row.connect("notify::active", self._natural)
        self.speed_adj = Gtk.Adjustment(value=0, lower=-1, upper=1, step_increment=0.05,
                                        page_increment=0.25)
        self.speed_scale = Gtk.Scale(adjustment=self.speed_adj, draw_value=False, hexpand=True)
        a11y_name(self.speed_scale, "Touchpad speed")
        self.speed_adj.connect("value-changed", self._speed)
        self.speed_row = Adw.ActionRow(title="Speed",
                                       subtitle="How far the cursor moves for a finger")
        self.speed_val = label("–", ("numeric", "dim-label"), xalign=1.0)
        self.speed_val.set_valign(Gtk.Align.CENTER)
        speed_box = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
        speed_box.append(self.speed_val)
        speed_box.append(self.speed_scale)
        self.speed_row.add_suffix(speed_box)
        self.click_row = Adw.ComboRow(title="Click method",
                                      subtitle="What a press in the button area does")
        self.click_row.connect("notify::selected", self._click)
        self.click_row.connect("notify::selected-item", _combo_tip)
        _combo_tip(self.click_row)
        self.pad_rows = (self.on_row, self.mouse_row, self.tap_row, self.natural_row,
                         self.speed_row, self.click_row)
        for r in self.pad_rows:
            self.panel.add(r)
        self.pad_err = info_row(
            "Touchpad settings unavailable",
            "The desktop's touchpad schema could not be read, so the controls are off. "
            "Restart Asusius to try again.")
        self.pad_err.set_visible(False)
        self.panel.add(self.pad_err)

        # -- gestures ------------------------------------------------------------------------
        self.gestures = Adw.PreferencesGroup(
            title="Gestures",
            description="Bind swipes, pinches, and a four-finger tap. Configured "
                        "swipes may take ownership of touchpad input.")
        self.ext_row, self.ext_val = value_row("Shell extension",
                                               "Quick-settings tile that runs the gestures")
        self.enable_btn = button("Enable", "suggested-action", on_click=self._enable_extension)
        self.enable_btn.set_visible(False)
        self.ext_row.add_suffix(self.enable_btn)
        self.enabled_row = Adw.SwitchRow(
            title="Gestures enabled",
            subtitle="Run the selected action after each configured gesture")
        self.enabled_row.connect("notify::active", self._gest_enabled)
        self.gestures.add(self.ext_row)
        self.gestures.add(self.enabled_row)
        self.gesture_rows: dict[str, Adw.ComboRow] = {}
        self._binding_choices: dict[str, tuple[str, ...]] = {}
        self._ensure_gesture_rows(reorder=False)
        self.app_row = Adw.ComboRow(title="App to launch",
                                    subtitle="Used by gestures set to “Launch an app”")
        self.app_row.set_model(Gtk.StringList.new(["None"]))
        self.app_row.connect("notify::selected", self._launch_app)
        self.app_row.connect("notify::selected-item", _combo_tip)
        _combo_tip(self.app_row)
        self.gestures.add(self.app_row)
        self.gest_err = info_row("Gestures unavailable", "")
        self.gest_err.set_visible(False)
        self.gestures.add(self.gest_err)

        self.qs_group = Adw.PreferencesGroup(
            title="Quick Settings",
            description="What the shell extension does to the Quick Settings panel")
        self.hide_pm_row = Adw.SwitchRow(
            title="Hide GNOME's Power Mode tile",
            subtitle="Our Power Mode tile switches the same profile through asusd "
                     "(with power-profiles-daemon as fallback), so the shell's own "
                     "tile is redundant while this extension runs")
        self.hide_pm_row.connect("notify::active", self._hide_pm)
        self.qs_group.add(self.hide_pm_row)
        self.hide_nl_row = Adw.SwitchRow(
            title="Hide GNOME's Night Light tile",
            subtitle="The shell's own Night Light toggle leaves the Quick Settings "
                     "panel while this extension runs; Night Light itself keeps "
                     "working from Settings and the panel icon")
        self.hide_nl_row.connect("notify::active", self._hide_nl)
        self.qs_group.add(self.hide_nl_row)
        self.set_child(prefs_page(self.panel, self.gestures, self.qs_group))

    # -- lifecycle & layout helpers ------------------------------------------------------
    def on_hidden(self) -> None:
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = None
            self._commit_speed()

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = None

    def _ensure_gesture_rows(self, reorder: bool = True) -> None:
        if not self.gest.available:
            return
        added = False
        for key, title, sub in GESTURE_ROWS:
            if key in self.gesture_rows:
                continue
            choices = self.gest.binding_choices(key)
            if not choices:
                continue
            self._binding_choices[key] = choices
            row = Adw.ComboRow(
                title=title,
                model=Gtk.StringList.new([ACTION_LABELS[a] for a in choices]),
            )
            if sub:
                row.set_subtitle(sub)
            row.connect("notify::selected", self._gesture, key)
            row.connect("notify::selected-item", _combo_tip)
            _combo_tip(row)
            self.gesture_rows[key] = row
            self.gestures.add(row)
            added = True
        if added and reorder:
            self.gestures.remove(self.app_row)
            self.gestures.remove(self.gest_err)
            self.gestures.add(self.app_row)
            self.gestures.add(self.gest_err)

    # -- reads ---------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.pad is None:
            try:
                self.pad = touchpad.TouchpadSettings()
            except touchpad.SchemaMissing:
                self.pad = None
        if not self.gest.available:
            self.gest = touchpad.GestureSettings()
        if self.gest.available:
            self._ensure_gesture_rows(reorder=True)

        sync_info = touchpad.session_info(query_shell=False)
        banner = classify_session_banner(sync_info, self.gest.available)
        self._quiet = True
        self._apply_pad_state(sync_info, banner)
        self._apply_gest_state(banner)
        self._quiet = False

        if self.gest.available and not sync_info.is_other_desktop:
            self._load_apps_async(self.gest.launch_app())
        if sync_info.installed and sync_info.schema_compiled and not sync_info.is_other_desktop:
            self._session_seq += 1
            seq = self._session_seq
            run_bg(
                touchpad.session_info,
                done=lambda info: self._on_session_info(seq, info),
                error=lambda _exc: None,
            )

    def _apply_pad_state(self, info: touchpad.SessionInfo, banner: SessionBanner) -> None:
        can_pad = banner.pad_applicable and self.pad is not None
        for r in self.pad_rows:
            r.set_sensitive(can_pad)
        self.speed_scale.set_sensitive(can_pad)
        if not banner.pad_applicable:
            self.pad_err.set_title("Touchpad settings apply to GNOME")
            self.pad_err.set_subtitle(
                f"You are running {info.desktop}. Touchpad settings here use GNOME's "
                f"desktop schema and do not affect {info.desktop}; configure your "
                "touchpad in your desktop settings instead."
            )
            self.pad_err.set_visible(True)
            return
        if self.pad is None:
            self.pad_err.set_title("Touchpad settings unavailable")
            self.pad_err.set_subtitle(
                "The desktop's touchpad schema could not be read, so the controls "
                "are off. Restart Asusius to try again."
            )
            self.pad_err.set_visible(True)
            return
        self.pad_err.set_visible(False)
        value = self.pad.send_events()
        self.on_row.set_active(value != "disabled")
        self.mouse_row.set_active(value == "disabled-on-external-mouse")
        self.mouse_row.set_sensitive(value != "disabled")
        self.tap_row.set_active(self.pad.tap_to_click())
        self.natural_row.set_active(self.pad.natural_scroll())
        self.speed_adj.set_value(self.pad.speed())
        self.speed_val.set_label(self._speed_text(self.pad.speed()))
        choices = self.pad.click_method_choices()
        self.click_row.set_model(Gtk.StringList.new([CLICK_LABELS.get(c, c) for c in choices]))
        method = self.pad.click_method()
        self.click_row.set_selected(choices.index(method) if method in choices else 0)

    def _apply_gest_state(self, banner: SessionBanner) -> None:
        self._apply_banner(banner)
        if self.gest.available and banner.pad_applicable:
            self.enabled_row.set_active(self.gest.enabled())
            for key, row in self.gesture_rows.items():
                choices = self._binding_choices[key]
                action = self.gest.binding(key)
                row.set_selected(choices.index(action) if action in choices else 0)
            self._select_launch_app(self.gest.launch_app())
            pm = self.gest.hides_power_mode()
            self.hide_pm_row.set_visible(pm is not None)
            if pm is not None:
                self.hide_pm_row.set_active(pm)
            nl = self.gest.hides_night_light()
            self.hide_nl_row.set_visible(nl is not None)
            if nl is not None:
                self.hide_nl_row.set_active(nl)
            self.qs_group.set_visible(banner.show_qs_group and (pm is not None or nl is not None))
        else:
            self.enabled_row.set_active(False)
            self.qs_group.set_visible(False)
        self._gesture_sensitivity(banner)

    def _apply_banner(self, banner: SessionBanner) -> None:
        self.ext_val.set_label(banner.ext_label)
        self.enable_btn.set_visible(banner.show_enable)
        self.enable_btn.set_sensitive(banner.show_enable)
        self.gest_err.set_visible(banner.err_visible)
        if banner.err_visible:
            self.gest_err.set_title(banner.err_title)
            self.gest_err.set_subtitle(banner.err_subtitle)

    def _on_session_info(self, seq: int, info: touchpad.SessionInfo) -> None:
        if self._destroyed or seq != self._session_seq:
            return
        self._session_info = info
        banner = classify_session_banner(info, self.gest.available)
        self._apply_banner(banner)
        if self.gest.available and banner.show_qs_group:
            pm = self.gest.hides_power_mode()
            nl = self.gest.hides_night_light()
            self.qs_group.set_visible(pm is not None or nl is not None)
        else:
            self.qs_group.set_visible(False)
        self._gesture_sensitivity(banner)

    def _load_apps_async(self, selected_id: str) -> None:
        if not self._apps_loaded and selected_id and all(
            aid != selected_id for aid, _n in self._apps
        ):
            self._apps = [("", "None"), (selected_id, selected_id)]
            self.app_row.set_model(Gtk.StringList.new([n for _i, n in self._apps]))
            self._select_launch_app(selected_id)
        self._apps_seq += 1
        seq = self._apps_seq
        run_bg(
            _discover_apps,
            selected_id,
            done=lambda apps: self._apply_apps(seq, apps),
            error=lambda _exc: None,
        )

    def _apply_apps(self, seq: int, apps: list[tuple[str, str]]) -> None:
        if self._destroyed or seq != self._apps_seq:
            return
        self._apps_loaded = True
        was_quiet = self._quiet
        self._quiet = True
        self._apps = apps
        self.app_row.set_model(Gtk.StringList.new([name for _i, name in apps]))
        if self.gest.available:
            self._select_launch_app(self.gest.launch_app())
        _combo_tip(self.app_row)
        self._quiet = was_quiet

    def _select_launch_app(self, app_id: str) -> None:
        self.app_row.set_selected(
            next((i for i, (aid, _n) in enumerate(self._apps) if aid == app_id), 0)
        )
        _combo_tip(self.app_row)

    def _enable_extension(self, *_args) -> None:
        self.enable_btn.set_sensitive(False)
        run_bg(
            touchpad.enable_extension,
            done=self._on_enable_done,
            error=lambda exc: self._on_enable_error(exc),
        )

    def _on_enable_done(self, result: tuple[bool, str]) -> None:
        if self._destroyed:
            return
        ok, message = result
        self.win.toast(message)
        if not ok:
            self.enable_btn.set_sensitive(True)
        self.refresh()

    def _on_enable_error(self, exc: Exception) -> None:
        if self._destroyed:
            return
        self.win.toast_error(exc)
        self.enable_btn.set_sensitive(True)

    @staticmethod
    def _speed_text(v: float) -> str:
        pct = round(v * 100)
        return f"{pct:+d}%" if pct else "0%"

    def _gesture_sensitivity(self, banner: SessionBanner | None = None) -> None:
        allowed = self.gest.available and (banner.gest_interactive if banner else True)
        live = allowed and self.enabled_row.get_active()
        for row in (*self.gesture_rows.values(), self.app_row):
            row.set_sensitive(live)
        self.enabled_row.set_sensitive(allowed)
        self.hide_pm_row.set_sensitive(allowed)
        self.hide_nl_row.set_sensitive(allowed)

    def _hide_pm(self, row=None, *_pspec) -> None:
        self._toggle_qs_hide(row, self.gest.set_hides_power_mode, "GNOME's Power Mode tile")

    def _hide_nl(self, row=None, *_pspec) -> None:
        self._toggle_qs_hide(row, self.gest.set_hides_night_light, "GNOME's Night Light tile")

    def _toggle_qs_hide(self, row, setter, tile_name: str) -> None:
        if self._quiet or not self.gest.available or row is None:
            return
        on = row.get_active()

        def write() -> None:
            if not setter(on):
                raise RuntimeError("This extension version does not support hiding that tile.")

        self._write_gest(write, f"{tile_name} {'hidden' if on else 'shown'}")

    # -- writes: GSettings calls are sync-fast, so they apply on the main thread ------------
    def _send_events(self, *_args) -> None:
        if self._quiet or self.pad is None:
            return
        value = ("disabled" if not self.on_row.get_active()
                 else "disabled-on-external-mouse" if self.mouse_row.get_active()
                 else "enabled")
        if value == self.pad.send_events():
            return
        self._write_setting(
            lambda: self.pad.set_send_events(value),
            "Touchpad off while a mouse is connected"
            if value == "disabled-on-external-mouse"
            else f"Touchpad {'on' if value == 'enabled' else 'off'}",
            after=lambda: self.mouse_row.set_sensitive(value != "disabled"),
        )

    def _tap(self, row=None, *_pspec) -> None:
        if self._quiet or self.pad is None or row is None:
            return
        self._write_pad(lambda: self.pad.set_tap_to_click(row.get_active()),
                        f"Tap to click {'on' if row.get_active() else 'off'}")

    def _natural(self, row=None, *_pspec) -> None:
        if self._quiet or self.pad is None or row is None:
            return
        self._write_pad(lambda: self.pad.set_natural_scroll(row.get_active()),
                        f"Natural scrolling {'on' if row.get_active() else 'off'}")

    def _speed(self, adj) -> None:
        if self._quiet or self.pad is None:
            return
        self.speed_val.set_label(self._speed_text(adj.get_value()))
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(SPEED_DEBOUNCE_MS, self._commit_speed)

    def _commit_speed(self):
        self._pending = None
        if self._destroyed or self.pad is None:
            return False
        v = self.speed_adj.get_value()
        if abs(self.pad.speed() - v) < 1e-9:  # the slider settled on the stored value
            return False
        self._write_setting(
            lambda: self.pad.set_speed(v),
            f"Touchpad speed {self._speed_text(v)}",
        )
        return False

    def _click(self, row=None, *_pspec) -> None:
        if self._quiet or self.pad is None or row is None:
            return
        choice = self.pad.click_method_choices()
        i = row.get_selected()
        if i < 0 or i >= len(choice):
            return
        self._write_pad(lambda: self.pad.set_click_method(choice[i]),
                        f"Click method set to {CLICK_LABELS.get(choice[i], choice[i])}")

    def _write_pad(self, write, ok_text: str) -> None:
        if self._quiet or self.pad is None:
            return
        self._write_setting(write, ok_text)

    def _gest_enabled(self, row=None, *_pspec) -> None:
        if self._quiet or not self.gest.available or row is None:
            return
        on = row.get_active()
        self._write_gest(lambda: self.gest.set_enabled(on), f"Gestures {'on' if on else 'off'}",
                         after=self._gesture_sensitivity)

    def _gesture(self, row, _pspec=None, key=None) -> None:
        # notify::selected delivers (row, pspec) plus the connect-time key: the pspec
        # slot must exist or every binding change died in a TypeError before the write
        if key is None:
            key = _pspec
        if self._quiet or not self.gest.available:
            return
        i = row.get_selected()
        choices = self._binding_choices[key]
        action = choices[i] if 0 <= i < len(choices) else "none"
        self._write_gest(lambda: self.gest.set_binding(key, action),
                         f"{row.get_title()} set to {ACTION_LABELS[action]}")

    def _launch_app(self, row, _pspec=None) -> None:
        if self._quiet or not self.gest.available:
            return
        i = row.get_selected()
        if i < 0 or i >= len(self._apps):
            return
        app_id, name = self._apps[i]
        self._write_gest(
            lambda: self.gest.set_launch_app(app_id),
            "Launch app gesture does nothing" if not app_id
            else f"Launch app gesture starts {name}",
        )

    def _write_gest(self, write, ok_text: str, after=None) -> None:
        self._write_setting(write, ok_text, after=after)

    def _write_setting(self, write, ok_text: str, after=None) -> None:
        try:
            write()
        except Exception as exc:
            self.win.toast_error(exc)
            self.refresh()
            return
        self.win.toast(ok_text)
        if after:
            after()


def _combo_tip(row: Adw.ComboRow, _pspec=None) -> None:
    """ComboRow ellipsizes its selected-item label when the row is narrow; put the full
    text on the row as a tooltip (the audit inherits ancestor tooltips, and unlike the
    row-internal label this survives the factory rebinding the label)."""
    item = row.get_selected_item()
    row.set_tooltip_text(str(item.get_string()) if item is not None else None)
