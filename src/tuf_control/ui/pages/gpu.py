# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

from gi.repository import Adw, Gtk

from ...core import GpuAction
from ..tasks import run_bg
from ..viewmodels import (
    DGPU_LOST_TEXT,
    DGPU_POWER_TEXT,
    GPU_MODE_INFO,
    GPU_UI_ORDER,
    fmt_num,
    gpu_action_text,
    gpu_switch_requirement,
)
from .base import Page, a11y_name, button, info_row, pill, prefs_page, value_row


class GpuPage(Page):
    key, title = "gpu", "GPU mode"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.status = None
        self.banner = Adw.Banner(revealed=False)
        self.modes = Adw.PreferencesGroup(
            title="GPU mode", description="Handled by supergfxd. Some switches need a restart.")
        self.switch_btn = button("Switch…", "suggested-action", on_click=self._switch)
        self.switch_btn.set_sensitive(False)
        self.modes.set_header_suffix(self.switch_btn)
        self.mode_rows: list = []
        self.checks = {}
        self.unavail = info_row("GPU switching unavailable", "")
        self.unavail.set_visible(False)
        self.modes.add(self.unavail)

        self.dgpu = Adw.PreferencesGroup(title="Discrete GPU")
        self.power_row, self.power_val = value_row("State", numeric=False)
        self.temp_row, self.temp_val = value_row("Temperature")
        self.draw_row, self.draw_val = value_row("Power draw")
        for r in (self.power_row, self.temp_row, self.draw_row):
            self.dgpu.add(r)
        self.dgpu.add(info_row("Battery tip", "A sleeping dGPU uses almost no power. TUF Control "
                               "only reads its sensors while it is awake.",
                               "battery-level-80-symbolic"))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        page = prefs_page(self.modes, self.dgpu)
        page.set_vexpand(True)
        box.append(page)
        self.set_child(box)

    def refresh(self) -> None:
        run_bg(lambda: self.ctl.get_gpu_status()[0], done=self._show, error=self._failed)

    def _failed(self, exc) -> None:
        self.unavail.set_subtitle(
            "Not available right now: GPU switching could not be read. Check that the "
            "supergfxd service is running, then restart TUF Control.")
        self.unavail.set_visible(True)
        self.unavail.set_tooltip_text(str(exc))
        for r in self.mode_rows:
            r.set_visible(False)
        self.switch_btn.set_sensitive(False)

    def _show(self, st) -> None:
        self.status = st
        self.unavail.set_visible(False)
        for r in self.mode_rows:
            self.modes.remove(r)
        self.mode_rows, self.checks = [], {}
        group = None
        modes = [m for m in GPU_UI_ORDER if m in st.supported] or [st.mode]
        for mode in modes:
            title, sub = GPU_MODE_INFO[mode]
            row = Adw.ActionRow(title=title, subtitle=sub)
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            a11y_name(chk, title)
            if group:
                chk.set_group(group)
            group = group or chk
            chk.set_active(mode is (st.pending_mode or st.mode))
            chk.connect("toggled", lambda *_: self._sync())
            row.add_prefix(chk)
            row.set_activatable_widget(chk)
            if mode is st.mode:
                row.add_suffix(pill("Current", "accent"))
            elif mode is st.pending_mode:
                row.add_suffix(pill("After restart" if st.pending_action is GpuAction.REBOOT
                                    else "Pending", "gold"))
            self.checks[mode] = chk
            self.modes.add(row)
            self.mode_rows.append(row)
        text = gpu_action_text(st.pending_action, st.pending_mode)
        self.banner.set_title(text + " · save your work first" if text else "")
        self.banner.set_revealed(bool(text))
        self.power_val.set_label(DGPU_POWER_TEXT.get(st.dgpu_power or "", st.dgpu_power or "–"))
        self._sync()

    def _target(self):
        return next((m for m, c in self.checks.items() if c.get_active()), None)

    def _sync(self):
        st, target = self.status, self._target()
        self.switch_btn.set_sensitive(bool(st and target is not None and
                                           target is not (st.pending_mode or st.mode)))

    def _switch(self):
        st, target = self.status, self._target()
        if not st or target is None:
            return
        need = gpu_switch_requirement(st.mode, target)
        name = GPU_MODE_INFO[target][0]
        if need == "reboot":
            body = (f"{name} changes the display wiring (MUX). It takes effect only after a "
                    "restart. Save your work first.")
            if target.name == "ASUS_MUX_DGPU":
                body += " The dGPU stays on in this mode, so battery life drops."
        elif need == "logout":
            body = (f"Switching to {name} may require logging out. Apps using the dGPU "
                    "will be closed.")
        else:
            body = f"Cancel the pending switch and stay on {name}."

        def go():
            def done(res):
                action = res.value if res is not None else None
                msg = gpu_action_text(action, target) or f"Switched to {name}"
                self.win.toast(msg)
                self.refresh()
            run_bg(lambda: self.ctl.set_gpu_mode(target), done=done,
                   error=lambda e: (self.win.toast_error(e), self.refresh()))

        self.win.confirm(f"Switch to {name}?", body,
                         "Switch and restart later" if need == "reboot" else "Switch", go,
                         destructive=need == "reboot")

    def on_telemetry(self, t) -> None:
        asleep = t.gpu_runtime not in (None, "active")
        self.temp_val.set_label("–" if asleep else f"{fmt_num(t.gpu_temp_c)} °C")
        self.draw_val.set_label("–" if asleep else f"{fmt_num(t.gpu_power_w, 1)} W")
        self.power_row.set_subtitle(DGPU_LOST_TEXT if t.gpu_runtime == "error" else "")
        if t.gpu_runtime:
            self.power_val.set_label(DGPU_POWER_TEXT.get(t.gpu_runtime, t.gpu_runtime))
