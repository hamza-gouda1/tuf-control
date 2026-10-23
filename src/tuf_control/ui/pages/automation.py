"""Automation: the tuf-controld service, rules (trigger -> actions) and recent activity."""
from __future__ import annotations

import copy

from gi.repository import Adw, Gdk, GLib, Gtk

from ...automation.config import load_config, update_config
from ...automation.forms import (
    ACTIONS,
    BACKLIGHT,
    PPD,
    PROFILES,
    TRIGGERS,
    build_trigger,
    default_action,
    log_line,
    preset_rules,
    trigger_icon,
    trigger_key,
    unique_id,
)
from ...automation.rules import DAYS, Rule
from ...automation.service import DaemonClient
from ...effects.colour import hex_to_rgb, rgb_to_hex
from ...effects.registry import EFFECTS
from ..tasks import run_bg
from .base import Page, button, info_row, pill, prefs_page

STATUS_EVERY = 2  # telemetry ticks (seconds) between daemon status reads
esc = GLib.markup_escape_text


def _rgba(rgb) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0
    return c


def _combo(title: str, items: list[str], selected: int = 0, subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new(items))
    row.set_selected(max(0, selected))
    return row


def _spin(title: str, lo: float, hi: float, value: float, step: float = 1,
          subtitle: str = "") -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(lo, hi, step)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_value(value)
    return row


class AutomationPage(Page):
    key, title = "automation", "Automation"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.client = DaemonClient()
        self.status: dict = {"running": False}
        self.cfg: dict | None = None
        self.rules: list[Rule] = []
        self._ticks = 0
        self._quiet = False
        self.last_dialog: RuleDialog | None = None

        # service
        self.svc = Adw.PreferencesGroup(
            title="Service", description="tuf-controld applies your rules and keyboard effects "
                                         "in the background, as you. No root needed.")
        self.svc_row = Adw.ActionRow(title="tuf-controld", subtitle="Checking…")
        self.svc_row.add_prefix(Gtk.Image.new_from_icon_name("system-run-symbolic"))
        self.svc_pill = pill("Stopped")
        self.svc_btn = button("Start", "suggested-action", on_click=self._toggle_service)
        self.svc_row.add_suffix(self.svc_pill)
        self.svc_row.add_suffix(self.svc_btn)
        self.enabled_row = Adw.SwitchRow(title="Run rules",
                                         subtitle="Off pauses every rule; effects keep running")
        self.enabled_row.connect("notify::active", self._set_flag, "enabled")
        self.dry_row = Adw.SwitchRow(title="Dry run",
                                     subtitle="Log what rules would do without changing anything")
        self.dry_row.connect("notify::active", self._set_flag, "dry_run")
        self.cfg_err = info_row("Config file has an error", "", "dialog-warning-symbolic")
        self.cfg_err.set_visible(False)
        for r in (self.svc_row, self.enabled_row, self.dry_row, self.cfg_err):
            self.svc.add(r)

        # rules
        self.rules_g = Adw.PreferencesGroup(
            title="Rules", description="When a trigger starts its actions apply; when it ends "
                                       "the previous values return. Higher priority wins.")
        self.rules_g.set_header_suffix(button("Add rule", "flat", icon="list-add-symbolic",
                                              on_click=lambda: self.open_editor(None)))
        self.empty = info_row("No rules yet", "Add one, or start from a suggestion below.",
                              "media-playlist-repeat-symbolic")
        self.rules_g.add(self.empty)
        self.rule_rows: list[Gtk.Widget] = []

        self.sugg = Adw.PreferencesGroup(title="Suggestions")
        self.sugg_rows: list[Gtk.Widget] = []

        self.activity = Adw.PreferencesGroup(title="Recent activity",
                                             description="What the service did, newest first.")
        self.act_rows: list[Gtk.Widget] = []
        self.act_empty = info_row("Nothing yet", "Start the service to run rules.",
                                  "document-open-recent-symbolic")
        self.activity.add(self.act_empty)

        self.set_child(prefs_page(self.svc, self.rules_g, self.sugg, self.activity))

    # -- data --------------------------------------------------------------
    def refresh(self) -> None:
        run_bg(load_config, done=self._got_config, error=self._config_failed)
        self.poll_status()

    def poll_status(self) -> None:
        run_bg(self.client.status, done=self._got_status, error=lambda e: None)

    def on_telemetry(self, t) -> None:
        if not self.loaded:
            return
        self._ticks += 1
        if self._ticks % STATUS_EVERY == 0:
            self.poll_status()

    def _config_failed(self, exc) -> None:
        self.cfg_err.set_subtitle(str(exc))
        self.cfg_err.set_visible(True)

    def _got_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.cfg_err.set_visible(False)
        self.rules = [Rule.from_dict(r) for r in cfg["automation"]["rules"]]
        self._quiet = True
        self.enabled_row.set_active(cfg["automation"]["enabled"])
        self.dry_row.set_active(cfg["automation"]["dry_run"])
        self._quiet = False
        self._build_rules()
        self._build_suggestions()

    def _got_status(self, st: dict) -> None:
        self.status = st
        running = st.get("running")
        self.svc_pill.set_label("Running" if running else "Stopped")
        if running:
            self.svc_pill.add_css_class("accent")
        else:
            self.svc_pill.remove_css_class("accent")
        self.svc_btn.set_label("Stop" if running else "Start")
        if running:
            self.svc_btn.remove_css_class("suggested-action")
        else:
            self.svc_btn.add_css_class("suggested-action")
        if running:
            n = len(st.get("active_rules") or [])
            sub = f"Running · {n} rule{'s' if n != 1 else ''} active"
            if st.get("dry_run"):
                sub += " · dry run"
            if st.get("fake"):
                sub += " · fake hardware"
        else:
            sub = "Stopped. Rules and software effects are paused."
        self.svc_row.set_subtitle(sub)
        if st.get("config_error"):
            self.cfg_err.set_subtitle(f"The service kept the last good config. "
                                      f"{st['config_error']}")
            self.cfg_err.set_visible(True)
        self._mark_active()
        self._build_activity()

    # -- rows --------------------------------------------------------------
    def _active_ids(self) -> set[str]:
        return set(self.status.get("active_rules") or []) if self.status.get("running") else set()

    def _build_rules(self) -> None:
        for r in self.rule_rows:
            self.rules_g.remove(r)
        self.rule_rows = []
        self.empty.set_visible(not self.rules)
        for rule in self.rules:
            sub = rule.describe()
            if rule.priority != 50:
                sub += f" · priority {rule.priority}"
            row = Adw.ActionRow(title=esc(rule.name), subtitle=esc(sub), activatable=True)
            row.set_subtitle_lines(2)
            row.add_prefix(Gtk.Image.new_from_icon_name(trigger_icon(rule.trigger.to_dict())))
            badge = pill("Active", "accent")
            badge.set_visible(False)
            row.add_suffix(badge)
            sw = Gtk.Switch(active=rule.enabled, valign=Gtk.Align.CENTER,
                            tooltip_text="Enable rule")
            sw.connect("notify::active", self._toggle_rule, rule.id)
            row.add_suffix(sw)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, rid=rule.id: self.open_editor(rid))
            row.rule_id, row.badge = rule.id, badge
            self.rules_g.add(row)
            self.rule_rows.append(row)
        self._mark_active()

    def _mark_active(self) -> None:
        active = self._active_ids()
        for row in self.rule_rows:
            row.badge.set_visible(row.rule_id in active)

    def _build_suggestions(self) -> None:
        for r in self.sugg_rows:
            self.sugg.remove(r)
        self.sugg_rows = []
        presets = preset_rules(self.rules)[:3]
        self.sugg.set_visible(bool(presets))
        for p in presets:
            rule = Rule.from_dict(p)
            row = Adw.ActionRow(title=esc(rule.name), subtitle=esc(rule.describe()))
            row.set_subtitle_lines(2)
            row.add_prefix(Gtk.Image.new_from_icon_name(trigger_icon(p["trigger"])))
            row.add_suffix(button("Add", "flat", on_click=lambda p=p: self._add_preset(p)))
            self.sugg.add(row)
            self.sugg_rows.append(row)

    def _build_activity(self) -> None:
        for r in self.act_rows:
            self.activity.remove(r)
        self.act_rows = []
        log = [e for e in (self.status.get("log") or []) if e.get("kind") != "conflict"]
        self.act_empty.set_visible(not log)
        icons = {"error": "dialog-warning-symbolic", "restore": "edit-undo-symbolic",
                 "activate": "media-playback-start-symbolic",
                 "deactivate": "media-playback-stop-symbolic"}
        names = {r.id: r.name for r in self.rules}
        for e in reversed(log[-6:]):
            title, sub = log_line(e, names)
            row = Adw.ActionRow(title=esc(title), subtitle=esc(sub))
            row.add_prefix(Gtk.Image.new_from_icon_name(icons.get(e.get("kind"),
                                                                  "emblem-ok-symbolic")))
            self.activity.add(row)
            self.act_rows.append(row)

    # -- edits --------------------------------------------------------------
    def _write(self, fn, ok: str, after=None) -> None:
        def done(cfg):
            self._got_config(cfg)
            if ok:
                self.win.toast(ok)
            if after:
                after()
        run_bg(update_config, fn, done=done, error=self.win.toast_error)

    def _set_flag(self, row, _pspec, key: str) -> None:
        if self._quiet:
            return
        val = row.get_active()
        self._write(lambda c: c["automation"].update({key: val}), "")

    def _toggle_rule(self, sw, _pspec, rid: str) -> None:
        on = sw.get_active()

        def edit(c):
            for r in c["automation"]["rules"]:
                if r["id"] == rid:
                    r["enabled"] = on
        self._write(edit, "")

    def _add_preset(self, preset: dict) -> None:
        d = copy.deepcopy(preset)
        d["id"] = unique_id(d["name"], {r.id for r in self.rules})
        self._write(lambda c: c["automation"]["rules"].append(d), f"Added “{d['name']}”")

    def save_rule(self, data: dict, old_id: str | None) -> None:
        def edit(c):
            rules = c["automation"]["rules"]
            idx = next((i for i, r in enumerate(rules) if r["id"] == old_id), None)
            if idx is None:
                rules.append(data)
            else:
                rules[idx] = data
        self._write(edit, f"Saved “{data['name']}”" + self._hint())

    def delete_rule(self, rid: str) -> None:
        name = next((r.name for r in self.rules if r.id == rid), rid)

        def edit(c):
            c["automation"]["rules"] = [r for r in c["automation"]["rules"] if r["id"] != rid]
        self._write(edit, f"Deleted “{name}”")

    def _hint(self) -> str:
        return "" if self.status.get("running") else " · start the service to run it"

    def _toggle_service(self) -> None:
        running = self.status.get("running")
        self.svc_btn.set_sensitive(False)

        def done(msg):
            self.svc_btn.set_sensitive(True)
            self.win.toast("Service stopped" if running else "Service started")
            GLib.timeout_add(700, lambda: (self.poll_status(), False)[1])

        def fail(exc):
            self.svc_btn.set_sensitive(True)
            self.win.toast_error(exc)
        run_bg(self.client.stop if running else self.client.start, done=done, error=fail)

    def open_editor(self, rid: str | None) -> RuleDialog:
        rule = next((r for r in self.rules if r.id == rid), None)
        dlg = RuleDialog(self, rule)
        dlg.present(self.win)
        self.last_dialog = dlg
        return dlg


class RuleDialog(Adw.Dialog):
    def __init__(self, page: AutomationPage, rule: Rule | None) -> None:
        super().__init__(title="Edit rule" if rule else "New rule", content_width=560,
                         content_height=720)
        self.page, self.rule = page, rule
        self.ctl = page.ctl
        d = rule.to_dict() if rule else {
            "name": "", "priority": 50, "restore": True, "debounce_s": 2.0,
            "trigger": {"type": "app", "names": []}, "actions": []}
        self.extra_actions = [a for a in d["actions"] if a["type"] not in ACTIONS]

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self._save())
        hb.pack_end(self.save_btn)
        tv.add_top_bar(hb)
        self.prefs = Adw.PreferencesPage()
        tv.set_content(self.prefs)
        self.set_child(tv)

        # -- rule ------------------------------------------------------------------------------
        g = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title="Name", text=d["name"])
        self.name.connect("changed", lambda *_: self._validate())
        self.prio = _spin("Priority", 0, 100, d["priority"], 5,
                          "Wins over lower-priority rules that change the same setting")
        self.debounce = _spin("Wait before acting", 0, 600, d["debounce_s"], 1,
                              "Seconds the trigger must hold, both ways")
        self.restore = Adw.SwitchRow(title="Restore when it ends", active=d["restore"],
                                     subtitle="Put back what was set before the rule started")
        for r in (self.name, self.prio, self.debounce, self.restore):
            g.add(r)
        self.prefs.add(g)

        # -- trigger ----------------------------------------------------------------------------
        trig = d["trigger"]
        self.when = Adw.PreferencesGroup(title="When")
        keys = [k for k, _t, _i in TRIGGERS]
        self.trig_row = _combo("Trigger", [t for _k, t, _i in TRIGGERS],
                               keys.index(trigger_key(trig)))
        self.trig_row.connect("notify::selected", lambda *_: self._trigger_changed())
        self.when.add(self.trig_row)
        below = "above" not in trig
        self.lvl_dir = _combo("Condition", ["At or below", "At or above"], 0 if below else 1)
        self.lvl = _spin("Battery level", 1, 99, trig.get("below", trig.get("above", 20)), 1,
                         "Percent, with 3% hysteresis so it does not flap")
        self.apps = Adw.EntryRow(title="Programs, comma-separated",
                                 text=", ".join(trig.get("names", [])))
        self.apps.connect("changed", lambda *_: self._validate())
        self.apps_hint = info_row(
            "Matched against running processes",
            "Executable names (steam, gamescope, Hades.exe) or desktop ids "
            "(com.valvesoftware.Steam). Window focus is not visible to apps on GNOME Wayland.")
        self.days_row = Adw.ActionRow(title="Days")
        days_box = Gtk.Box(valign=Gtk.Align.CENTER)
        days_box.add_css_class("linked")
        self.day_btns = {}
        active_days = trig.get("days", list(DAYS))
        for day in DAYS:
            b = Gtk.ToggleButton(label=day[:2].capitalize(), active=day in active_days)
            b.connect("toggled", lambda *_: self._validate())
            days_box.append(b)
            self.day_btns[day] = b
        self.days_row.add_suffix(days_box)
        self.start = Adw.EntryRow(title="From (HH:MM)", text=trig.get("start", "22:00"))
        self.end = Adw.EntryRow(title="Until (HH:MM)", text=trig.get("end", "07:00"))
        for e in (self.start, self.end):
            e.connect("changed", lambda *_: self._validate())
        self.prof_trig = _combo("Profile", [p.capitalize() for p in PROFILES],
                                PROFILES.index(trig.get("value", "performance"))
                                if trig.get("value") in PROFILES else 2)
        self.trig_rows = {"battery": [self.lvl_dir, self.lvl],
                          "app": [self.apps, self.apps_hint],
                          "schedule": [self.days_row, self.start, self.end],
                          "profile": [self.prof_trig]}
        for rows in self.trig_rows.values():
            for r in rows:
                self.when.add(r)
        self.prefs.add(self.when)

        # -- actions -----------------------------------------------------------------------------
        self.then = Adw.PreferencesGroup(title="Then",
                                         description="Switch on what the rule should change.")
        have = {a["type"]: a for a in d["actions"]}
        self.act_rows: dict[str, Adw.ExpanderRow] = {}
        self.act_get = {}
        for kind, (title, sub) in ACTIONS.items():
            a = have.get(kind) or default_action(kind)
            exp = Adw.ExpanderRow(title=title, subtitle=sub, show_enable_switch=True,
                                  enable_expansion=kind in have, expanded=False)
            exp.connect("notify::enable-expansion", lambda *_: self._validate())
            row, getter = self._action_editor(kind, a)
            exp.add_row(row)
            self.then.add(exp)
            self.act_rows[kind], self.act_get[kind] = exp, getter
        self.prefs.add(self.then)

        # -- test ---------------------------------------------------------------------------------
        self.test_g = Adw.PreferencesGroup(title="Check")
        self.err = info_row("Not valid yet", "", "dialog-warning-symbolic")
        self.err.set_visible(False)
        self.test_g.add(self.err)
        trow = Adw.ActionRow(title="Test with a dry run",
                             subtitle="Each action goes through the Controller's checks; "
                                      "nothing is changed")
        trow.add_prefix(Gtk.Image.new_from_icon_name("system-run-symbolic"))
        self.test_btn = button("Test", on_click=self.run_test)
        trow.add_suffix(self.test_btn)
        self.test_g.add(trow)
        self.test_rows: list[Gtk.Widget] = []
        self.prefs.add(self.test_g)

        if rule:
            g_del = Adw.PreferencesGroup()
            drow = Adw.ActionRow(title="Delete this rule")
            db = button("Delete", "destructive-action", on_click=self._delete)
            drow.add_suffix(db)
            g_del.add(drow)
            self.prefs.add(g_del)

        self._trigger_changed()

    # -- editors --------------------------------------------------------------
    def _action_editor(self, kind: str, a: dict):
        if kind == "profile":
            row = _combo("Set to", [p.capitalize() for p in PROFILES],
                         PROFILES.index(a["value"]))
            return row, lambda: {"type": kind, "value": PROFILES[row.get_selected()]}
        if kind == "ppd":
            row = _combo("Set to", PPD, PPD.index(a["value"]) if a["value"] in PPD else 1)
            return row, lambda: {"type": kind, "value": PPD[row.get_selected()]}
        if kind == "kbd_colour":
            row = Adw.ActionRow(title="Colour")
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                        valign=Gtk.Align.CENTER)
            btn.set_rgba(_rgba(hex_to_rgb(a["value"])))
            row.add_suffix(btn)

            def get():
                c = btn.get_rgba()
                return {"type": kind, "value": rgb_to_hex(
                    (round(c.red * 255), round(c.green * 255), round(c.blue * 255)))}
            return row, get
        if kind == "kbd_brightness":
            row = _combo("Level", BACKLIGHT, a["value"])
            return row, lambda: {"type": kind, "value": row.get_selected()}
        if kind == "charge_limit":
            row = _spin("Stop at (%)", 20, 100, a["value"], 5)
            return row, lambda: {"type": kind, "value": int(row.get_value())}
        if kind == "panel_overdrive":
            row = Adw.SwitchRow(title="Overdrive on", active=a["value"])
            return row, lambda: {"type": kind, "value": row.get_active()}
        names = list(EFFECTS)
        titles = ["Off (hardware mode)"] + [EFFECTS[n].title for n in names]
        cur = a.get("name")
        row = _combo("Effect", titles, names.index(cur) + 1 if cur in names else 0)
        params = a.get("params") or {}

        def get_fx():
            i = row.get_selected()
            name = names[i - 1] if i > 0 else None
            return {"type": kind, "name": name,
                    "params": params if name == cur else {}}
        return row, get_fx

    def _trigger_changed(self) -> None:
        key = TRIGGERS[self.trig_row.get_selected()][0]
        for k, rows in self.trig_rows.items():
            for r in rows:
                r.set_visible(k == key)
        self._validate()

    # -- data --------------------------------------------------------------
    def collect(self) -> dict:
        key = TRIGGERS[self.trig_row.get_selected()][0]
        trig = build_trigger(
            key, level=int(self.lvl.get_value()), below=self.lvl_dir.get_selected() == 0,
            apps=self.apps.get_text(), days=[d for d, b in self.day_btns.items() if b.get_active()],
            start=self.start.get_text(), end=self.end.get_text(),
            profile=PROFILES[self.prof_trig.get_selected()])
        actions = [self.act_get[k]() for k, exp in self.act_rows.items()
                   if exp.get_enable_expansion()] + self.extra_actions
        name = self.name.get_text().strip() or "Untitled rule"
        taken = {r.id for r in self.page.rules if not self.rule or r.id != self.rule.id}
        rid = self.rule.id if self.rule else unique_id(name, taken)
        return {"id": rid, "name": name, "enabled": self.rule.enabled if self.rule else True,
                "priority": int(self.prio.get_value()), "restore": self.restore.get_active(),
                "debounce_s": float(self.debounce.get_value()), "trigger": trig,
                "actions": actions}

    def _validate(self) -> Rule | None:
        try:
            rule = Rule.from_dict(self.collect())
        except Exception as exc:
            msg = str(exc).split(": ", 1)[-1]
            self.err.set_subtitle(msg[:1].upper() + msg[1:])
            self.err.set_visible(True)
            self.save_btn.set_sensitive(False)
            self.test_btn.set_sensitive(False)
            return None
        self.err.set_visible(False)
        self.save_btn.set_sensitive(True)
        self.test_btn.set_sensitive(True)
        return rule

    def _save(self) -> None:
        rule = self._validate()
        if rule is None:
            return
        self.page.save_rule(rule.to_dict(), self.rule.id if self.rule else None)
        self.close()

    def _delete(self) -> None:
        if self.rule:
            self.page.delete_rule(self.rule.id)
        self.close()

    def run_test(self) -> None:
        rule = self._validate()
        if rule is None:
            return
        from ...automation.actions import ControllerSink
        from ...automation.engine import evaluate, test_rule
        from ...automation.sources import StateCollector
        ctl = self.ctl

        def work():
            st = StateCollector(ctl).collect()
            return evaluate([rule], st)["matched"], test_rule(rule, ControllerSink(ctl))

        self.test_btn.set_sensitive(False)
        run_bg(work, done=self._show_test, error=self._test_failed)

    def _clear_test(self) -> None:
        for r in self.test_rows:
            self.test_g.remove(r)
        self.test_rows = []

    def _test_failed(self, exc) -> None:
        self.test_btn.set_sensitive(True)
        self._clear_test()
        r = info_row("Test failed", str(exc), "dialog-warning-symbolic")
        self.test_g.add(r)
        self.test_rows.append(r)

    def _show_test(self, res) -> None:
        matched, results = res
        self.test_btn.set_sensitive(True)
        self._clear_test()
        head = Adw.ActionRow(title="Matches right now" if matched else "Not matching right now",
                             subtitle="Its actions would apply now" if matched else
                             "Its actions would apply when the trigger starts")
        head.add_prefix(Gtk.Image.new_from_icon_name(
            "emblem-ok-symbolic" if matched else "media-playback-pause-symbolic"))
        rows = [head]
        for r in results:
            detail = r["detail"][:1].upper() + r["detail"][1:]
            row = Adw.ActionRow(title=esc(r["action"]), subtitle=esc(detail))
            row.set_subtitle_lines(2)
            row.add_suffix(pill("OK", "accent") if r["ok"] else pill("Error"))
            rows.append(row)
        for r in rows:
            self.test_g.add(r)
            self.test_rows.append(r)

