# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Private state writes and locks use only temporary files and synthetic tokens."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tuf_control.automation import config
from tuf_control.daemon import acquire_lock
from tuf_control.effects.ambient import MAX_TOKEN_BYTES, TokenStore


class StatePermissionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"HOME": str(self.root),
                                 "XDG_CONFIG_HOME": str(self.root / "config"),
                                 "XDG_RUNTIME_DIR": str(self.root / "runtime"),
                                 "GSETTINGS_BACKEND": "keyfile"})
        environment.start()
        self.addCleanup(environment.stop)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_atomic_token_is_private_before_replace_under_permissive_umask(self):
        path = self.root / "tokens" / "restore-token"
        store = TokenStore(path)
        original_replace = os.replace
        observations = []

        def replace(source, destination):
            observations.append((self.mode(Path(source)), Path(source).read_text()))
            original_replace(source, destination)

        previous_umask = os.umask(0)
        try:
            with patch.object(config.os, "replace", side_effect=replace):
                store.set("synthetic-token")
        finally:
            os.umask(previous_umask)
        self.assertEqual(observations, [(0o600, "synthetic-token\n")])
        self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.mode(path.parent), 0o700)
        self.assertEqual(store.get(), "synthetic-token")

    def test_token_write_replaces_symlink_without_touching_target(self):
        target = self.root / "unrelated"
        target.write_text("unchanged")
        target.chmod(0o644)
        token = self.root / "token"
        token.symlink_to(target)
        store = TokenStore(token)
        self.assertIsNone(store.get())
        store.set("replacement")
        self.assertFalse(token.is_symlink())
        self.assertEqual(target.read_text(), "unchanged")
        self.assertEqual(self.mode(target), 0o644)
        self.assertEqual(store.get(), "replacement")

    def test_token_failed_replace_preserves_old_file_and_cleans_temporary(self):
        token = self.root / "token"
        store = TokenStore(token)
        store.set("old")
        with patch.object(config.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                store.set("new")
        self.assertEqual(store.get(), "old")
        self.assertEqual(list(self.root.glob(".token.*")), [])

    def test_token_reads_reject_nonregular_oversized_and_invalid_text(self):
        token = self.root / "token"
        store = TokenStore(token)
        os.mkfifo(token)
        self.assertIsNone(store.get())
        token.unlink()
        token.mkdir()
        self.assertIsNone(store.get())
        token.rmdir()
        token.write_bytes(b"x" * (MAX_TOKEN_BYTES + 1))
        self.assertIsNone(store.get())
        token.write_bytes(b"\xff")
        self.assertIsNone(store.get())
        store.set(None)
        self.assertFalse(token.exists())

    def test_config_and_lock_modes_and_default_directory_upgrade(self):
        parent = self.root / "config" / "tuf-control"
        parent.mkdir(parents=True)
        parent.chmod(0o775)
        path = parent / "config.json"
        lock = parent / "config.json.lock"
        lock.write_text("")
        lock.chmod(0o666)
        previous_umask = os.umask(0)
        try:
            config.save_config({}, path)
        finally:
            os.umask(previous_umask)
        self.assertEqual(self.mode(parent), 0o700)
        self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.mode(lock), 0o600)

    def test_existing_custom_parent_is_never_chmodded(self):
        self.root.chmod(0o755)
        config.save_config({}, self.root / "custom.json")
        TokenStore(self.root / "custom-token").set("synthetic")
        self.assertEqual(self.mode(self.root), 0o755)

    def test_config_lock_rejects_symlink_and_fifo(self):
        path = self.root / "config.json"
        lock = self.root / "config.json.lock"
        target = self.root / "unrelated"
        target.write_text("unchanged")
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            config.save_config({}, path)
        self.assertEqual(target.read_text(), "unchanged")
        lock.unlink()
        os.mkfifo(lock)
        with self.assertRaises(OSError):
            config.save_config({}, path)

    def test_daemon_lock_is_private_and_rejects_symlinks_and_nonregular_files(self):
        parent = self.root / "runtime" / "tuf-control"
        parent.mkdir(parents=True)
        parent.chmod(0o775)
        lock = parent / "daemon.lock"
        handle = acquire_lock(lock)
        self.assertIsNotNone(handle)
        try:
            self.assertEqual(self.mode(parent), 0o700)
            self.assertEqual(self.mode(lock), 0o600)
            self.assertIsNone(acquire_lock(lock))
        finally:
            handle.close()
        lock.unlink()
        target = self.root / "unrelated"
        target.write_text("unchanged")
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            acquire_lock(lock)
        self.assertEqual(target.read_text(), "unchanged")
        lock.unlink()
        os.mkfifo(lock)
        with self.assertRaises(OSError):
            acquire_lock(lock)


if __name__ == "__main__":
    unittest.main()
