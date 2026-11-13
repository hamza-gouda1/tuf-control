# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, GLib, Gio, Gtk

from ... import touchpad
from ..widgets import label
from .base import Page, a11y_name, info_row, prefs_page, value_row

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
        self._apps: list[tuple[str, str]] = [("", "None")]  # (desktop id, shown name)
        self._apps_loaded = False

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
        self.enabled_row = Adw.SwitchRow(
            title="Gestures enabled",
            subtitle="Run the selected action after each configured gesture")
        self.enabled_row.connect("notify::active", self._gest_enabled)
        self.gestures.add(self.ext_row)
        self.gestures.add(self.enabled_row)
        self.gesture_rows: dict[str, Adw.ComboRow] = {}
        self._binding_choices: dict[str, tuple[str, ...]] = {}
        for key, title, sub in GESTURE_ROWS:
            choices = self.gest.binding_choices(key) if self.gest.available else ()
            if not choices:
                continue
            self._binding_choices[key] = choices
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new([ACTION_LABELS[a] for a in choices]))
            if sub:
                row.set_subtitle(sub)
            row.connect("notify::selected", self._gesture, key)
            row.connect("notify::selected-item", _combo_tip)
            _combo_tip(row)
            self.gesture_rows[key] = row
            self.gestures.add(row)
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

    # -- reads ---------------------------------------------------------------------------
    def refresh(self) -> None:
        if not self._apps_loaded:
            self._load_apps()
        self._quiet = True
        for r in self.pad_rows:
            r.set_sensitive(self.pad is not None)
        self.speed_scale.set_sensitive(self.pad is not None)
        self.pad_err.set_visible(self.pad is None)
        if self.pad is not None:
            value = self.pad.send_events()
            self.on_row.set_active(value != "disabled")
            self.mouse_row.set_active(value == "disabled-on-external-mouse")
            self.mouse_row.set_sensitive(value != "disabled")
            self.tap_row.set_active(self.pad.tap_to_click())
            self.natural_row.set_active(self.pad.natural_scroll())
            self.speed_adj.set_value(self.pad.speed())
            self.speed_val.set_label(self._speed_text(self.pad.speed()))
            choices = self.pad.click_method_choices()
            self.click_row.set_model(Gtk.StringList.new([CLICK_LABELS.get(c, c)
                                                         for c in choices]))
            method = self.pad.click_method()
            self.click_row.set_selected(choices.index(method) if method in choices else 0)
        if self.gest.available:
            self.gest_err.set_visible(False)
            self.ext_val.set_label("Installed")
            self.enabled_row.set_active(self.gest.enabled())
            for key, row in self.gesture_rows.items():
                choices = self._binding_choices[key]
                action = self.gest.binding(key)
                row.set_selected(choices.index(action) if action in choices else 0)
            app_id = self.gest.launch_app()
            self.app_row.set_selected(next((i for i, (aid, _n) in enumerate(self._apps)
                                            if aid == app_id), 0))
            pm = self.gest.hides_power_mode()
            # None: the installed extension predates the key — nothing to show
            self.hide_pm_row.set_visible(pm is not None)
            if pm is not None:
                self.hide_pm_row.set_active(pm)
            nl = self.gest.hides_night_light()
            # None: the installed extension predates the key — nothing to show
            self.hide_nl_row.set_visible(nl is not None)
            if nl is not None:
                self.hide_nl_row.set_active(nl)
        else:
            self.ext_val.set_label("Not installed")
            self.enabled_row.set_active(False)
            self.gest_err.set_subtitle(
                "The asusius-quick shell extension is not installed, so there is "
                "nothing to bind the gestures with. Run install.sh, enable the extension "
                "with gnome-extensions, then start Asusius again.")
            self.gest_err.set_visible(True)
        self._gesture_sensitivity()
        self._quiet = False

    def _load_apps(self) -> None:
        """Installed applications for the launch-app binding: a plain Gio.AppInfo walk,
        the same enumeration the Processes page does. Cached for the page's life."""
        self._apps_loaded = True
        apps, seen = [("", "None")], set()
        for info in Gio.AppInfo.get_all():
            if not isinstance(info, Gio.DesktopAppInfo) or not info.should_show():
                continue
            app_id = info.get_id() or ""
            if not app_id or app_id in seen:
                continue
            seen.add(app_id)
            apps.append((app_id, info.get_name() or app_id))
        apps.sort(key=lambda a: (a[0] == "", a[1].casefold()))
        self._apps = apps
        self.app_row.set_model(Gtk.StringList.new([name for _i, name in apps]))
        _combo_tip(self.app_row)

    @staticmethod
    def _speed_text(v: float) -> str:
        pct = round(v * 100)
        return f"{pct:+d}%" if pct else "0%"

    def _gesture_sensitivity(self) -> None:
        live = self.gest.available and self.enabled_row.get_active()
        for row in (*self.gesture_rows.values(), self.app_row):
            row.set_sensitive(live)
        self.enabled_row.set_sensitive(self.gest.available)
        self.hide_pm_row.set_sensitive(self.gest.available)
        self.hide_nl_row.set_sensitive(self.gest.available)

    def _hide_pm(self, row=None, *_pspec) -> None:
        if self._quiet or not self.gest.available:
            return
        on = row.get_active()
        if self.gest.set_hides_power_mode(on):
            self._write_gest_text("GNOME's Power Mode tile hidden" if on
                                  else "GNOME's Power Mode tile shown")

    def _hide_nl(self, row=None, *_pspec) -> None:
        if self._quiet or not self.gest.available:
            return
        on = row.get_active()
        if self.gest.set_hides_night_light(on):
            self._write_gest_text("GNOME's Night Light tile hidden" if on
                                  else "GNOME's Night Light tile shown")

    # -- writes: GSettings calls are sync-fast, so they apply on the main thread ------------
    def _send_events(self, *_args) -> None:
        if self._quiet or self.pad is None:
            return
        value = ("disabled" if not self.on_row.get_active()
                 else "disabled-on-external-mouse" if self.mouse_row.get_active()
                 else "enabled")
        if value == self.pad.send_events():
            return
        try:
            self.pad.set_send_events(value)
        except Exception as exc:
            self.win.toast_error(exc)
            self.refresh()
            return
        self.win.toast("Touchpad off while a mouse is connected"
                       if value == "disabled-on-external-mouse"
                       else f"Touchpad {'on' if value == 'enabled' else 'off'}")

    def _tap(self, row=None, *_pspec) -> None:
        self._write_pad(lambda: self.pad.set_tap_to_click(row.get_active()),
                        f"Tap to click {'on' if row.get_active() else 'off'}")

    def _natural(self, row=None, *_pspec) -> None:
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
        v = self.speed_adj.get_value()
        if abs(self.pad.speed() - v) < 1e-9:  # the slider settled on the stored value
            return False
        self._write_pad(lambda: self.pad.set_speed(v), f"Touchpad speed {self._speed_text(v)}")
        return False

    def _click(self, row=None, *_pspec) -> None:
        if self._quiet or self.pad is None:
            return
        choice = self.pad.click_method_choices()
        i = row.get_selected()
        if i < 0 or i >= len(choice):
            return
        self._write_pad(lambda: self.pad.set_click_method(choice[i]),
                        f"Click method set to {CLICK_LABELS.get(choice[i], choice[i])}")

    def _write_pad(self, write, ok_text: str) -> None:
        try:
            write()
        except Exception as exc:
            self.win.toast_error(exc)
            self.refresh()
            return
        self.win.toast(ok_text)

    def _gest_enabled(self, row=None, *_pspec) -> None:
        if self._quiet or not self.gest.available:
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

        def write():
            self.gest.set_launch_app(app_id)

        self._write_gest(write, "Launch app gesture does nothing" if not app_id
                         else f"Launch app gesture starts {name}")

    def _write_gest_text(self, ok_text: str) -> None:
        self.win.toast(ok_text)

    def _write_gest(self, write, ok_text: str, after=None) -> None:
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
