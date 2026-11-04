# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""'Effects (software)' section of the Keyboard page: picker, parameters, live preview,
service status. The effect itself runs in tuf-controld; this only edits the config."""
from __future__ import annotations

import time

from gi.repository import Adw, Gdk, GLib, Gtk

from ...automation.config import load_config, update_config
from ...automation.service import DaemonClient
from ...core import BackendError, KbdEffect
from ...effects.colour import hex_to_rgb, rgb_to_hex
from ...effects.preview import EffectPreview
from ...effects.registry import EFFECTS
from ...effects.theme import theme_accent
from ..tasks import run_bg
from ..viewmodels import PALETTES, FrameGate

HW_NOTE = "Runs on the keyboard itself, no CPU cost."
from ..widgets import _round_rect, label, palette_buttons
from .base import button, info_row, pill

NAMES = list(EFFECTS)


def _rgba(rgb) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0
    return c


class Swatch(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__(content_width=56, content_height=30, valign=Gtk.Align.CENTER)
        self.rgb: tuple[int, int, int] | None = None
        self.set_draw_func(self._draw)

    def set_rgb(self, rgb) -> None:
        if rgb != self.rgb:
            self.rgb = rgb
            self.queue_draw()

    def _draw(self, _a, cr, w, h):
        _round_rect(cr, 0.5, 0.5, w - 1, h - 1, 8)
        if self.rgb is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.18)
        else:
            cr.set_source_rgb(*(c / 255 for c in self.rgb))
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.14)
        cr.set_line_width(1)
        cr.stroke()


class EffectsSection(Adw.PreferencesGroup):
    def __init__(self, page) -> None:
        super().__init__(title="Effects (software)",
                         description="Run by tuf-controld at a few writes per second. Off "
                                     "brings the hardware effect back.")
        self.page, self.win = page, page.win
        self.client = DaemonClient()
        self.status: dict = {"running": False}
        self.saved: dict | None = None   # effects section as in the config file
        self.values: dict[str, dict] = {n: {} for n in NAMES}
        self.notif: dict = {}
        self._quiet = False
        self._timer = None
        self._ticks = 0
        self._timer_ms = 0
        self._root = None
        self._root_ids: list[int] = []
        self._last_t = 0.0
        self.gate = FrameGate()
        self._shown_rgb = None
        self._soft_shown = False
        self._last_error: str | None = None
        self.telemetry = None
        self.preview = EffectPreview(telemetry=lambda: self.telemetry,
                                     profile=lambda: self.win.profile.value
                                     if self.win.profile else None,
                                     theme=theme_accent)

        self.apply_btn = button("Apply", "suggested-action", on_click=self.apply)
        self.apply_btn.set_sensitive(False)
        self.set_header_suffix(self.apply_btn)

        self.pick = Adw.ComboRow(title="Effect", model=Gtk.StringList.new(
            ["Off"] + [EFFECTS[n].title for n in NAMES]))
        self.pick.set_subtitle_lines(0)
        self.pick.connect("notify::selected", lambda *_: self._picked())
        self.add(self.pick)
        self.param_rows: list[Gtk.Widget] = []
        self.note = info_row("", "", "dialog-information-symbolic")
        self.note.set_visible(False)
        self.add(self.note)

        self.flash = Adw.SwitchRow(title="Flash on notifications",
                                   subtitle="Above any effect. Read-only session-bus monitor.")
        self.flash.add_prefix(Gtk.Image.new_from_icon_name(
            "preferences-system-notifications-symbolic"))
        self.flash.connect("notify::active", lambda *_: self._edited())
        self.add(self.flash)

        self.prev_row = Adw.ActionRow(title="Preview", subtitle="Simulated inputs")
        self.prev_row.add_prefix(Gtk.Image.new_from_icon_name("view-reveal-symbolic"))
        self.hex = label("", ("numeric", "dim-label"), valign=Gtk.Align.CENTER)
        self.swatch = Swatch()
        self.test_flash = button("Flash", "flat", on_click=self._flash)
        self.test_flash.set_tooltip_text("Preview a notification flash")
        for w in (self.test_flash, self.hex, self.swatch):
            self.prev_row.add_suffix(w)
        self.add(self.prev_row)

        self.svc_row = Adw.ActionRow(title="Background service", subtitle=
                                     "Checking… Runs effects, automation rules and update checks.")
        self.svc_row.add_prefix(Gtk.Image.new_from_icon_name("system-run-symbolic"))
        self.svc_pill = pill("Stopped")
        self.svc_btn = button("Start", on_click=self._toggle_service)
        self.svc_row.add_suffix(self.svc_pill)
        self.svc_row.add_suffix(self.svc_btn)
        self.add(self.svc_row)

        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._on_unmap())
        self._picked()

    # -- state --------------------------------------------------------------
    @property
    def selected(self) -> str | None:
        i = self.pick.get_selected()
        return NAMES[i - 1] if 0 < i <= len(NAMES) else None

    @property
    def active(self) -> bool:
        return self.selected is not None

    def refresh(self) -> None:
        run_bg(load_config, done=self._got_config, error=lambda e: self.win.toast_error(e))
        self.poll_status()

    def poll_status(self) -> None:
        run_bg(self.client.status, done=self._got_status, error=lambda e: None)

    def _got_config(self, cfg) -> None:
        fx = cfg["effects"]
        self.saved = fx
        for n in NAMES:
            self.values[n] = dict(fx["params"].get(n, {}))
        self.notif = {k: v for k, v in fx["notifications"].items() if k != "enabled"}
        self._quiet = True
        self.pick.set_selected(NAMES.index(fx["active"]) + 1 if fx["active"] in NAMES else 0)
        self.flash.set_active(fx["notifications"]["enabled"])
        self._quiet = False
        self._picked()
        self.apply_btn.set_sensitive(False)
        self.page.soft_config_loaded(fx)

    def _got_status(self, st) -> None:
        self.status = st
        run = st.get("running")
        fx = st.get("effects") or {}
        self.svc_pill.set_label("Running" if run else "Stopped")
        (self.svc_pill.add_css_class if run else self.svc_pill.remove_css_class)("accent")
        self.svc_btn.set_label("Stop" if run else "Start")
        err = fx.get("error") if run else None
        if err and err != self._last_error:  # failures are toasted once, never silent
            self.win.toast_error(BackendError(f"Software effect: {err}"))
        self._last_error = err
        if not run:
            sub = ("Stopped. Effects, automation rules and update checks are paused; "
                   "Apply starts the service.")
        elif err:
            sub = ("The effect stopped on an error and is paused; it retries on its own. "
                   "If it stays off, switch the effect Off and back on.")
        elif fx.get("waiting"):
            sub = f"Showing {fx.get('effect')} · {fx['waiting']}"
        elif fx.get("running"):
            eff = fx.get("effect")
            sub = f"Showing {EFFECTS[eff].title.lower() if eff in EFFECTS else eff}"
            if fx.get("override"):
                sub += " (set by a rule)"
            if fx.get("backlight_off"):
                sub += " · paused, backlight off"
        else:
            sub = "Running · keyboard in hardware mode"
        if run:
            sub += ". Stop also pauses automation rules and background update checks."
        self.svc_row.set_subtitle(GLib.markup_escape_text(sub))
        hw = HW_NOTE
        if run and fx.get("running") and fx.get("effect") == "gradient" and self.page.form.blend:
            hw = ("Breathe with Two colours: tuf-controld fades Colour 1 ↔ Colour 2; "
                  "the firmware Breathe returns when effects are Off.")
        elif run and fx.get("running"):
            hw = "Paused while a software effect runs; it returns when effects are Off."
        self.page.hw_note = hw
        self.page.describe()

    def on_telemetry(self, t) -> None:
        self.telemetry = t
        self._ticks += 1
        if self.page.loaded and self._ticks % 2 == 0:
            self.poll_status()

    # -- params --------------------------------------------------------------
    def _clear_params(self) -> None:
        for r in self.param_rows:
            self.remove(r)
        self.param_rows = []

    def _picked(self) -> None:
        name = self.selected
        self._clear_params()
        if name:
            cls = EFFECTS[name]
            self.pick.set_subtitle(cls.description)
            # rows go between the picker and the note: re-add the fixed rows after them
            for w in (self.note, self.flash, self.prev_row, self.svc_row):
                self.remove(w)
            rows = [self._palette_row(name)] if name == "gradient" else []
            rows += [self._param_row(name, p) for p in cls.params]
            for row in rows:
                self.add(row)
                self.param_rows.append(row)
            for w in (self.note, self.flash, self.prev_row, self.svc_row):
                self.add(w)
            self.note.set_title({"ambient": "Needs permission", "audio": "Audio source",
                                 "spectrum": "Audio source"}.get(name, "Note"))
            self.note.set_subtitle(GLib.markup_escape_text(cls.note))
            self.note.set_visible(bool(cls.note))
        else:
            self.pick.set_subtitle("The keyboard runs its hardware effect")
            self.note.set_visible(False)
        self.flash.set_sensitive(name is not None)
        self.prev_row.set_visible(name is not None)
        self._edited()

    def _palette_row(self, name: str) -> Gtk.Widget:
        row = Adw.ActionRow(title="Palette", subtitle="Sets Colour 1 and Colour 2")

        def pick(i):
            _n, a, b = PALETTES[i]
            self.values[name].update(colour1=rgb_to_hex(a), colour2=rgb_to_hex(b))
            self._picked()  # rebuild the colour buttons from the new values
        row.add_suffix(palette_buttons(PALETTES, pick))
        return row

    def _param_row(self, name: str, p) -> Gtk.Widget:
        vals = self.values[name]
        cur = vals.get(p.key, p.default)

        def put(v):
            vals[p.key] = v
            self._edited()

        if p.kind == "colour":
            row = Adw.ActionRow(title=p.label)
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                        valign=Gtk.Align.CENTER)
            btn.set_rgba(_rgba(hex_to_rgb(cur)))
            btn.connect("notify::rgba", lambda b, _p: put(rgb_to_hex(
                (round(b.get_rgba().red * 255), round(b.get_rgba().green * 255),
                 round(b.get_rgba().blue * 255)))))
            row.add_suffix(btn)
            return row
        if p.kind == "bool":
            row = Adw.SwitchRow(title=p.label, active=bool(cur))
            row.connect("notify::active", lambda r, _p: put(r.get_active()))
            return row
        if p.kind == "choice":
            row = Adw.ComboRow(title=p.label, model=Gtk.StringList.new(
                [c.upper() if len(c) <= 3 else c.capitalize() for c in p.choices]))
            row.set_selected(p.choices.index(cur) if cur in p.choices else 0)
            row.connect("notify::selected", lambda r, _p: put(p.choices[r.get_selected()]))
            return row
        step = p.step or (1 if p.kind == "int" else 0.1)
        row = Adw.SpinRow.new_with_range(p.min, p.max, step)
        row.set_title(p.label)
        if p.kind == "float":
            row.set_digits(2 if step < 0.1 else 1)
        row.set_value(cur)
        row.connect("notify::value", lambda r, _p: put(
            int(r.get_value()) if p.kind == "int" else round(r.get_value(), 2)))
        return row

    # -- preview / dirty --------------------------------------------------------------------------
    def wanted(self) -> dict:
        name = self.selected
        return {"active": name, "params": dict(self.values[name]) if name else {},
                "notifications": self.flash.get_active()}

    def _edited(self) -> None:
        name = self.selected
        self.gate.wake()
        self.preview.configure(name, self.values[name] if name else {},
                               {**self.notif, "enabled": self.flash.get_active()})
        self.test_flash.set_visible(self.flash.get_active() and name is not None)
        if self._quiet or self.saved is None:
            return
        s = self.saved
        same = (s["active"] == name and s["notifications"]["enabled"] == self.flash.get_active()
                and (name is None or {**self._defaults(name), **s["params"].get(name, {})}
                     == {**self._defaults(name), **self.values[name]}))
        self.apply_btn.set_sensitive(not same)
        self._frame()
        self._sync()

    @staticmethod
    def _defaults(name: str) -> dict:
        return {p.key: p.default for p in EFFECTS[name].params}

    def _flash(self) -> None:
        self.preview.flash()
        self.gate.wake()
        self._sync()

    # The timer exists only while this section is mapped and its window is visible and not
    # suspended; it runs at 15 fps (10 when slow) and backs off to 4 Hz once the colour has
    # stopped changing.
    def _on_map(self) -> None:
        root = self.get_root()
        if root is not None and root is not self._root:
            self._drop_root()
            self._root = root
            self._root_ids = [root.connect("notify::suspended", lambda *_: self._sync()),
                              root.connect("notify::visible", lambda *_: self._sync())]
        self._sync()

    def _on_unmap(self) -> None:
        self._drop_root()
        self._sync()

    def _drop_root(self) -> None:
        if self._root is not None:
            for i in self._root_ids:
                self._root.disconnect(i)
        self._root, self._root_ids = None, []

    def _sync(self) -> None:
        r = self._root
        shown = bool(self.get_mapped() and r is not None and r.get_visible()
                     and not r.get_property("suspended"))
        if shown and self._timer is None:
            self._last_t = time.monotonic()
            self._arm()
        elif not shown and self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _arm(self) -> None:
        self._timer_ms = round(self.gate.interval * 1000)
        self._timer = GLib.timeout_add(self._timer_ms, self._on_timer)

    def _on_timer(self) -> bool:
        self._frame()
        if round(self.gate.interval * 1000) != self._timer_ms:
            self._arm()
            return False
        return True

    def _frame(self) -> None:
        now = time.monotonic()
        dt, self._last_t = min(now - self._last_t, 0.5), now
        start = time.perf_counter()
        rgb = self.preview.tick(dt) if self.active else None
        rgb = tuple(rgb) if rgb is not None else None
        self.gate.observe(rgb)
        if rgb != self._shown_rgb:  # dirty flag: touch widgets only when the colour moved
            self._shown_rgb = rgb
            self.swatch.set_rgb(rgb)
            self.hex.set_label(rgb_to_hex(rgb).upper() if rgb else "")
        if self.active and rgb is not None:
            self._soft_shown = True
            self.page.preview.set_effect(KbdEffect(0, colour1=rgb), self.page.brightness)
        elif not self.active and self._soft_shown:
            self._soft_shown = False
            self.page.preview.set_effect(self.page.effect, self.page.brightness)
        self.gate.add_cost(time.perf_counter() - start)

    # -- actions --------------------------------------------------------------
    def apply(self) -> None:
        want = self.wanted()

        def edit(cfg):
            fx = cfg["effects"]
            fx["active"] = want["active"]
            if want["active"]:
                fx["params"][want["active"]] = want["params"]
            fx["notifications"]["enabled"] = want["notifications"]

        def work():
            return self.write_config(edit, start=bool(want["active"]))

        def done(res):
            cfg, started = res
            self._got_config(cfg)
            title = EFFECTS[want["active"]].title if want["active"] else "Hardware effect"
            self.win.toast(f"{title} on" + (" · service started" if started else ""))
            GLib.timeout_add(800, lambda: (self.poll_status(), False)[1])
        run_bg(work, done=done, error=self.win.toast_error)

    def write_config(self, edit, start: bool) -> tuple[dict, object]:
        """Worker thread. Edit the effects config; start tuf-controld when an effect is on."""
        cfg = update_config(edit)
        started = None
        if start and not self.client.status()["running"]:
            started = self.client.start()
            self.client.wait_running()  # raises with the daemon's own error output
        return cfg, started

    def enable(self, name: str, params: dict) -> tuple[dict, object]:
        """Worker thread. Turn the software effect `name` on with `params` merged over its
        saved parameters (the hardware Breathe's Two colours switch uses this)."""
        def edit(cfg):
            fx = cfg["effects"]
            fx["active"] = name
            fx["params"][name] = {**fx["params"].get(name, {}), **params}
        return self.write_config(edit, start=True)

    def release_for_hardware(self, timeout_s: float = 6.0) -> bool:
        """Worker thread. Turn the configured software effect off and wait until the
        daemon has restored the hardware mode; True when something was stopped."""
        st = self.client.status()
        cfg_active = load_config()["effects"]["active"]
        running = st.get("running") and (st.get("effects") or {}).get("running")
        if not cfg_active and not running:
            return False
        if cfg_active:
            update_config(lambda c: c["effects"].__setitem__("active", None))
        if not st.get("running"):
            return bool(cfg_active)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            fx = self.client.status().get("effects") or {}
            if not fx.get("running"):
                return True
            time.sleep(0.15)
        if (self.client.status().get("effects") or {}).get("override"):
            raise BackendError("an automation rule keeps a software effect on; "
                               "turn that rule off first")
        raise BackendError("tuf-controld did not release the keyboard in time")

    def _toggle_service(self) -> None:
        run = self.status.get("running")

        def go():
            run_bg(self.client.stop if run else self.client.start,
                   done=lambda _m: (self.win.toast("Service stopped" if run else "Service started"),
                                    GLib.timeout_add(700, lambda: (self.poll_status(), False)[1])),
                   error=self.win.toast_error)

        if run:
            self.win.confirm("Stop background service?",
                             "Stopping the service pauses keyboard effects, automation rules "
                             "and background update checks. To stop only keyboard effects, "
                             "choose Off and Apply.", "Stop", go, destructive=True)
        else:
            go()
