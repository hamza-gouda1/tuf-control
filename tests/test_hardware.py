# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Read-only smoke test against the real machine. Skipped unless explicitly enabled."""
import os
import unittest

from asusius.controller import Controller
from asusius.core import Capability


try:  # optional: lets pytest users select with -m hardware
    import pytest
    _mark = pytest.mark.hardware
except ImportError:
    def _mark(cls):
        return cls


@_mark
@unittest.skipUnless(os.environ.get("ASUSIUS_HARDWARE_TESTS") == "1",
                     "set ASUSIUS_HARDWARE_TESTS=1 to run (reads only)")
class HardwareSmoke(unittest.TestCase):
    def test_reads(self):
        os.environ.pop("ASUSIUS_FAKE_ROOT", None)
        ctl = Controller()
        self.assertTrue(ctl.capabilities()["telemetry"]["available"])
        ctl.read(Capability.TELEMETRY)
        ctl.cross_check()
