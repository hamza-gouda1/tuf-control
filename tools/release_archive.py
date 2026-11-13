#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Build a reproducible tagged source archive and SHA256SUMS without network access."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


PROJECT_NAME = "asusius"
PALETTE_REPOSITORY = "bordeaux-gnome"


def _is_project_repository(repo: Path) -> bool:
    """Accept a Asusius checkout even when its directory keeps a stale name.

    ``pyproject.toml`` declares the project identity, so a checkout of this
    repository is recognized by content rather than by directory name.
    """
    if repo.name in (PROJECT_NAME, PALETTE_REPOSITORY):
        return True
    try:
        metadata = (repo / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r'(?m)^name = "asusius"\s*$', metadata) is not None


def build_archive(repo: Path, tag: str, output: Path) -> tuple[Path, str]:
    repo = repo.resolve()
    if not _is_project_repository(repo):
        raise ValueError("expected a Asusius or Bordeaux GNOME repository")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("tag must be a vMAJOR.MINOR.PATCH version")
    ref = f"refs/tags/{tag}"
    subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo,
                   check=True, stdout=subprocess.DEVNULL)
    name = f"{repo.name}-{tag[1:]}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{name}.tar.gz"
    with tempfile.TemporaryFile() as source:
        subprocess.run(["git", "archive", "--format=tar", f"--prefix={name}/", ref],
                       cwd=repo, check=True, stdout=source)
        source.seek(0)
        with archive.open("wb") as destination:
            with gzip.GzipFile(filename="", fileobj=destination, mode="wb", mtime=0,
                               compresslevel=9) as compressed:
                shutil.copyfileobj(source, compressed)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
    return archive, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    archive, digest = build_archive(repo, args.tag, args.out)
    print(f"{digest}  {archive}")


if __name__ == "__main__":
    main()
