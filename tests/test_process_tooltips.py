# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Process column tooltip wiring, exercised without importing or creating GTK."""
import ast
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] /
          "src/asusius/ui/pages/processes.py")


class FakeHeader:
    def __init__(self, text):
        self.text = text
        self.tooltip = None

    def get_text(self):
        return self.text

    def set_tooltip_text(self, text):
        self.tooltip = text


class ProcessTooltipTests(unittest.TestCase):
    def test_header_labels_receive_their_column_explanations(self):
        tree = ast.parse(SOURCE.read_text())
        columns = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "COLUMNS"
                               for target in node.targets))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_attach_header_tooltip")
        namespace = {"COLUMNS": ast.literal_eval(columns.value)}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SOURCE), "exec"),
             namespace)

        headers = {title: FakeHeader(f"{title}\n42 %")
                   for _key, title, _chars, tip in namespace["COLUMNS"] if tip}
        for title, header in headers.items():
            with self.subTest(title=title):
                namespace["_attach_header_tooltip"](header)
                expected = next(tip for _key, name, _chars, tip in namespace["COLUMNS"]
                                if name == title)
                self.assertEqual(header.tooltip, expected)

    def test_columns_without_explanations_remain_unmodified(self):
        tree = ast.parse(SOURCE.read_text())
        columns = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "COLUMNS"
                               for target in node.targets))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_attach_header_tooltip")
        namespace = {"COLUMNS": ast.literal_eval(columns.value)}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SOURCE), "exec"),
             namespace)
        header = FakeHeader("Name")
        namespace["_attach_header_tooltip"](header)
        self.assertIsNone(header.tooltip)


if __name__ == "__main__":
    unittest.main()
