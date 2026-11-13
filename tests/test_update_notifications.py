# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Notification delivery and click-watch lifecycle with temporary state and fake buses."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from asusius.updates import check, notify
from asusius.updates.model import Report, SourceResult, UpdateItem
from asusius.updates.scheduler import UpdateScheduler, mark_seen


def report():
    return Report(checked_ts=100, sources=[SourceResult("apt", items=[
        UpdateItem("apt:example", "apt", "example", "1", "2", apply_method="packagekit")])])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"HOME": str(self.root),
                                 "ASUSIUS_CACHE_DIR": str(self.root / "cache"),
                                 "GSETTINGS_BACKEND": "keyfile"})
        environment.start()
        self.addCleanup(environment.stop)
        self.report = report()

    def test_zero_id_retries_identical_report_then_success_deduplicates(self):
        notifier = Mock(side_effect=[0, 17])
        scheduler = UpdateScheduler(notifier=notifier)
        self.assertIsNone(scheduler.after_check(self.report))
        self.assertEqual(check.load_cache().get("notify", {}).get("seen", []), [])
        self.assertEqual(scheduler.after_check(self.report), "1 update available (apt 1)")
        self.assertEqual(check.load_cache()["notify"]["seen"], self.report.keys())
        self.assertIsNone(scheduler.after_check(self.report))
        self.assertEqual(notifier.call_count, 2)

    def test_exception_does_not_mark_seen_and_next_check_retries(self):
        notifier = Mock(side_effect=[RuntimeError("synthetic notification failure"), 3])
        scheduler = UpdateScheduler(notifier=notifier)
        self.assertIsNone(scheduler.after_check(self.report))
        self.assertIsNotNone(scheduler.after_check(self.report))
        self.assertEqual(notifier.call_count, 2)

    def test_seen_commits_after_delivery_merging_concurrent_state(self):
        def deliver(*_):
            self.assertNotIn("apt:example=2", check.load_cache().get("notify", {}).get("seen", []))
            check.update_cache(lambda state: state.update(notify={
                "seen": ["apt:other=3"], "ignored": ["retained"], "snooze_until": 500}))
            return 9

        scheduler = UpdateScheduler(notifier=deliver)
        self.assertIsNotNone(scheduler.after_check(self.report))
        state = check.load_cache()["notify"]
        self.assertEqual(state["seen"], ["apt:other=3", "apt:example=2"])
        self.assertEqual(state["ignored"], ["retained"])
        self.assertEqual(state["snooze_until"], 500)

    def test_intentional_suppression_and_owner_view_still_mark_seen(self):
        notifier = Mock(return_value=1)
        scheduler = UpdateScheduler(notifier=notifier)
        scheduler.configure({"notify": False})
        self.assertIsNone(scheduler.after_check(self.report))
        scheduler.configure({"notify": True})
        self.assertIsNone(scheduler.after_check(self.report))
        check.update_cache(lambda state: state.update(notify={}))
        mark_seen(self.report)
        self.assertIsNone(scheduler.after_check(self.report))
        notifier.assert_not_called()

    def test_default_notifier_returns_id_and_retains_replacement_on_failure(self):
        scheduler = UpdateScheduler()
        with patch.object(notify, "send", side_effect=[17, 0]) as send:
            self.assertEqual(scheduler._notify("summary", "body"), 17)
            self.assertEqual(scheduler._notify("summary", "body"), 0)
        self.assertEqual(scheduler._notify_id, 17)
        self.assertEqual(send.call_args.args[2], 17)


class FakeContext:
    def __init__(self):
        self.waiting = threading.Event()
        self.woken = threading.Event()
        self.iterations = []
        self.push_thread_default = Mock()
        self.pop_thread_default = Mock()

    def iteration(self, blocking):
        self.iterations.append(blocking)
        self.waiting.set()
        self.woken.wait(2)
        self.woken.clear()

    def wakeup(self):
        self.woken.set()


class ClickWatchTests(unittest.TestCase):
    def start_watch(self):
        context = FakeContext()
        source = Mock()
        glib = SimpleNamespace(MainContext=SimpleNamespace(new=lambda: context),
                               timeout_source_new_seconds=Mock(return_value=source))
        repository = SimpleNamespace(Gio=SimpleNamespace(DBusSignalFlags=SimpleNamespace(NONE=0)),
                                     GLib=glib)
        modules = patch.dict("sys.modules", {"gi.repository": repository})
        modules.start()
        self.addCleanup(modules.stop)
        connection = Mock()
        connection.signal_subscribe.return_value = 7
        watch = notify._ClickWatch(connection, 11)
        self.addCleanup(watch.close)
        self.assertEqual(watch.thread.name, "asusius-update-notification")
        self.assertTrue(context.waiting.wait(1), "watch must enter its private context")
        return watch, context, source, glib, connection

    def test_blocks_with_one_second_timeout_and_wakes_promptly_on_close(self):
        watch, context, source, glib, connection = self.start_watch()
        glib.timeout_source_new_seconds.assert_called_once_with(1)
        self.assertEqual(context.iterations, [True])
        timeout = source.set_callback.call_args.args[0]
        self.assertTrue(timeout())
        watch.close()
        self.assertFalse(watch.thread.is_alive())
        self.assertFalse(timeout())
        source.destroy.assert_called_once_with()
        connection.signal_unsubscribe.assert_called_once_with(7)
        context.pop_thread_default.assert_called_once_with()

    def test_click_and_notification_close_retire_subscription(self):
        for signal in ("ActionInvoked", "NotificationClosed"):
            with self.subTest(signal=signal):
                watch, _context, source, _glib, connection = self.start_watch()
                with patch.object(notify, "_launch_app") as launch:
                    watch._signal(None, None, None, None, signal,
                                  SimpleNamespace(unpack=lambda: (11, "default")))
                    watch.thread.join(1)
                    self.assertFalse(watch.thread.is_alive())
                    self.assertEqual(launch.call_count, int(signal == "ActionInvoked"))
                source.destroy.assert_called_once_with()
                connection.signal_unsubscribe.assert_called_once_with(7)

    def test_other_notification_signal_does_not_stop_or_launch(self):
        watch, _context, _source, _glib, _connection = self.start_watch()
        with patch.object(notify, "_launch_app") as launch:
            watch._signal(None, None, None, None, "ActionInvoked",
                          SimpleNamespace(unpack=lambda: (99, "default")))
            self.assertFalse(watch.stop.is_set())
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
