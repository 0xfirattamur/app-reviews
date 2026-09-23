#!/usr/bin/env python3
"""Prepare versioned files on a release branch without committing or publishing."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path

VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def _run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [*arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"could not update version in {path}")
    path.write_text(updated, encoding="utf-8")


def _update_changelog(path: Path, version: str, previous: str, date: str) -> None:
    text = path.read_text(encoding="utf-8")
    if f"## [{version}]" not in text:
        marker = re.search(r"^## \[", text, flags=re.MULTILINE)
        if marker is None:
            raise ValueError("CHANGELOG.md has no version section")
        section = (
            f"## [{version}] - {date}\n\n"
            "Describe user-visible additions, changes, fixes, and migration notes.\n\n"
        )
        text = text[: marker.start()] + section + text[marker.start() :]
    if f"[{version}]:" not in text:
        text = text.rstrip() + (
            f"\n[{version}]: https://github.com/0xfirattamur/app-reviews/compare/"
            f"v{previous}...v{version}\n"
        )
    path.write_text(text, encoding="utf-8")


def prepare(root: Path, version: str, create: bool) -> None:
    if VERSION.fullmatch(version) is None:
        raise ValueError("version must be strict X.Y.Z without leading zeroes")
    if _run(root, "git", "status", "--porcelain"):
        raise ValueError("working tree must be clean before release preparation")
    branch = _run(root, "git", "branch", "--show-current")
    target = f"release/v{version}"
    if branch in {"main", "master"}:
        raise ValueError("refusing to prepare a release directly on main")
    if branch != target:
        if not create:
            raise ValueError(f"expected branch {target}; pass --create to create it")
        existing = _run(root, "git", "branch", "--list", target)
        if existing:
            raise ValueError(f"branch {target} already exists; switch to it explicitly")
        _run(root, "git", "switch", "-c", target)

    pyproject = root / "pyproject.toml"
    current_match = re.search(
        r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE
    )
    if current_match is None:
        raise ValueError("pyproject.toml has no project version")
    previous = current_match.group(1)
    _replace_once(pyproject, r'^version = "[^"]+"', f'version = "{version}"')

    citation = root / "CITATION.cff"
    _replace_once(citation, r'^version: "[^"]+"', f'version: "{version}"')
    today = dt.date.today().isoformat()
    citation_text = citation.read_text(encoding="utf-8")
    if re.search(r"^date-released:", citation_text, re.MULTILINE):
        _replace_once(citation, r"^date-released: .*", f"date-released: {today}")
    else:
        citation.write_text(
            citation_text.rstrip() + f"\ndate-released: {today}\n", encoding="utf-8"
        )

    notes = root / f".github/release-notes/v{version}.md"
    if not notes.exists():
        notes.write_text(
            f"# app-reviews v{version}\n\n"
            "Describe highlights, breaking changes, migration, and compatibility.\n",
            encoding="utf-8",
        )
    _update_changelog(root / "CHANGELOG.md", version, previous, today)

    _run(root, "uv", "lock")
    _run(root, "uv", "lock", "--check")
    print(f"Prepared {target}; no commit, tag, push, merge, or publish was performed.")
    print("Complete the release notes, run make all, and open a reviewed PR to main.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", action="store_true")
    parser.add_argument("version")
    arguments = parser.parse_args()
    prepare(Path.cwd(), arguments.version, arguments.create)


if __name__ == "__main__":
    main()
