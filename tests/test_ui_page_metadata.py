# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Navigation metadata stays consistent with page headings and documentation.

Read the literal metadata without importing GTK or constructing a display.
"""
import ast
import unittest
from pathlib import Path

from tuf_control.ui import viewmodels

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src/tuf_control/ui"


def assigned_literal(body, name):
    for statement in body:
        if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name
                for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"missing literal metadata: {name}")


class PageMetadataTests(unittest.TestCase):
    def entries(self):
        tree = ast.parse((UI / "window.py").read_text())
        return assigned_literal(tree.body, "CONTROL")

    def test_control_page_headings_match_navigation(self):
        for key, title, _icon, module, class_name in self.entries():
            with self.subTest(page=key):
                tree = ast.parse((UI / "pages" / f"{module}.py").read_text())
                cls = next(node for node in tree.body
                           if isinstance(node, ast.ClassDef) and node.name == class_name)
                metadata = next(node for node in cls.body
                                if isinstance(node, ast.Assign) and any(
                                    isinstance(target, ast.Tuple) and
                                    [name.id for name in target.elts] == ["key", "title"]
                                    for target in node.targets))
                self.assertEqual(ast.literal_eval(metadata.value), (key, title))

    def test_every_control_page_is_documented(self):
        docs = (ROOT / "docs/UI.md").read_text().split("## Pages", 1)[1].split("\n## ", 1)[0]
        for key, title, _icon, _module, _class in self.entries():
            with self.subTest(page=key):
                self.assertIn(title, docs)

    def test_no_second_page_registry_in_viewmodels(self):
        self.assertFalse(hasattr(viewmodels, "PAGES"))


if __name__ == "__main__":
    unittest.main()
