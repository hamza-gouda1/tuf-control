# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""StatGrid.set(): an empty tooltip clears the cell tooltip, None is no update.

Borrows the unbound set() onto a double instead of constructing GTK widgets, so the
suite stays headless-safe on CI (the fan-curve canvas tests use the same pattern).
"""
import unittest
from unittest.mock import Mock

from asusius.ui.pages.monitor_base import StatGrid


class StatGridDouble:
    set = StatGrid.set

    def __init__(self):
        self.cells = {"fan": Mock()}
        self.boxes = {"fan": Mock()}


class StatGridTooltipTests(unittest.TestCase):
    def test_empty_tooltip_clears_and_none_is_no_update(self):
        sg = StatGridDouble()
        sg.set("fan", "500 rpm", "tip text")
        sg.boxes["fan"].set_tooltip_text.assert_called_once_with("tip text")
        sg.set("fan", "600 rpm", "")
        sg.boxes["fan"].set_tooltip_text.assert_called_with("")
        sg.set("fan", "700 rpm", None)
        sg.boxes["fan"].set_tooltip_text.assert_called_with("")  # None added no call

    def test_setting_a_tooltip_again_works_after_a_clear(self):
        sg = StatGridDouble()
        sg.set("fan", "500 rpm", "")
        sg.boxes["fan"].set_tooltip_text.assert_called_once_with("")
        sg.set("fan", "800 rpm", "NVML fan")
        sg.boxes["fan"].set_tooltip_text.assert_called_with("NVML fan")


if __name__ == "__main__":
    unittest.main()
