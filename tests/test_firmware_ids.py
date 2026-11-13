# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Firmware report identity remains unique for devices with matching prefixes."""
import json
import unittest
from pathlib import Path

from asusius.updates import firmware


class FirmwareIdentityTests(unittest.TestCase):
    def test_devices_with_matching_prefix_keep_separate_results(self):
        data = json.loads((Path(__file__).parent / "fixtures/updates/fwupd_prefix_collision.json").read_text())
        items, _info = firmware.parse(data["devices"], data["upgrades"])
        by_id = {item.id: item for item in items}
        self.assertEqual(len(by_id), 2)
        self.assertEqual({item.available for item in by_id.values()}, {"2", "3"})
        for item in items:
            self.assertEqual(item.id, "firmware:" + item.extra["device_id"])


if __name__ == "__main__":
    unittest.main()
