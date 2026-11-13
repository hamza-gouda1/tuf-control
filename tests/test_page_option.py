# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

import contextlib
import io
import unittest

from asusius.ui.app import _show_start_page


class FakeWindow:
    def __init__(self, keys: tuple[str, ...]) -> None:
        self.pages = {key: object() for key in keys}
        self.shown: list[str] = []

    def show_page(self, key: str) -> None:
        self.shown.append(key)


class PageOptionTests(unittest.TestCase):
    def test_valid_key_shows_page_without_warning(self) -> None:
        window = FakeWindow(("dashboard", "updates"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = _show_start_page(window, "updates")
        self.assertTrue(result)
        self.assertEqual(window.shown, ["updates"])
        self.assertEqual(stderr.getvalue(), "")

    def test_invalid_key_warns_with_available_keys_and_does_not_show_page(self) -> None:
        window = FakeWindow(("dashboard", "processes", "dgpu"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = _show_start_page(window, "gpu-monitor")
        self.assertFalse(result)
        self.assertEqual(window.shown, [])
        self.assertEqual(
            stderr.getvalue(),
            "asusius-gui: unknown page 'gpu-monitor'; valid pages: dashboard, processes, dgpu\n",
        )

    def test_missing_key_is_silent(self) -> None:
        window = FakeWindow(("dashboard",))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = _show_start_page(window, None)
        self.assertFalse(result)
        self.assertEqual(window.shown, [])
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
