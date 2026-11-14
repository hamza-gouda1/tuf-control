# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Asusius GTK application."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..controller import Controller  # noqa: E402
from . import prefs  # noqa: E402

APP_ID = "io.github.hamzagouda.Asusius"
SCHEME_ENV = "ASUSIUS_COLOR_SCHEME"  # "dark" | "light" (screenshots); default follows system


def _icon_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    candidates = [here.parent / "icons"]
    if len(here.parents) > 3:
        candidates.append(here.parents[3] / "data" / "icons")
    return [p for p in candidates if p.is_dir()]


def _show_start_page(window, start_page: str | None) -> bool:
    """Show a requested page without building pages just to validate its key."""
    if not start_page:
        return False
    if start_page in window.pages:
        window.show_page(start_page)
        return True
    valid_keys = ", ".join(window.pages)
    print(f"asusius-gui: unknown page {start_page!r}; valid pages: {valid_keys}",
          file=sys.stderr)
    return False


class AsusiusApp(Adw.Application):
    def __init__(self, controller: Controller | None = None) -> None:
        from .lean import choose_renderer
        choose_renderer()  # before the display opens: Cairo unless asked otherwise
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.controller = controller
        self.window = None
        self.styles = None
        self.start_page: str | None = None
        self.add_main_option("page", ord("p"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING, "open this page on start", "KEY")

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        env_scheme = os.environ.get(SCHEME_ENV, "").lower()
        scheme = env_scheme if env_scheme in ("dark", "light") else prefs.get_scheme()
        self._apply_scheme(scheme)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for d in _icon_dirs():
            theme.add_search_path(str(d))
        from .theme import Styles
        self.styles = Styles()

        scheme_act = Gio.SimpleAction.new_stateful("scheme", GLib.VariantType.new("s"),
                                                   GLib.Variant("s", scheme))
        scheme_act.connect("change-state", self._on_scheme_action)
        self.add_action(scheme_act)

        unit_act = Gio.SimpleAction.new_stateful("temp_unit", GLib.VariantType.new("s"),
                                                 GLib.Variant("s", prefs.get_temp_unit()))
        unit_act.connect("change-state", self._on_temp_unit_action)
        self.add_action(unit_act)

        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_accels_for_action("win.quick-jump", ["<Control>k"])
        self.set_accels_for_action("win.shortcuts", ["<Control>question", "F1"])
        self.set_accels_for_action("win.refresh-all", ["<Control>r", "F5"])

    def _apply_scheme(self, scheme: str) -> None:
        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme({"dark": Adw.ColorScheme.FORCE_DARK,
                             "light": Adw.ColorScheme.FORCE_LIGHT}.get(scheme,
                                                                       Adw.ColorScheme.DEFAULT))

    def _on_scheme_action(self, act: Gio.SimpleAction, value: GLib.Variant) -> None:
        scheme = value.get_string()
        if scheme not in prefs.VALID_SCHEMES:
            return
        act.set_state(value)
        prefs.save_prefs(scheme=scheme)
        self._apply_scheme(scheme)

    def _on_temp_unit_action(self, act: Gio.SimpleAction, value: GLib.Variant) -> None:
        unit = value.get_string()
        if unit not in prefs.VALID_TEMP_UNITS:
            return
        act.set_state(value)
        prefs.save_prefs(temp_unit=unit)
        if self.window is not None:
            self.window.refresh_temperature_unit()

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        page = options.lookup_value("page", GLib.VariantType("s"))
        if page is not None and page.get_string():
            self.start_page = page.get_string()
        return -1  # continue startup

    def do_activate(self) -> None:
        if self.window is None:
            from .window import MainWindow
            self.window = MainWindow(self, self.controller or Controller())
        _show_start_page(self.window, self.start_page)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    app = AsusiusApp()
    return app.run(sys.argv if argv is None else argv)
