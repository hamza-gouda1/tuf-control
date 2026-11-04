# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The UI QA lint's pure logic (no display), plus an opt-in headless run.

Fast subset, always on: WCAG contrast maths, text/background colour extraction, geometry
helpers, GLib log parsing with its allowlist, finding de-duplication, the report, stale
colour detection and the matrix definition.

Slow headless run (a private session bus, mutter --headless, several GTK processes under a
1500 MB systemd scope; minutes):

    TUF_QA_SLOW=1 python3 -m unittest tests.ui.test_qa_lint            # --quick matrix
    python3 tests/ui/qa_lint.py                                         # the full matrix
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qa_core as q  # noqa: E402


class ContrastTests(unittest.TestCase):
    def test_extremes_and_symmetry(self):
        self.assertAlmostEqual(q.contrast_ratio((0, 0, 0), (255, 255, 255)), 21.0, places=1)
        self.assertAlmostEqual(q.contrast_ratio((255, 255, 255), (255, 255, 255)), 1.0)
        a, b = (142, 42, 68), (248, 243, 241)
        self.assertAlmostEqual(q.contrast_ratio(a, b), q.contrast_ratio(b, a))

    def test_known_value(self):
        # #767676 on white is the classic 4.54:1 AA pass; #777777 is 4.48:1 and fails
        self.assertGreater(q.contrast_ratio((0x76,) * 3, (255,) * 3), 4.5)
        self.assertLess(q.contrast_ratio((0x77,) * 3, (255,) * 3), 4.5)

    def test_required_ratio_large_text(self):
        self.assertEqual(q.required_ratio(13, False), 4.5)
        self.assertEqual(q.required_ratio(24, False), 3.0)  # 18 pt
        self.assertEqual(q.required_ratio(19, True), 3.0)  # 14 pt bold
        self.assertEqual(q.required_ratio(19, False), 4.5)

    def test_text_colours_pick_background_and_ink(self):
        cols = [(900, (255, 255, 255)), (60, (40, 26, 31)), (30, (200, 190, 192))]
        bg, fg = q.text_colours(cols)
        self.assertEqual(bg, (255, 255, 255))
        self.assertEqual(fg, (40, 26, 31))

    def test_nothing_drawn(self):
        self.assertIsNone(q.text_colours([(1000, (20, 20, 20)), (3, (21, 21, 20))]))
        self.assertIsNone(q.text_colours([]))


class GeometryTests(unittest.TestCase):
    def test_overlap(self):
        self.assertEqual(q.overlap_area((0, 0, 10, 10), (5, 5, 10, 10)), 25)
        self.assertEqual(q.overlap_area((0, 0, 10, 10), (10, 0, 10, 10)), 0)
        self.assertEqual(q.overlap_area((0, 0, 10, 10), (20, 20, 5, 5)), 0)

    def test_overflow(self):
        parent = (100, 0, 200, 50)
        self.assertEqual(q.overflow_x((110, 0, 50, 10), parent), 0)
        self.assertEqual(q.overflow_x((90, 0, 50, 10), parent), 10)
        self.assertEqual(q.overflow_x((250, 0, 80, 10), parent), 30)
        self.assertEqual(q.overflow_x((99.5, 0, 50, 10), parent), 0)  # sub-pixel rounding
        self.assertEqual(q.overflow_y((110, -10, 50, 10), parent), 10)

    def test_target_sizes(self):
        self.assertEqual(q.classify_target(20, 40), "fail")
        self.assertEqual(q.classify_target(46, 26), "warn")
        self.assertIsNone(q.classify_target(34, 34))
        self.assertIsNone(q.classify_target(24 + 8, 32))

    def test_centred(self):
        win = (0, 0, 1280, 720)
        self.assertTrue(q.centred((400, 200, 480, 320), win))
        self.assertFalse(q.centred((0, 0, 480, 320), win))


class LogTests(unittest.TestCase):
    SAMPLE = "\n".join([
        "(python3:123): Gtk-WARNING **: 12:00:01.234: Negative content width -3 (allocation 1, "
        "extents 2x2) while allocating gadget",
        "(python3:123): Adwaita-WARNING **: 12:00:01.500: Using GtkSettings:gtk-application-"
        "prefer-dark-theme with libadwaita is unsupported.",
        "(python3:123): GLib-GIO-WARNING **: 12:00:02.000: portal not available",
        "(python3:123): Gtk-DEBUG: 12:00:02.100: this is only a debug line",
        "(python3:123): Gdk-CRITICAL **: 12:00:02.200: gdk_surface_do_thing: assertion failed",
        "(python3:123): Gtk-WARNING **: 12:00:01.234: Negative content width -3 (allocation 1, "
        "extents 2x2) while allocating gadget",
        "Traceback (most recent call last):",
        "  File \"x.py\", line 1, in <module>",
        "ValueError: boom",
        "/home/u/tuf/ui/foo.py:10: DeprecationWarning: Gtk.Thing is deprecated",
        "/repo/tests/ui/qa_lint.py:310: DeprecationWarning: harness-only call",
    ])

    def test_parse(self):
        lines = q.parse_log(self.SAMPLE)
        levels = [(l.domain, l.level) for l in lines]
        self.assertIn(("Gtk", "WARNING"), levels)
        self.assertIn(("Gtk", "DEBUG"), levels)
        self.assertIn(("Gdk", "CRITICAL"), levels)
        self.assertIn(("Python", "ERROR"), levels)

    def test_failures_are_deduplicated_and_filtered(self):
        texts = [l.text for l in q.log_failures(self.SAMPLE)]
        self.assertEqual(sum("Negative content width" in t for t in texts), 1)
        self.assertTrue(any("gdk_surface_do_thing" in t for t in texts))
        self.assertTrue(any("ValueError: boom" in t for t in texts))
        self.assertFalse(any("portal" in t for t in texts))  # harness noise
        self.assertFalse(any("only a debug line" in t for t in texts))
        self.assertFalse(any("qa_lint.py" in t for t in texts))  # the lint's own calls
        self.assertTrue(any("foo.py" in t for t in texts))  # the app's deprecations count

    def test_clean_log(self):
        self.assertEqual(q.log_failures("Gdk-Message: Error reading events\n"), [])


class FindingTests(unittest.TestCase):
    def test_dedupe_keeps_worst_severity_and_all_combos(self):
        col = q.Collected()
        col.add(q.Finding("ellipsized", "warn", "fans", "Label 'x'", "m"), "dark@1280x720")
        col.add(q.Finding("ellipsized", "fail", "fans", "Label 'x'", "m"), "light@900x700")
        col.add(q.Finding("ellipsized", "warn", "fans", "Label 'x'", "m"), "dark@900x700")
        self.assertEqual(col.count("fail"), 1)
        self.assertEqual(col.count("warn"), 0)
        (f, combos), = col.by_severity("fail")
        self.assertEqual(combos, ["dark@1280x720", "dark@900x700", "light@900x700"])

    def test_report(self):
        col = q.Collected()
        col.add(q.Finding("contrast", "fail", "dashboard", "Label 'W'", "3.2:1"), "light@1280x720")
        text = q.render_report(col, {"date": "today"})
        self.assertIn("1 failures", text)
        self.assertIn("### contrast (1)", text)
        self.assertIn("light@1280x720", text)
        self.assertIn("No findings", q.render_report(q.Collected()))
        self.assertEqual(q.summarise(q.Collected()), "no failures")


class StaleColourTests(unittest.TestCase):
    def test_same_palette_is_fine(self):
        fresh = [(900, (248, 243, 240)), (100, (40, 24, 32))]
        self.assertEqual(q.stale_colours(fresh, [(880, (248, 243, 240)), (120, (40, 24, 32))]),
                         [])

    def test_leftover_dark_surface_is_found(self):
        fresh = [(900, (248, 243, 240)), (100, (40, 24, 32))]
        switched = [(700, (248, 243, 240)), (200, (32, 24, 24)), (100, (40, 24, 32))]
        stale = q.stale_colours(fresh, [(700, (248, 243, 240)), (200, (200, 16, 16)),
                                        (100, (40, 24, 32))])
        self.assertEqual(stale[0][1], (200, 16, 16))
        self.assertEqual(q.stale_colours(fresh, switched), [])  # within tolerance of ink


class MatrixTests(unittest.TestCase):
    def test_sizes_pages_variants(self):
        self.assertEqual(list(q.SIZES), ["1920x1080", "1280x720", "900x700", "640x700",
                                         "360x640"])
        self.assertTrue(q.SIZES["1920x1080"][2])
        self.assertEqual(q.VARIANTS, ("dark", "light", "usertheme"))
        self.assertEqual(len(q.PAGES), len(set(q.PAGES)))

    def test_pages_match_the_sidebar(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from tuf_control.ui.window import ENTRIES
        self.assertEqual(sorted(q.PAGES), sorted(ENTRIES))


@unittest.skipUnless(os.environ.get("TUF_QA_SLOW"), "headless run: set TUF_QA_SLOW=1")
class HeadlessRun(unittest.TestCase):
    def test_quick_matrix_is_clean(self):
        report = HERE / "qa_report.md"
        proc = subprocess.run([sys.executable, str(HERE / "qa_lint.py"), "--quick",
                               "--report", str(report)], capture_output=True, text=True,
                              timeout=1800)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-1000:])


class SnapshotTargetTests(unittest.TestCase):
    def test_native_child_includes_dialog_overlay_without_coordinate_changes(self):
        from types import SimpleNamespace
        from qa_lint import snapshot_widget
        content = SimpleNamespace(get_width=lambda: 360, get_height=lambda: 640)
        dialog_host = SimpleNamespace(get_width=lambda: 360, get_height=lambda: 640)
        window = SimpleNamespace(get_width=lambda: 360, get_height=lambda: 640,
                                 get_content=lambda: content, get_child=lambda: dialog_host)
        # Application content excludes Adw.Dialog; capture its enclosing host.
        self.assertIs(snapshot_widget(window), dialog_host)
        partial = SimpleNamespace(get_width=lambda: 340, get_height=lambda: 600)
        window.get_child = lambda: partial
        self.assertIs(snapshot_widget(window), window)
        window.get_child = lambda: None
        self.assertIs(snapshot_widget(window), window)
        self.assertIs(snapshot_widget(content), content)

    def test_window_child_is_snapshotted_directly_instead_of_cached_paintable(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from qa_lint import snapshot_contents
        child = SimpleNamespace(get_width=lambda: 360, get_height=lambda: 640)
        window = SimpleNamespace(get_width=lambda: 360, get_height=lambda: 640,
                                 get_child=lambda: child, snapshot_child=Mock())
        snapshot, factory = object(), Mock()
        snapshot_contents(window, snapshot, factory)
        window.snapshot_child.assert_called_once_with(child, snapshot)
        factory.assert_not_called()
        snapshot_contents(child, snapshot, factory)
        factory.assert_called_once_with(widget=child)
        factory.return_value.snapshot.assert_called_once_with(snapshot, 360, 640)


class ToastOcclusionTests(unittest.TestCase):
    def test_only_mapped_toasts_supply_occluding_bounds(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from qa_audit import toast_rects
        toast = SimpleNamespace(get_mapped=lambda: True,
                                __gtype__=SimpleNamespace(name="AdwToastWidget"))
        root = SimpleNamespace(get_mapped=lambda: True,
                               __gtype__=SimpleNamespace(name="GtkBox"))
        with patch("qa_audit.children", return_value=[toast]), patch(
                "qa_audit.rect_in", return_value=(10, 20, 100, 40)):
            self.assertEqual(toast_rects(root, root), [(10, 20, 100, 40)])
            toast.get_mapped = lambda: False
            self.assertEqual(toast_rects(root, root), [])


if __name__ == "__main__":
    unittest.main()
