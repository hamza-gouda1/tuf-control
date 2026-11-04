# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Release archives are deterministic, tag-pinned and safe to unpack by prefix."""
import hashlib
import importlib.util
import re
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_archive", REPO / "tools/release_archive.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseArchiveTests(unittest.TestCase):
    def test_tagged_source_is_reproducible_and_checksum_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            first, digest = release.build_archive(REPO, "v0.1.0", output / "one")
            second, other = release.build_archive(REPO, "v0.1.0", output / "two")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(digest, other)
            self.assertEqual(digest, hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual((first.parent / "SHA256SUMS").read_text(),
                             f"{digest}  {first.name}\n")
            with tarfile.open(first) as archive:
                names = archive.getnames()
            self.assertTrue(names)
            prefix = REPO.name + "-0.1.0"
            self.assertTrue(all(name == prefix or name.startswith(prefix + "/") for name in names))
            if REPO.name == "tuf-control":
                packaging = (REPO / "packaging/arch/PKGBUILD").read_text()
                expected = re.search(r"sha256sums=\('([0-9a-f]{64})'\)", packaging)
                self.assertIsNotNone(expected)
                self.assertEqual(expected[1], digest)

    def test_invalid_tags_cannot_become_git_options_or_output_paths(self):
        with patch.object(release.subprocess, "run") as run:
            for tag in ("--all", "../main", "v1.2.3/../../bad", "main"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.build_archive(REPO, tag, Path("unused"))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
