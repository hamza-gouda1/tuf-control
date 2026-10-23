"""About page."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ... import __version__
from ..widgets import label
from .base import Page, pill, prefs_page

CAP_TITLES = {
    "profile": "Power profile",
    "power_limits": "Power limits",
    "firmware_attributes": "Firmware settings",
    "battery_threshold": "Charge limit",
    "battery_health": "Battery health",
    "gpu_mode": "GPU mode",
    "kbd_brightness": "Keyboard brightness",
    "kbd_rgb": "Keyboard effects",
    "fan_curves": "Fan curves",
    "telemetry": "Sensors",
}


class AboutPage(Page):
    key, title = "about", "About"

    def __init__(self, win) -> None:
        super().__init__(win)
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       halign=Gtk.Align.CENTER, margin_bottom=8)
        icon = Gtk.Image.new_from_icon_name(win.get_application().get_application_id())
        icon.set_pixel_size(96)
        icon.set_margin_bottom(10)
        hero.append(icon)
        hero.append(label("TUF Control", ("about-name",), xalign=0.5))
        hero.append(label(f"Version {__version__}", ("about-version",), xalign=0.5))
        hero.append(label("Fans, power, graphics and lighting for ASUS TUF and ROG laptops.",
                          ("stat-sub",), xalign=0.5, wrap=True, margin_top=6))
        g0 = Adw.PreferencesGroup()
        g0.add(hero)
        self.support = Adw.PreferencesGroup(
            title="Hardware support", description="What this laptop exposes, and through what.")
        self.rows = []
        g2 = Adw.PreferencesGroup()
        lic = Adw.ActionRow(title="License", subtitle="GPL-3.0-or-later · © Hamza Gouda")
        g2.add(lic)
        self.set_child(prefs_page(g0, self.support, g2))

    def refresh(self) -> None:
        for r in self.rows:
            self.support.remove(r)
        self.rows = []
        caps = self.win.caps or {}
        for key, title in CAP_TITLES.items():
            info = caps.get(key)
            if info is None:
                continue
            if info["available"]:
                sub = f"via {info['backend']}"
                if info["fallbacks"]:
                    sub += f", fallback {', '.join(info['fallbacks'])}"
                badge = pill("Available", "accent")
            else:
                reasons = [v for v in info["unavailable"].values() if v]
                sub = reasons[0] if reasons else "No backend"
                sub = sub[:1].upper() + sub[1:]
                badge = pill("Unavailable")
            row = Adw.ActionRow(title=title, subtitle=sub)
            row.set_subtitle_lines(2)
            row.add_suffix(badge)
            self.support.add(row)
            self.rows.append(row)
