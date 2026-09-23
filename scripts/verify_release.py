"""Validate release identity before building distributable artifacts."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

SEMVER_TAG = re.compile(
    r"v"
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-(?:"
    r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def is_semver_tag(tag: str) -> bool:
    """Return whether *tag* is a strict ``v``-prefixed SemVer tag."""
    return SEMVER_TAG.fullmatch(tag) is not None


def verify_release(tag: str, ref: str, root: Path) -> Path:
    """Validate a release and return its release-notes path."""
    if not is_semver_tag(tag):
        raise ValueError(f"release tag is not strict SemVer: {tag}")
    if ref != f"refs/tags/{tag}":
        raise ValueError(f"release must use exact tag ref refs/tags/{tag}, got {ref}")

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]
    if tag != f"v{project_version}":
        raise ValueError(f"tag {tag} does not match project version {project_version}")

    notes = root / ".github/release-notes" / f"{tag}.md"
    if not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
        raise ValueError(f"missing or empty release notes: {notes}")
    return notes


def verify_tag_ancestry(sha: str, default_branch: str, root: Path) -> None:
    """Require the tagged commit to belong to the remote default branch."""
    if COMMIT_SHA.fullmatch(sha) is None:
        raise ValueError(f"invalid release commit SHA: {sha}")

    branch_check = subprocess.run(
        ["git", "check-ref-format", "--branch", default_branch],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if branch_check.returncode != 0:
        raise ValueError(f"invalid default branch: {default_branch}")

    remote_ref = f"refs/remotes/origin/{default_branch}"
    fetch = subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{default_branch}:{remote_ref}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        raise ValueError(f"could not fetch default branch {default_branch}: {detail}")

    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, remote_ref],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode == 1:
        raise ValueError(
            f"tagged commit {sha} is not an ancestor of origin/{default_branch}"
        )
    if ancestry.returncode != 0:
        detail = ancestry.stderr.strip() or ancestry.stdout.strip()
        raise ValueError(f"could not verify tag ancestry: {detail}")


def main() -> int:
    """Run release validation from the command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--default-branch", required=True)
    args = parser.parse_args()
    try:
        notes = verify_release(args.tag, args.ref, Path.cwd())
        verify_tag_ancestry(args.sha, args.default_branch, Path.cwd())
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    print(f"release validation passed: {args.tag}; notes: {notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
