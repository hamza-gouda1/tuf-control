# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Release archives are deterministic, tag-pinned and safe to unpack by prefix."""
import hashlib
import importlib.util
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_archive", REPO / "tools/release_archive.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def _tag_exists(tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _latest_release_tag() -> str:
    tags = subprocess.check_output(
        ["git", "tag", "-l", "v*", "--sort=-v:refname"],
        cwd=REPO,
        text=True,
    ).splitlines()
    for tag in tags:
        if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            return tag
    raise AssertionError("no vMAJOR.MINOR.PATCH release tag found in repository")


class ReleaseArchiveTests(unittest.TestCase):
    def test_tagged_source_is_reproducible_and_checksum_matches(self):
        packaging = (REPO / "packaging/arch/PKGBUILD").read_text(encoding="utf-8")
        pkgver = re.search(r"(?m)^pkgver=(\S+)", packaging)[1]
        pkg_tag = f"v{pkgver}"
        tagged_pkgver = _tag_exists(pkg_tag)
        tag = pkg_tag if tagged_pkgver else _latest_release_tag()
        ver = tag[1:]

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            out_one = output / "one"
            out_one.mkdir(parents=True)
            (out_one / "tuf-control-0.2.0.tar.gz").write_bytes(b"stale")
            (out_one / f"asusius-{ver}.tar.gz").write_bytes(b"stale")

            first, digest = release.build_archive(REPO, tag, out_one)
            second, other = release.build_archive(REPO, tag, output / "two")
            self.assertEqual(
                [p.name for p in sorted(out_one.iterdir())],
                ["SHA256SUMS", first.name],
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(digest, other)
            self.assertEqual(digest, hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(
                (first.parent / "SHA256SUMS").read_text(encoding="utf-8"),
                f"{digest}  {first.name}\n",
            )
            with tarfile.open(first) as archive:
                names = archive.getnames()
            self.assertTrue(names)
            prefix = release.PROJECT_NAME + "-" + ver
            self.assertTrue(all(name == prefix or name.startswith(prefix + "/") for name in names))

            if release._is_project_repository(REPO):
                expected = re.search(r"sha256sums=\('([^']+)'\)", packaging)
                self.assertIsNotNone(expected)
                if tagged_pkgver:
                    self.assertEqual(expected[1], digest)
                else:
                    self.assertRegex(expected[1], r"^(?:SKIP|[0-9a-f]{64})$")

    def test_project_detection_is_case_insensitive(self):
        self.assertTrue(release._is_project_repository(Path("/tmp/Asusius")))
        self.assertTrue(release._is_project_repository(REPO))

    def test_invalid_tags_cannot_become_git_options_or_output_paths(self):
        with patch.object(release.subprocess, "run") as run:
            for tag in ("--all", "../main", "v1.2.3/../../bad", "main"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.build_archive(REPO, tag, Path("unused"))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
