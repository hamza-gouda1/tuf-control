# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Canvas keyboard and accessibility callbacks without constructing GTK widgets."""
import unittest
from unittest.mock import Mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk  # noqa: E402

from asusius.backends.fake import DEFAULT_CURVES  # noqa: E402
from asusius.core import FanCurve  # noqa: E402
from asusius.ui.viewmodels import CurveGeometry, FanCurveModel, pwm_to_pct  # noqa: E402
from asusius.ui.widgets import FAN_CURVE_KEYBOARD_HELP, FanCurveCanvas  # noqa: E402


class CanvasDouble:
    set_model = FanCurveCanvas.set_model
    _announce = FanCurveCanvas._announce
    _key = FanCurveCanvas._key
    _begin = FanCurveCanvas._begin
    _update = FanCurveCanvas._update

    def __init__(self):
        self.model = None
        self.active = None
        self.on_change = Mock()
        self.on_select = Mock()
        self.queue_draw = Mock()
        self.update_property = Mock()
        self.grab_focus = Mock()
        self.set_cursor_from_name = Mock()
        self._hit = Mock(return_value=2)
        self.geometry = lambda: CurveGeometry(600, 300)


def curve_model():
    temps, pwms = DEFAULT_CURVES[1]
    return FanCurveModel(FanCurve.from_lists("CPU", list(temps), list(pwms)))


class FanCurveCanvasTests(unittest.TestCase):
    def setUp(self):
        self.canvas = CanvasDouble()
        self.model = curve_model()
        self.canvas.set_model(self.model)

    def assert_current_point(self, index):
        props, values = self.canvas.update_property.call_args.args
        accessible = dict(zip(props, values))
        temp, pwm = self.model.points[index]
        self.assertEqual(accessible[Gtk.AccessibleProperty.LABEL],
                         f"Fan curve, point {index + 1} of 8, {temp}°C at {pwm_to_pct(pwm):.0f}%")
        self.assertEqual(accessible[Gtk.AccessibleProperty.DESCRIPTION], FAN_CURVE_KEYBOARD_HELP)

    def key(self, key):
        return self.canvas._key(None, key, 0, 0)

    def test_loading_and_clearing_model_announces_state_without_an_edit(self):
        self.assert_current_point(0)
        self.canvas.set_model(None)
        props, values = self.canvas.update_property.call_args.args
        self.assertEqual(dict(zip(props, values))[Gtk.AccessibleProperty.LABEL],
                         "Fan curve, no curve available")
        self.canvas.on_change.assert_not_called()
        self.canvas.on_select.assert_not_called()

    def test_keyboard_selection_announces_every_point_without_dirtying_curve(self):
        for index in range(1, 8):
            self.assertTrue(self.key(Gdk.KEY_bracketright if index % 2 else Gdk.KEY_period))
            self.assert_current_point(index)
        self.assertTrue(self.key(Gdk.KEY_period))
        self.assertEqual(self.canvas.active, 7)
        for index in range(6, -1, -1):
            self.assertTrue(self.key(Gdk.KEY_bracketleft if index % 2 else Gdk.KEY_comma))
            self.assert_current_point(index)
        self.assertTrue(self.key(Gdk.KEY_comma))
        self.assertEqual(self.canvas.active, 0)
        self.assertFalse(self.model.dirty)
        self.canvas.on_change.assert_not_called()
        self.assertEqual(self.canvas.on_select.call_count, 16)

    def test_nudging_announces_edited_values_and_calls_change_only(self):
        self.assertTrue(self.key(Gdk.KEY_Right))
        self.assert_current_point(0)
        self.assertTrue(self.model.dirty)
        self.canvas.on_change.assert_called_once_with()
        self.canvas.on_select.assert_not_called()

    def test_drag_selection_and_movement_have_separate_callbacks(self):
        x, y = self.canvas.geometry().to_px(*self.model.points[2])
        self.canvas._begin(Mock(), x, y)
        self.assert_current_point(2)
        self.canvas.on_select.assert_called_once_with()
        self.canvas.on_change.assert_not_called()
        self.assertFalse(self.model.dirty)
        self.canvas._update(None, 5, -5)
        self.assert_current_point(2)
        self.canvas.on_change.assert_called_once_with()
        self.assertTrue(self.model.dirty)

    def test_tab_and_unknown_keys_preserve_normal_focus_navigation(self):
        self.canvas.update_property.reset_mock()
        self.assertFalse(self.key(Gdk.KEY_Tab))
        self.assertFalse(self.key(Gdk.KEY_Escape))
        self.canvas.update_property.assert_not_called()
        self.canvas.on_change.assert_not_called()
        self.canvas.on_select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
