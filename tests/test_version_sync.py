# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Release versions must stay synchronized across package, metadata, packaging and docs."""
from __future__ import annotations

import re
import tomllib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VersionSyncTests(unittest.TestCase):
    def test_all_release_versions_match(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        pyproject_version = pyproject["project"]["version"]
        self.assertRegex(pyproject_version, r"^\d+\.\d+\.\d+$")

        init_text = (ROOT / "src/asusius/__init__.py").read_text(encoding="utf-8")
        init_match = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']', init_text)
        self.assertIsNotNone(init_match, "__version__ not found in src/asusius/__init__.py")
        package_version = init_match[1]

        pkgbuild_text = (ROOT / "packaging/arch/PKGBUILD").read_text(encoding="utf-8")
        pkgbuild_match = re.search(r"(?m)^pkgver=(\S+)", pkgbuild_text)
        self.assertIsNotNone(pkgbuild_match, "pkgver not found in packaging/arch/PKGBUILD")
        pkgbuild_version = pkgbuild_match[1]

        meta_root = ET.parse(ROOT / "data/Asusius.metainfo.xml").getroot()
        releases = meta_root.find("releases")
        self.assertIsNotNone(releases, "<releases> missing in data/Asusius.metainfo.xml")
        first_release = releases.find("release")
        self.assertIsNotNone(first_release, "<release> missing in data/Asusius.metainfo.xml")
        metainfo_version = first_release.attrib.get("version")
        metainfo_date = first_release.attrib.get("date")
        self.assertRegex(metainfo_date or "", r"^\d{4}-\d{2}-\d{2}$")

        changelog_text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        changelog_match = re.search(
            r"(?m)^## \[(\d+\.\d+\.\d+)\]\s*-\s*(\d{4}-\d{2}-\d{2})\s*$",
            changelog_text,
        )
        self.assertIsNotNone(
            changelog_match,
            "Latest dated release section missing in CHANGELOG.md",
        )
        changelog_version, changelog_date = changelog_match[1], changelog_match[2]

        found = {
            "pyproject.toml": pyproject_version,
            "src/asusius/__init__.py": package_version,
            "packaging/arch/PKGBUILD": pkgbuild_version,
            "data/Asusius.metainfo.xml": metainfo_version,
            "CHANGELOG.md": changelog_version,
        }
        self.assertEqual(
            set(found.values()),
            {pyproject_version},
            f"Version mismatch across files: {found}",
        )
        self.assertEqual(metainfo_date, changelog_date)


if __name__ == "__main__":
    unittest.main()
