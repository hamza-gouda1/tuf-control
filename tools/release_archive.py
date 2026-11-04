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


def build_archive(repo: Path, tag: str, output: Path) -> tuple[Path, str]:
    repo = repo.resolve()
    if repo.name not in ("tuf-control", "bordeaux-gnome"):
        raise ValueError("expected a TUF Control or Bordeaux GNOME repository")
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
