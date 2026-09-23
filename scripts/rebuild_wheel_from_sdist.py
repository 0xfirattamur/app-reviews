#!/usr/bin/env python3
"""Rebuild a wheel from the generated sdist using the locked local backend."""

from __future__ import annotations

import argparse
import subprocess
import tarfile
import tempfile
from pathlib import Path


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"sdist member escapes extraction root: {member.name}")
    archive.extractall(destination, filter="data")


def rebuild(dist_dir: Path, output_dir: Path) -> Path:
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise ValueError(f"expected exactly one sdist in {dist_dir}: {sdists}")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="app-reviews-sdist-") as temporary:
        extracted = Path(temporary)
        with tarfile.open(sdists[0], "r:gz") as archive:
            _safe_extract(archive, extracted)
        roots = [item for item in extracted.iterdir() if item.is_dir()]
        if len(roots) != 1:
            raise ValueError(f"sdist must have one root directory: {roots}")
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--no-build-isolation",
                "--offline",
                "--out-dir",
                str(output_dir.resolve()),
                str(roots[0]),
            ],
            check=True,
        )
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected one rebuilt wheel in {output_dir}: {wheels}")
    return wheels[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    rebuild(arguments.dist_dir, arguments.output_dir)


if __name__ == "__main__":
    main()
