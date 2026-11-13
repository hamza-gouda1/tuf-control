# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""StatGrid.set(): an empty tooltip string clears the cell tooltip, None is no update."""
import unittest

from asusius.ui.pages.monitor_base import StatGrid


class StatGridTooltipTests(unittest.TestCase):
    def test_empty_tooltip_clears_and_none_is_no_update(self):
        sg = StatGrid()
        sg.add("fan", "Fan")
        box = sg.boxes["fan"]
        sg.set("fan", "500 rpm", "tip text")
        self.assertEqual(box.get_tooltip_text(), "tip text")
        sg.set("fan", "600 rpm", "")
        self.assertIsNone(box.get_tooltip_text())
        sg.set("fan", "700 rpm", None)
        self.assertIsNone(box.get_tooltip_text())

    def test_setting_a_tooltip_again_works_after_a_clear(self):
        sg = StatGrid()
        sg.add("fan", "Fan")
        box = sg.boxes["fan"]
        sg.set("fan", "500 rpm", "")
        self.assertIsNone(box.get_tooltip_text())
        sg.set("fan", "800 rpm", "NVML fan")
        self.assertEqual(box.get_tooltip_text(), "NVML fan")


if __name__ == "__main__":
    unittest.main()
