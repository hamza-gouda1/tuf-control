# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Opting into checkout checks must not inject the maintainer's own paths."""
import unittest

from tuf_control.updates.settings import validate_updates_section


class CheckoutDefaultsTests(unittest.TestCase):
    def test_opt_in_needs_owner_supplied_checkouts(self):
        settings = validate_updates_section({"check_repos": True})
        self.assertEqual(settings["repos"], [])
        self.assertTrue(settings["check_repos"])


if __name__ == "__main__":
    unittest.main()
