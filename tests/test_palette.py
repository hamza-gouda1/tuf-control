# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free checks for shared palette generation and drift detection."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from asusius.brand_palette import ACCENTS, HEX_PALETTES
from asusius.effects import plugins, theme

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_tuf_palette", REPO / "tools/sync_tuf_palette.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class PaletteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.target = self.root / "source", self.root / "target"
        (self.source / "tokens").mkdir(parents=True)
        ui = self.target / "src/asusius/ui"
        ui.mkdir(parents=True)
        for filename in ("style.css", "style-dark.css"):
            (ui / filename).write_text("/* fixture */\n@define-color old #000000;\n"
                                       ".tile { padding: 16px; }\n")
        self.tokens = self.source / "tokens/tokens.toml"
        lines = []
        for scheme, variant in (("dark", "Bordeaux"), ("light", "Merlot-Day")):
            lines.append(f"[variant.{variant}.palette]")
            lines += [f"{key} = {json.dumps(value)}" for key, value in HEX_PALETTES[scheme].items()]
        lines += ["[variant.Oxblood-Noir.palette]", f"accent = {json.dumps(ACCENTS['oxblood-noir'])}"]
        self.tokens.write_text("\n".join(lines) + "\n")

    def run_tool(self, check=False):
        args = ["--source", str(self.source), "--target", str(self.target)]
        with contextlib.redirect_stdout(io.StringIO()):
            return sync.main(args + (["--check"] if check else []))

    def test_generation_check_detects_and_repairs_source_drift(self):
        self.assertEqual(self.run_tool(check=True), 1)
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual(self.run_tool(check=True), 0)
        before = self.tokens.read_text()
        light_gold = HEX_PALETTES["light"]["gold"]
        self.tokens.write_text(before.replace(f'gold = "{light_gold}"', 'gold = "#123456"'))
        self.assertEqual(self.run_tool(check=True), 1)
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual(self.run_tool(check=True), 0)
        namespace = {}
        output = self.target / "src/asusius/brand_palette.py"
        exec(compile(output.read_text(), str(output), "exec"), namespace)
        self.assertEqual(namespace["HEX_PALETTES"]["light"]["gold"], "#123456")
        self.assertEqual(namespace["PALETTE"]["light"]["gold"], (18 / 255, 52 / 255, 86 / 255))
        css = (self.target / "src/asusius/ui/style.css").read_text()
        self.assertIn("@define-color gold_color #123456;", css)
        self.assertIn(".tile { padding: 16px; }", css)
        self.assertEqual(css.count(sync.BEGIN), 1)
        self.assertEqual(css.count(sync.END), 1)

    def test_css_role_mapping_and_generated_output_are_deterministic(self):
        self.run_tool()
        outputs = sync.generated_files(self.source, self.target)
        self.assertEqual(outputs, sync.generated_files(self.source, self.target))
        for scheme, filename in (("light", "style.css"), ("dark", "style-dark.css")):
            css = outputs[self.target / "src/asusius/ui" / filename]
            for role, token in sync.ROLE_TOKENS.items():
                self.assertIn(f"@define-color {role} {HEX_PALETTES[scheme][token]};", css)
        self.assertIs(theme.ACCENTS, ACCENTS)
        self.assertEqual(plugins.BORDEAUX, ACCENTS["bordeaux"].lower())
        self.assertEqual(plugins.BRAND_GOLD, HEX_PALETTES["dark"]["gold"].lower())

    def test_invalid_shared_colour_fails_without_writing_target(self):
        self.tokens.write_text(self.tokens.read_text().replace('gold = "', 'gold = "invalid', 1))
        before = (self.target / "src/asusius/ui/style.css").read_text()
        with self.assertRaises(ValueError):
            self.run_tool()
        self.assertEqual((self.target / "src/asusius/ui/style.css").read_text(), before)
        self.assertFalse((self.target / "src/asusius/brand_palette.py").exists())


if __name__ == "__main__":
    unittest.main()
