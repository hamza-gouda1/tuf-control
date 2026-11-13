# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Agent access: who may use the MCP server, approvals waiting for you, and the audit log.

Built on first visit. While the page is visible it stat()s three small files every two
seconds (agent.json, the approval queue, the audit log) and reloads only what changed;
when it is hidden nothing runs.
"""
from __future__ import annotations

import json

from gi.repository import Adw, GLib, Gtk

from ...agent import policy, present
from ...agent.approvals import ApprovalQueue
from ...agent.audit import AuditLog
from ...agent.store import access_path
from ..tasks import run_bg
from ..widgets import label
from .base import Page, a11y_name, button, info_row, pill, prefs_page

esc = GLib.markup_escape_text
CHECK_EVERY = 2     # telemetry ticks between file checks while visible
FORCE_EVERY = 15    # ticks between full reloads (expiry, the countdown)
AUDIT_ROWS = 50
ROW_PX = 58         # one activity row (two lines of text and margins)


def _sig(path) -> tuple | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


class AgentAccessPage(Page):
    key, title = "agent", "Agent access"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.queue = ApprovalQueue()
        self.audit = AuditLog()
        self.acc: policy.Access | None = None
        self._quiet = False
        self._ticks = 0
        self._sigs: tuple = ()
        self._busy = False
        self.pending_rows: list[Gtk.Widget] = []
        self.last_dialog = None

        # access
        self.access_g = Adw.PreferencesGroup(
            title="Agent access",
            description="MCP clients and scripts run “asusius mcp” as you, with no extra "
                        "privileges. Reads are allowed by default; every change is a dry run "
                        "until you approve it, and everything is logged.")
        self.access_g.set_header_suffix(button("Revoke all", "destructive-action",
                                               on_click=self._revoke))
        self.master = Adw.SwitchRow(title="Allow agent access")
        self.master.add_prefix(Gtk.Image.new_from_icon_name("channel-secure-symbolic"))
        self.master.connect("notify::active", self._on_master)
        self.level = Adw.ComboRow(title="Access level", model=Gtk.StringList.new(
            [policy.LEVEL_TITLES[x] for x in present.PAGE_LEVELS]))
        self.level.set_subtitle_lines(0)
        self.level.connect("notify::selected", self._on_level)
        self.high = Adw.SwitchRow(title="High-risk tools",
                                  subtitle="Fan curves, GPU mode, power limits, restore "
                                           "defaults. Each call needs your approval.")
        self.high.connect("notify::active", self._on_high)
        for r in (self.master, self.level, self.high):
            self.access_g.add(r)

        # standing approval
        self.standing_g = Adw.PreferencesGroup(
            title="Approve automatically",
            description="For a while, basic changes from agents apply without asking. "
                        "High-risk tools always ask.")
        self.st_row = Adw.ActionRow(title="Standing approval", subtitle="")
        self.st_row.set_subtitle_lines(0)
        self.st_row.add_prefix(Gtk.Image.new_from_icon_name("alarm-symbolic"))
        self.st_dur = Gtk.DropDown(model=Gtk.StringList.new([d for d, _ in present.DURATIONS]),
                                   valign=Gtk.Align.CENTER, selected=1)
        a11y_name(self.st_dur, "Standing approval duration")
        self.st_btn = button("Allow", on_click=self._toggle_standing)
        self.st_row.add_suffix(self.st_dur)
        self.st_row.add_suffix(self.st_btn)
        self.standing_g.add(self.st_row)

        # pending
        self.pending_g = Adw.PreferencesGroup(
            title="Waiting for you",
            description="Requests show the exact values that would be sent. The reason is the "
                        "agent's own words.")
        self.pending_empty = info_row("Nothing waiting", "Requests from agents appear here and "
                                      "as a notification.", "emblem-ok-symbolic")
        self.pending_g.add(self.pending_empty)

        # activity
        self.activity_g = Adw.PreferencesGroup(title="Activity",
                                               description="The audit log, newest first.")
        self.activity = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.activity.add_css_class("boxed-list")
        self.activity_sw = Gtk.ScrolledWindow(child=self.activity, propagate_natural_height=True,
                                              max_content_height=5 * ROW_PX,
                                              hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.activity_g.add(self.activity_sw)
        self.activity_empty = info_row("No activity yet", "Connect a client to see its calls.",
                                       "document-open-recent-symbolic")
        self.activity_g.add(self.activity_empty)

        # connect
        self.connect_g = Adw.PreferencesGroup(title="Connect a client")
        cmd = Adw.ActionRow(title="Command", subtitle="asusius mcp")
        cmd.add_css_class("property")
        cmd.add_prefix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        cmd.add_suffix(button("Copy client config", "flat", icon="edit-copy-symbolic",
                              on_click=self._copy_config))
        self.connect_g.add(cmd)
        self.connect_g.add(info_row(
            "Speaks the Model Context Protocol over stdio",
            "Tools, resources and prompts are listed in docs/AGENT_API.md. Scripts can use "
            "“asusius agent …” and “--json” instead.", "dialog-information-symbolic"))

        self.set_child(prefs_page(self.access_g, self.pending_g, self.standing_g,
                                  self.activity_g, self.connect_g))

    # -- data --------------------------------------------------------------------------
    def on_shown(self) -> None:
        self.loaded = True
        self._sigs = ()
        self.refresh()

    def _paths(self):
        return (access_path(), self.queue.path, self.audit.path)

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True

        def load():
            sigs = tuple(_sig(p) for p in self._paths())
            return sigs, policy.load_access(), self.queue.pending(), self.audit.tail(AUDIT_ROWS)

        def fail(exc):
            self._busy = False
            self.win.toast_error(exc)
        run_bg(load, done=self._got, error=fail)

    def on_telemetry(self, _t) -> None:
        if not self.loaded or self.win.current != self.key:
            return  # hidden: nothing to do
        self._ticks += 1
        if self._ticks % FORCE_EVERY == 0:
            self._sigs = ()
        if self._ticks % CHECK_EVERY:
            return
        if tuple(_sig(p) for p in self._paths()) != self._sigs:
            self.refresh()

    def _got(self, res) -> None:
        self._busy = False
        sigs, acc, pending, audit = res
        self._sigs = sigs
        self.acc = acc
        self._quiet = True
        self.master.set_active(acc.enabled and not acc.error)
        self.master.set_subtitle(esc(present.master_subtitle(acc)))
        lvl = acc.level if acc.level in present.PAGE_LEVELS else "read"
        self.level.set_selected(present.PAGE_LEVELS.index(lvl))
        self.level.set_subtitle(esc(present.LEVEL_HELP[lvl]))
        self.level.set_tooltip_text(f"Access level: {policy.LEVEL_TITLES[lvl]}")
        self.high.set_active(acc.high_risk_tools)
        self._quiet = False
        on = acc.enabled and not acc.error
        self.level.set_sensitive(on)
        self.high.set_sensitive(on and acc.level == "control-all")
        g = acc.standing()
        self.st_row.set_subtitle(esc(present.standing_text(acc)))
        self.st_dur.set_visible(g is None)
        self.st_btn.set_label("End now" if g else "Allow")
        if g:
            self.st_btn.remove_css_class("suggested-action")
        self.st_row.set_sensitive(on)
        self._build_pending(pending)
        self._build_activity(audit)

    # -- rows ----------------------------------------------------------------------------
    def _build_pending(self, items: list[dict]) -> None:
        for r in self.pending_rows:
            self.pending_g.remove(r)
        self.pending_rows = []
        rows = present.pending_rows(items)
        self.pending_empty.set_visible(not rows)
        for p in rows:
            sub = p["subtitle"] + ("\n" + p["reason"] if p["reason"] else "")
            row = Adw.ActionRow(title=esc(p["title"]), subtitle=esc(sub))
            row.set_title_lines(0)
            row.set_subtitle_lines(0)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "dialog-warning-symbolic" if p["high"] else "dialog-question-symbolic"))
            row.add_suffix(button("Deny", "flat", on_click=lambda p=p: self._decide(p, False)))
            row.add_suffix(button("Approve once", "suggested-action",
                                  on_click=lambda p=p: self._approve(p)))
            self.pending_g.add(row)
            self.pending_rows.append(row)

    def _build_activity(self, entries: list[dict]) -> None:
        self.activity.remove_all()
        self.activity_empty.set_visible(not entries)
        self.activity_sw.set_visible(bool(entries))
        for e in reversed(entries[-AUDIT_ROWS:]):
            r = present.audit_row(e)
            box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12,
                          margin_end=12)
            box.append(label(r["time"], ("numeric", "dim-label"), width_chars=5))
            mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            what = label(r["what"], ellipsize=3, tooltip_text=r["what"])
            mid.append(what)
            sub = r["who"] + (" · " + r["detail"] if r["detail"] else "")
            mid.append(label(sub, ("caption", "dim-label"), ellipsize=3, tooltip_text=sub))
            box.append(mid)
            box.append(pill(r["label"], *((r["tone"],) if r["tone"] else ())))
            self.activity.append(box)
        # show up to five rows without scrolling, the rest (up to 50) scroll inside the card
        self.activity_sw.set_min_content_height(min(5, len(entries)) * ROW_PX)

    # -- actions ---------------------------------------------------------------------------
    def _owner(self, name: str, decision: str, params=None, rid=None) -> None:
        self.audit.write(client="owner:app", kind="owner", name=name, decision=decision,
                         params=params, request_id=rid)

    def _apply(self, fn, toast: str = "") -> None:
        def done(_res):
            if toast:
                self.win.toast(toast)
            self.refresh()

        def fail(exc):
            self.win.toast_error(exc)
            self.refresh()
        run_bg(fn, done=done, error=fail)

    def _on_master(self, row, _p) -> None:
        if self._quiet:
            return
        on = row.get_active()

        def work():
            if on and self.acc is not None and self.acc.error:
                policy.grant("read")  # a broken file is replaced by the read-only default
            else:
                policy.set_enabled(on)
            self._owner("access", "grant" if on else "revoke", {"enabled": on})
        self._apply(work, "Agent access on" if on else "Agent access off")

    def _on_level(self, row, _p) -> None:
        if self._quiet:
            return
        lvl = present.PAGE_LEVELS[row.get_selected()]

        def work():
            policy.grant(lvl)
            self._owner("level", "grant", {"level": lvl})
        self._apply(work, f"{policy.LEVEL_TITLES[lvl]} for agents")

    def _on_high(self, row, _p) -> None:
        if self._quiet:
            return
        on = row.get_active()

        def work():
            policy.set_high_risk(on)
            self._owner("high_risk_tools", "grant" if on else "revoke", {"enabled": on})
        self._apply(work, "High-risk tools on (approval per call)" if on
                    else "High-risk tools off")

    def _toggle_standing(self) -> None:
        if self.acc is not None and self.acc.standing():
            def end():
                policy.end_standing()
                self._owner("standing_grant", "revoke")
            self._apply(end, "Standing approval ended")
            return
        title, secs = present.DURATIONS[self.st_dur.get_selected()]
        lvl = "control-all" if self.acc and self.acc.level == "control-all" else "control-basic"

        def start():
            policy.grant(lvl, secs)
            self._owner("standing_grant", "grant", {"level": lvl, "for_s": secs})
        self._apply(start, f"Basic changes approved for {title}")

    def _revoke(self) -> None:
        def work():
            policy.revoke()
            n = self.queue.cancel_all(by="owner:app")
            self._owner("revoke", "revoke", {"cancelled": n})
            return n
        self._apply(work, "All agent access revoked")

    def _approve(self, p: dict) -> None:
        if not p["high"]:
            self._decide(p, True)
            return
        body = (f"{p['title']}\n\nExact values:\n"
                f"{json.dumps(p['params'], separators=(', ', ': '))}\n\n{p['subtitle']}")
        if p["reason"]:
            body += f"\n{p['reason']}"
        self.win.confirm("Approve a high-risk change?", body, "Approve once",
                         lambda: self._decide(p, True), destructive=True)
        self.last_dialog = getattr(self.win, "last_dialog", None)

    def _decide(self, p: dict, approve: bool) -> None:
        def work():
            e = self.queue.decide(p["id"], approve, by="owner:app")
            self._owner(e["tool"], "approve" if approve else "deny", e["params"], e["id"])
        self._apply(work, "Approved once: the agent can now apply it" if approve
                    else "Request denied")

    def _copy_config(self) -> None:
        text = present.config_snippet()
        try:
            self.get_clipboard().set(text)
        except (TypeError, AttributeError):
            from gi.repository import Gdk, GObject
            self.get_clipboard().set_content(
                Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))
        self.win.toast("Client config copied")

