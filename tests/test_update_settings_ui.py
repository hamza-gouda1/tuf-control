# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Background-check UI persistence without constructing GTK widgets."""
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
from types import SimpleNamespace

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tuf_control.ui.pages.updates import UpdatesPage, _write_background_checks  # noqa: E402


class BackgroundChecksSettingsTests(unittest.TestCase):
    def test_switch_changes_wait_for_atomic_worker_and_restore_on_failure(self):
        page = SimpleNamespace(_syncing_auto=False, _render_auto=Mock(), win=Mock())
        switch = Mock()
        switch.get_active.return_value = True
        with patch("tuf_control.ui.pages.updates.run_bg") as run:
            UpdatesPage._background_checks_changed(page, switch, None)
        switch.set_sensitive.assert_called_once_with(False)
        self.assertEqual(run.call_args.args, (_write_background_checks, True))
        run.call_args.kwargs["done"]({})
        self.assertEqual(switch.set_sensitive.call_args.args, (True,))
        page._render_auto.assert_called_once_with()
        failure = RuntimeError("cannot save settings")
        run.call_args.kwargs["error"](failure)
        page.win.toast_error.assert_called_once_with(failure)

    def test_loading_settings_never_writes_them(self):
        page = SimpleNamespace(_syncing_auto=True)
        with patch("tuf_control.ui.pages.updates.run_bg") as run:
            UpdatesPage._background_checks_changed(page, Mock(), None)
        run.assert_not_called()

    def test_enabling_updates_only_the_enabled_flag_via_injected_updater(self):
        config = {"updates": {"enabled": False, "interval_h": 12,
                              "respect_metered": False},
                  "automation": {"enabled": True}}
        original = deepcopy(config)

        def fake_update(edit):
            edit(config)
            return config

        result = _write_background_checks(True, updater=fake_update)

        self.assertIs(result, config)
        self.assertTrue(config["updates"]["enabled"])
        self.assertEqual(config["updates"]["interval_h"], 12)
        self.assertFalse(config["updates"]["respect_metered"])
        self.assertEqual(config["automation"], original["automation"])

    def test_disabling_calls_atomic_update_collaborator_with_only_the_flag_edit(self):
        updater = Mock(return_value={"updates": {"enabled": False}})

        result = _write_background_checks(False, updater=updater)

        updater.assert_called_once()
        edited = {"updates": {"enabled": True, "interval_h": 6}}
        updater.call_args.args[0](edited)
        self.assertEqual(edited, {"updates": {"enabled": False, "interval_h": 6}})
        self.assertEqual(result, {"updates": {"enabled": False}})


if __name__ == "__main__":
    unittest.main()
