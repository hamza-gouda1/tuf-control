# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""UI action routing with fake collaborators; no widgets or display are created."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tuf_control.ui.pages.automation import RuleDialog  # noqa: E402
from tuf_control.ui.pages.keyboard_effects import EffectsSection  # noqa: E402


class ActionSafetyTests(unittest.TestCase):
    def test_effect_picker_tooltip_tracks_the_full_selected_title(self):
        section = Mock(selected="temperature")
        section.param_rows = []
        EffectsSection._picked(section)
        section.pick.set_tooltip_text.assert_called_with("Temperature")
        section.selected = None
        EffectsSection._picked(section)
        section.pick.set_tooltip_text.assert_called_with("Off")

    def section(self, running):
        return SimpleNamespace(status={"running": running}, client=Mock(), win=Mock())

    def test_stopping_service_waits_for_confirmation(self):
        section = self.section(True)
        with patch("tuf_control.ui.pages.keyboard_effects.run_bg") as run:
            EffectsSection._toggle_service(section)
            run.assert_not_called()  # cancelling leaves effects, rules and updates running
            heading, body, action, on_yes = section.win.confirm.call_args.args
            self.assertEqual(heading, "Stop background service?")
            self.assertIn("keyboard effects", body)
            self.assertIn("automation rules", body)
            self.assertIn("update checks", body)
            self.assertEqual(action, "Stop")
            self.assertTrue(section.win.confirm.call_args.kwargs["destructive"])
            on_yes()
            self.assertEqual(run.call_args.args, (section.client.stop,))

    def test_starting_service_is_immediate(self):
        section = self.section(False)
        with patch("tuf_control.ui.pages.keyboard_effects.run_bg") as run:
            EffectsSection._toggle_service(section)
        section.win.confirm.assert_not_called()
        self.assertEqual(run.call_args.args, (section.client.start,))

    def test_every_service_status_explains_shared_scope(self):
        states = [
            {"running": False},
            {"running": True},
            {"running": True, "effects": {"error": "backend unavailable"}},
            {"running": True, "effects": {"waiting": "permission", "effect": "ambient"}},
            {"running": True, "effects": {"running": True, "effect": "accent"}},
            {"running": True, "effects": {"running": True, "effect": "gradient",
                                           "override": True, "backlight_off": True}},
        ]
        for state in states:
            with self.subTest(state=state):
                section = SimpleNamespace(svc_pill=Mock(), svc_btn=Mock(), svc_row=Mock(),
                                          win=Mock(), _last_error=None,
                                          page=SimpleNamespace(form=SimpleNamespace(blend=False),
                                                               describe=Mock()))
                EffectsSection._got_status(section, state)
                subtitle = section.svc_row.set_subtitle.call_args.args[0]
                self.assertIn("automation rules", subtitle)
                self.assertIn("update checks", subtitle)

    def test_deleting_rule_waits_for_confirmation_and_then_closes(self):
        rule = SimpleNamespace(id="game", name="Game lighting")
        dialog = SimpleNamespace(rule=rule, page=Mock(), close=Mock())
        RuleDialog._delete(dialog)
        dialog.page.delete_rule.assert_not_called()
        dialog.close.assert_not_called()
        heading, body, action, on_yes = dialog.page.win.confirm.call_args.args
        self.assertEqual((heading, action), ("Delete rule?", "Delete"))
        self.assertIn(rule.name, body)
        self.assertTrue(dialog.page.win.confirm.call_args.kwargs["destructive"])
        on_yes()
        dialog.page.delete_rule.assert_called_once_with(rule.id)
        dialog.close.assert_called_once_with()

    def test_new_rule_cannot_be_deleted(self):
        dialog = SimpleNamespace(rule=None, page=Mock(), close=Mock())
        RuleDialog._delete(dialog)
        dialog.page.win.confirm.assert_not_called()
        dialog.page.delete_rule.assert_not_called()
        dialog.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
