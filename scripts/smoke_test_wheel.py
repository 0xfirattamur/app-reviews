#!/usr/bin/env python3
"""Install the built wheel in a clean environment and run an offline smoke test."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SMOKE = r"""
import sys
from datetime import UTC, datetime
from pathlib import Path

import app_reviews
from app_reviews import Review

expected = sys.argv[1]
assert app_reviews.__version__ == expected, (app_reviews.__version__, expected)
assert "site-packages" in str(Path(app_reviews.__file__).resolve())
review = Review(
    store="appstore",
    app_id="offline-smoke",
    country="us",
    rating=5,
    title="Installed wheel",
    body="No network required",
    author_name="release-workflow",
    source="appstore_scraper",
    id="offline-smoke-review",
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)
assert review.to_dict()["store"] == "appstore"
"""


def _python_in(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts/python.exe"
    return venv / "bin/python"


def smoke_test(dist_dir: Path, expected_version: str) -> None:
    expected_version = expected_version.removeprefix("v")
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one wheel in {dist_dir}: {wheels}")
    wheel = wheels[0].resolve()
    with tempfile.TemporaryDirectory(prefix="app-reviews-wheel-") as temporary:
        root = Path(temporary)
        venv = root / "venv"
        requirements = root / "runtime-requirements.txt"
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(venv)], check=True
        )
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            check=True,
        )
        python = _python_in(venv)
        # Not --offline: `uv sync` caches by lockfile URL, which `uv pip install`
        # cannot read, so offline resolution fails on a cold CI cache. The
        # exported requirements are hash-pinned from uv.lock, so integrity holds.
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--requirement",
                str(requirements),
            ],
            check=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
            ],
            check=True,
        )
        environment = {
            **os.environ,
            "PIP_NO_INDEX": "1",
            "UV_OFFLINE": "1",
        }
        subprocess.run(
            [str(python), "-I", "-c", SMOKE, expected_version],
            cwd=root,
            env=environment,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    arguments = parser.parse_args()
    smoke_test(arguments.dist_dir, arguments.expected_version)


if __name__ == "__main__":
    main()
