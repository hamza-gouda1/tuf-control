# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Packaging metadata must point at repositories that actually exist."""
import re
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
SYMBOLIC_ICON = "data/icons/hicolor/scalable/apps/io.github.hamzagouda.Asusius-symbolic.svg"
UI_ICON = "src/asusius/ui/icons/hicolor/scalable/apps/io.github.hamzagouda.Asusius.svg"
UI_SYMBOLIC_ICON = "src/asusius/ui/icons/hicolor/scalable/apps/io.github.hamzagouda.Asusius-symbolic.svg"
HERO_DARK = "docs/brand/asusius-hero-dark.svg"
HERO_LIGHT = "docs/brand/asusius-hero-light.svg"


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
        self.assertIn(f"https://raw.githubusercontent.com/{LIVE_REPO}/", meta)
        self.assertNotIn(f"raw.githubusercontent.com/{DEAD_REPO}", meta)
        service = (ROOT / "data/systemd/asusiusd.service").read_text(encoding="utf-8")
        self.assertIn(f"Documentation=https://github.com/{LIVE_REPO}/blob/main/", service)
        config = (ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")
        self.assertIn(f"https://github.com/{LIVE_REPO}/security/advisories/new", config)


class IconGlyphTests(unittest.TestCase):
    def test_icon_is_the_armored_crest_not_legacy_monograms(self):
        svg = (ROOT / ICON).read_text(encoding="utf-8")
        # Legacy TC arc/crossbar mark must stay retired.
        self.assertNotIn('"TC"', svg)
        self.assertNotIn("A 33 33 0 1 0", svg)
        self.assertNotIn("M 46 47 H 80", svg)
        self.assertNotIn("M 63 47 V 80", svg)
        # Legacy single-stroke wireframe A monogram on wine rectangle must stay retired.
        self.assertNotIn("M 44 88 L 64 42 L 84 88", svg)
        self.assertNotIn("M 52 70 H 76", svg)
        # Current mark: Concept 3 Obsidian & Bordeaux Armored Crest.
        self.assertIn('viewBox="0 0 128 128"', svg)
        self.assertIn("c3-obsidian", svg)
        self.assertIn("c3-bordeaux", svg)
        self.assertIn("c3-gold-lit", svg)

    def test_symbolic_icon_uses_clean_monochrome_fills(self):
        svg = (ROOT / SYMBOLIC_ICON).read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 16 16"', svg)
        self.assertIn('fill="#241f31"', svg)
        self.assertNotIn("<linearGradient", svg)
        self.assertNotIn("<radialGradient", svg)
        self.assertNotIn("<filter", svg)
        self.assertNotIn("stroke=", svg)

    def test_ui_bundled_icons_match_data_icons(self):
        self.assertEqual(
            (ROOT / UI_ICON).read_text(encoding="utf-8"),
            (ROOT / ICON).read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (ROOT / UI_SYMBOLIC_ICON).read_text(encoding="utf-8"),
            (ROOT / SYMBOLIC_ICON).read_text(encoding="utf-8"),
        )

    def test_brand_svgs_and_metainfo_are_well_formed_xml_with_valid_refs(self):
        xml_files = (
            ICON,
            SYMBOLIC_ICON,
            UI_ICON,
            UI_SYMBOLIC_ICON,
            HERO_DARK,
            HERO_LIGHT,
            "data/Asusius.metainfo.xml",
        )
        for name in xml_files:
            with self.subTest(file=name):
                path = ROOT / name
                self.assertTrue(path.is_file(), f"Missing file: {name}")
                root = ET.parse(path).getroot()
                if name.endswith(".svg"):
                    ids = {el.attrib["id"] for el in root.iter() if "id" in el.attrib}
                    text = path.read_text(encoding="utf-8")
                    for ref in re.findall(r"url\(#([^)]+)\)", text):
                        self.assertIn(ref, ids, f"Broken url(#{ref}) in {name}")

    def test_issue_template_config_is_well_formed_yaml(self):
        with (ROOT / ".github/ISSUE_TEMPLATE/config.yml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        self.assertIn("contact_links", data)


if __name__ == "__main__":
    unittest.main()
