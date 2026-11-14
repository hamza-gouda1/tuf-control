# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Packaging metadata must point at repositories that actually exist."""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# github.com/hamza-gouda1/tuf-control is the retired name (now a 301 redirect to
# the renamed repository); packaging files must reference only the live name.
DEAD_REPO = "hamza-gouda1/tuf-control"
LIVE_REPO = "hamza-gouda1/asusius"

REPO_URL_FILES = (
    "README.md",
    "data/Asusius.metainfo.xml",
    "data/systemd/asusiusd.service",
    ".github/ISSUE_TEMPLATE/config.yml",
    "docs/QUICKSTART.md",
)

ICON = "data/icons/hicolor/scalable/apps/io.github.hamzagouda.Asusius.svg"


class RepoUrlTests(unittest.TestCase):
    def test_packaging_files_do_not_reference_the_dead_repo(self):
        for name in REPO_URL_FILES:
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn(DEAD_REPO, text)

    def test_key_urls_point_at_the_live_repo(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"https://github.com/{LIVE_REPO}/actions/workflows/ci.yml", readme)
        self.assertIn(
            f"https://github.com/{LIVE_REPO}/issues/new?template=hardware_report.yml", readme
        )
        meta = (ROOT / "data/Asusius.metainfo.xml").read_text(encoding="utf-8")
        self.assertIn(f"https://raw.githubusercontent.com/{LIVE_REPO}/v0.1.1/", meta)
        self.assertNotIn(f"raw.githubusercontent.com/{DEAD_REPO}", meta)
        service = (ROOT / "data/systemd/asusiusd.service").read_text(encoding="utf-8")
        self.assertIn(f"Documentation=https://github.com/{LIVE_REPO}/blob/main/", service)
        config = (ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")
        self.assertIn(f"https://github.com/{LIVE_REPO}/security/advisories/new", config)


class IconGlyphTests(unittest.TestCase):
    def test_icon_is_not_the_old_tc_monogram(self):
        svg = (ROOT / ICON).read_text(encoding="utf-8")
        # the old mark: C drawn as a 270-degree fan arc plus T crossbar and stem
        self.assertNotIn('"TC"', svg)
        self.assertNotIn("A 33 33 0 1 0", svg)
        self.assertNotIn("M 46 47 H 80", svg)
        self.assertNotIn("M 63 47 V 80", svg)
        # the new mark: an A monogram for Asusius
        self.assertIn("M 44 88 L 64 42 L 84 88", svg)
        self.assertIn("M 52 70 H 76", svg)

    def test_icon_and_metainfo_are_well_formed_xml(self):
        for name in (ICON, "data/Asusius.metainfo.xml"):
            with self.subTest(file=name):
                ET.parse(ROOT / name)

    def test_issue_template_config_is_well_formed_yaml(self):
        with (ROOT / ".github/ISSUE_TEMPLATE/config.yml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        self.assertIn("contact_links", data)


if __name__ == "__main__":
    unittest.main()
