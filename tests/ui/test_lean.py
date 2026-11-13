# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""ASUSIUS_RENDERER selection in ui/lean.py: which values actually set GSK_RENDERER."""
import os
import unittest

from asusius.ui.lean import RENDERER_ENV, choose_renderer


class RendererSelectionTests(unittest.TestCase):
    def setUp(self):
        keys = ("GSK_RENDERER", RENDERER_ENV)
        self._saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_gl_forces_gsk_renderer_gl(self):
        os.environ[RENDERER_ENV] = "gl"
        self.assertEqual(choose_renderer(), "gl")
        self.assertEqual(os.environ.get("GSK_RENDERER"), "gl")

    def test_ngl_and_vulkan_set_gsk_renderer(self):
        for want in ("ngl", "vulkan"):
            os.environ.pop("GSK_RENDERER", None)
            os.environ[RENDERER_ENV] = want
            self.assertEqual(choose_renderer(), want)
            self.assertEqual(os.environ.get("GSK_RENDERER"), want)

    def test_unset_forces_cairo(self):
        self.assertEqual(choose_renderer(), "cairo")
        self.assertEqual(os.environ.get("GSK_RENDERER"), "cairo")

    def test_default_and_empty_leave_gsk_renderer_unset(self):
        os.environ[RENDERER_ENV] = "default"
        self.assertEqual(choose_renderer(), "default")
        self.assertIsNone(os.environ.get("GSK_RENDERER"))
        os.environ[RENDERER_ENV] = ""
        self.assertEqual(choose_renderer(), "default")
        self.assertIsNone(os.environ.get("GSK_RENDERER"))

    def test_cairo_and_unknown_force_cairo(self):
        os.environ[RENDERER_ENV] = "cairo"
        self.assertEqual(choose_renderer(), "cairo")
        self.assertEqual(os.environ.get("GSK_RENDERER"), "cairo")
        os.environ[RENDERER_ENV] = "software"
        self.assertEqual(choose_renderer(), "cairo")
        self.assertEqual(os.environ.get("GSK_RENDERER"), "cairo")

    def test_explicit_gsk_renderer_wins(self):
        os.environ["GSK_RENDERER"] = "cairo"
        os.environ[RENDERER_ENV] = "gl"
        self.assertEqual(choose_renderer(), "cairo")
        self.assertEqual(os.environ.get("GSK_RENDERER"), "cairo")

    def test_case_and_whitespace_are_normalised(self):
        os.environ[RENDERER_ENV] = "  GL  "
        self.assertEqual(choose_renderer(), "gl")
        self.assertEqual(os.environ.get("GSK_RENDERER"), "gl")


if __name__ == "__main__":
    unittest.main()
