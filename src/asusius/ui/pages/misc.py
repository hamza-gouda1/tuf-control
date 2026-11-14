# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""About page."""
from __future__ import annotations

import platform

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ... import __version__
from ..widgets import label
from .base import Page, button, pill, prefs_page

REPO_URL = "https://github.com/hamza-gouda1/Asusius"
ISSUES_URL = "https://github.com/hamza-gouda1/Asusius/issues"

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
        hero.append(label("Asusius", ("about-name",), xalign=0.5))
        hero.append(label(f"Version {__version__}", ("about-version",), xalign=0.5))
        hero.append(label("Fans, power, graphics and lighting for ASUS TUF and ROG laptops.",
                          ("stat-sub",), xalign=0.5, wrap=True, margin_top=6))
        g0 = Adw.PreferencesGroup()
        g0.add(hero)

        self.support = Adw.PreferencesGroup(
            title="Hardware support",
            description="What this system exposes and which backend handles it.")
        copy_btn = button("Copy diagnostics", "flat", icon="edit-copy-symbolic",
                          on_click=self._copy_diagnostics)
        copy_btn.set_valign(Gtk.Align.CENTER)
        self.support.set_header_suffix(copy_btn)
        self.rows: list[Gtk.Widget] = []

        g2 = Adw.PreferencesGroup(title="Project")
        repo = Adw.ActionRow(title="Source repository", subtitle=REPO_URL, activatable=True)
        repo.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        repo.connect("activated", lambda *_: self._open_url(REPO_URL))
        issues = Adw.ActionRow(title="Report a bug or hardware quirk", subtitle=ISSUES_URL,
                               activatable=True)
        issues.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        issues.connect("activated", lambda *_: self._open_url(ISSUES_URL))
        lic = Adw.ActionRow(title="License", subtitle="GPL-3.0-or-later · © Hamza Gouda")
        for r in (repo, issues, lic):
            g2.add(r)
        self.set_child(prefs_page(g0, self.support, g2))

    def _open_url(self, uri: str) -> None:
        try:
            Gtk.UriLauncher.new(uri).launch(self.win, None, None, None)
        except Exception:
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except Exception:
                self.win.toast(f"Open: {uri}")

    def _copy_diagnostics(self) -> None:
        lines = [
            f"Asusius {__version__}",
            f"System: {platform.system()} {platform.release()} ({platform.machine()})",
            f"Python: {platform.python_version()} | GTK {Gtk.get_major_version()}."
            f"{Gtk.get_minor_version()}.{Gtk.get_micro_version()} | Adw {Adw.get_major_version()}."
            f"{Adw.get_minor_version()}.{Adw.get_micro_version()}",
            f"Mode: {'fake' if self.win.fake else 'hardware'} | Active profile: "
            f"{self.win.profile.value if self.win.profile else 'unknown'}",
        ]
        if self.win.caps_error:
            lines.append(f"Capability probe error: {self.win.caps_error}")
        caps = self.win.caps or {}
        for key, title in CAP_TITLES.items():
            info = caps.get(key)
            if not info:
                continue
            if info.get("available"):
                lines.append(f"  [ok] {title}: {info.get('backend')}")
            else:
                reasons = "; ".join(str(v) for v in (info.get("unavailable") or {}).values() if v)
                lines.append(f"  [--] {title}: {reasons or 'unsupported'}")
        text = "\n".join(lines)
        try:
            disp = Gdk.Display.get_default()
            if disp is not None:
                disp.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
            self.win.toast("Diagnostics copied to clipboard")
        except Exception:
            self.win.toast("Could not access clipboard")

    def refresh(self) -> None:
        for r in self.rows:
            self.support.remove(r)
        self.rows = []
        caps = self.win.caps or {}
        ordered = sorted(
            CAP_TITLES.items(),
            key=lambda kv: (0 if (caps.get(kv[0]) or {}).get("available") else 1, kv[1]),
        )
        for key, title in ordered:
            info = caps.get(key)
            if info is None:
                continue
            if info["available"]:
                sub = f"Handled by {info['backend']}"
                if info["fallbacks"]:
                    sub += f" · fallback {', '.join(info['fallbacks'])}"
                badge = pill("Available", "accent")
                reasons = []
            else:
                reasons = [str(v) for v in info["unavailable"].values() if v]
                if reasons:
                    sub = f"Unavailable — {reasons[0]}"
                else:
                    sub = "Not provided by firmware or driver on this laptop."
                badge = pill("Unavailable", "dim")
            row = Adw.ActionRow(title=GLib.markup_escape_text(title),
                                subtitle=GLib.markup_escape_text(sub))
            row.set_subtitle_lines(0)
            if reasons:
                row.set_tooltip_text("\n".join(reasons))
            row.add_suffix(badge)
            self.support.add(row)
            self.rows.append(row)
