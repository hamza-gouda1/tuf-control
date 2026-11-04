# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Held-package diagnosis shares a deadline; all apt commands are mocked."""
import subprocess
import unittest
from unittest import mock

from tuf_control.updates import apt


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def packages(count):
    return [apt._item(f"held-{i}", "amd64", "1", "2") for i in range(count)]


class AptBudgetTests(unittest.TestCase):
    def test_many_hanging_items_share_one_budget_and_remaining_items_stay_held(self):
        clock = Clock()
        items = packages(100)
        calls = []

        def hung(cmd, timeout):
            calls.append((cmd, timeout))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, apt.DIAGNOSTIC_COMMAND_TIMEOUT_S)
            clock.now += timeout
            raise subprocess.TimeoutExpired(cmd, timeout)

        with mock.patch.object(apt.time, "monotonic", side_effect=clock), \
                mock.patch.object(apt, "_run", side_effect=hung):
            apt.text_blockers(items, {"held-99"}, "", "dist-upgrade evidence", "resolver evidence")
        self.assertLessEqual(clock.now - 100, apt.DIAGNOSTIC_BUDGET_S)
        self.assertLessEqual(len(calls), 5)
        self.assertTrue(all(i.extra["held"] and i.extra["facts"]["kept_back"] for i in items))
        remainder = items[-1]
        self.assertTrue(remainder.extra["facts"]["diagnostics_deferred"])
        self.assertTrue(remainder.extra["facts"]["dpkg_hold"])
        self.assertEqual(remainder.extra["blocker"]["blocker"], "held")
        self.assertFalse(remainder.extra["blocker"]["fixable"])
        for item in items[5:]:
            self.assertNotIn("new_packages", item.extra["facts"])
            self.assertEqual(item.extra["facts"]["dist_upgrade"], "dist-upgrade evidence")

    def test_last_command_timeout_is_capped_by_remaining_wall_time(self):
        clock = Clock()
        timeouts = []

        def slow(cmd, timeout):
            timeouts.append(timeout)
            clock.now += min(2.7, timeout)
            return ""  # no facts that would justify installing extra dependencies

        with mock.patch.object(apt.time, "monotonic", side_effect=clock), \
                mock.patch.object(apt, "_run", side_effect=slow):
            apt.text_blockers(packages(20), set(), "", "")
        self.assertAlmostEqual(clock.now - 100, apt.DIAGNOSTIC_BUDGET_S)
        self.assertGreater(timeouts[-1], 0)
        self.assertLess(timeouts[-1], apt.DIAGNOSTIC_COMMAND_TIMEOUT_S)
        self.assertAlmostEqual(timeouts[-1], 1.5)

    def test_fast_targeted_diagnostics_keep_exact_facts_and_skip_safe_updates(self):
        items = packages(3)
        safe = "Inst held-1 [1] (2 Ubuntu:noble-updates [amd64])\n"
        target = ("Inst held-0 [1] (2 Ubuntu:noble-updates [amd64])\n"
                  "Inst runtime (1 Ubuntu:noble-updates [amd64])\n")
        with mock.patch.object(apt, "_run", side_effect=[target, "(phased 40%)", "", ""]) as run:
            apt.text_blockers(items, set(), safe, "batch evidence")
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(call.kwargs["timeout"] <= apt.DIAGNOSTIC_COMMAND_TIMEOUT_S
                            for call in run.call_args_list))
        self.assertEqual(items[0].extra["facts"]["new_packages"], ["runtime"])
        self.assertEqual(items[0].extra["facts"]["phased_pct"], 40)
        self.assertEqual(items[0].extra["blocker"]["blocker"], "phased")
        self.assertNotIn("held", items[1].extra)
        self.assertNotIn("diagnostics_deferred", items[0].extra["facts"])

    def test_expired_budget_starts_no_commands_and_does_not_invent_a_failure(self):
        clock = mock.Mock(side_effect=[100.0, 116.0, 116.0])
        items = packages(1)
        with mock.patch.object(apt.time, "monotonic", clock), mock.patch.object(apt, "_run") as run:
            apt.text_blockers(items, set(), "", "")
        run.assert_not_called()
        facts = items[0].extra["facts"]
        self.assertTrue(facts["diagnostics_deferred"])
        self.assertNotIn("broken", facts)
        self.assertEqual(items[0].extra["blocker"]["blocker"], "unknown")
        self.assertFalse(items[0].extra["blocker"]["fixable"])


if __name__ == "__main__":
    unittest.main()
