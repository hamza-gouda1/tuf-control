"""TUF Control GTK application."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from ..controller import Controller  # noqa: E402

APP_ID = "io.github.hamzagouda.TufControl"
SCHEME_ENV = "TUF_CONTROL_COLOR_SCHEME"  # "dark" | "light" (screenshots); default follows system


def _icon_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    return [p for p in (here.parent / "icons", here.parents[3] / "data" / "icons") if p.is_dir()]


class TufControlApp(Adw.Application):
    def __init__(self, controller: Controller | None = None) -> None:
        from .lean import choose_renderer
        choose_renderer()  # before the display opens: Cairo unless asked otherwise
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.controller = controller
        self.window = None
        self.styles = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        scheme = os.environ.get(SCHEME_ENV, "").lower()
        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme({"dark": Adw.ColorScheme.FORCE_DARK,
                             "light": Adw.ColorScheme.FORCE_LIGHT}.get(scheme,
                                                                       Adw.ColorScheme.DEFAULT))
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for d in _icon_dirs():
            theme.add_search_path(str(d))
        from .theme import Styles
        self.styles = Styles()
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self) -> None:
        if self.window is None:
            from .window import MainWindow
            self.window = MainWindow(self, self.controller or Controller())
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    app = TufControlApp()
    return app.run(sys.argv if argv is None else argv)
