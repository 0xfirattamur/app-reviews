"""Executable release tooling tests against actual distribution archives."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run(
    *arguments: str, cwd: Path = ROOT, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*arguments], cwd=cwd, check=check, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("release-dist")
    _run(
        "uv",
        "run",
        "--locked",
        "--group",
        "release",
        "uv",
        "build",
        "--no-build-isolation",
        "--out-dir",
        str(output),
    )
    return output


def test_exact_artifact_verifier_accepts_real_build(built_dist: Path) -> None:
    rebuilt = built_dist.parent / "rebuilt"
    _run(
        "uv",
        "run",
        "--locked",
        "--group",
        "release",
        "python",
        "scripts/rebuild_wheel_from_sdist.py",
        "--dist-dir",
        str(built_dist),
        "--output-dir",
        str(rebuilt),
    )
    _run(
        "uv",
        "run",
        "--locked",
        "--group",
        "release",
        "python",
        "scripts/verify_artifacts.py",
        "--dist-dir",
        str(built_dist),
        "--project",
        "pyproject.toml",
        "--rebuilt-wheel-dir",
        str(rebuilt),
    )


@pytest.mark.parametrize("damage", ["missing-source", "wrong-version", "corrupt"])
def test_artifact_verifier_rejects_bad_wheels(
    built_dist: Path, tmp_path: Path, damage: str
) -> None:
    sdist = next(built_dist.glob("*.tar.gz"))
    wheel = next(built_dist.glob("*.whl"))
    shutil.copy2(sdist, tmp_path / sdist.name)
    target = tmp_path / wheel.name

    if damage == "corrupt":
        target.write_bytes(b"not a zip archive")
    else:
        with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(target, "w") as output:
            for member in source.infolist():
                data = source.read(member.filename)
                if (
                    damage == "missing-source"
                    and member.filename == "app_reviews/__init__.py"
                ):
                    continue
                if damage == "wrong-version" and member.filename.endswith(
                    ".dist-info/METADATA"
                ):
                    data = data.replace(b"Version: 1.0.0", b"Version: 9.9.9")
                output.writestr(member, data)

    completed = _run(
        "uv",
        "run",
        "--locked",
        "--group",
        "release",
        "python",
        "scripts/verify_artifacts.py",
        "--dist-dir",
        str(tmp_path),
        "--project",
        "pyproject.toml",
        check=False,
    )
    assert completed.returncode != 0


def test_offline_smoke_script_executes_real_wheel(built_dist: Path) -> None:
    _run(
        "uv",
        "run",
        "--locked",
        "--group",
        "release",
        "python",
        "scripts/smoke_test_wheel.py",
        "--dist-dir",
        str(built_dist),
        "--expected-version",
        "1.0.0",
    )


def test_offline_smoke_script_rejects_wrong_expected_version(built_dist: Path) -> None:
    completed = _run(
        "uv",
        "run",
        "--locked",
        "--group",
        "release",
        "python",
        "scripts/smoke_test_wheel.py",
        "--dist-dir",
        str(built_dist),
        "--expected-version",
        "9.9.9",
        check=False,
    )
    assert completed.returncode != 0


def _git(repository: Path, *arguments: str) -> str:
    return _run("git", *arguments, cwd=repository).stdout.strip()


def _release_fixture(tmp_path: Path, branch: str = "develop") -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", branch)
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.invalid")
    for relative in (
        "scripts/release.sh",
        "scripts/prepare_release.py",
        "scripts/verify_release.py",
    ):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "app-reviews"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (repository / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "app-reviews"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (repository / "CITATION.cff").write_text(
        'cff-version: 1.2.0\ntitle: app-reviews\nversion: "1.0.0"\n'
        "date-released: 2026-09-22\n",
        encoding="utf-8",
    )
    (repository / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.0.0] - 2026-09-22\n\nStable.\n\n"
        "[1.0.0]: https://example.invalid/compare/v0.6.0...v1.0.0\n",
        encoding="utf-8",
    )
    notes = repository / ".github/release-notes"
    notes.mkdir(parents=True)
    (notes / "v1.0.0.md").write_text("# app-reviews v1.0.0\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import re, sys\n"
        "if sys.argv[1] == 'lock':\n"
        "    lock = Path.cwd() / 'uv.lock'\n"
        "    project = (Path.cwd() / 'pyproject.toml').read_text()\n"
        '    version = re.search(r\'^version = "([^"]+)"\', '
        "project, re.M).group(1)\n"
        "    text = lock.read_text()\n"
        '    pattern = r\'(name = "app-reviews"\\nversion = ")[^"]+\'\n'
        "    lock.write_text(re.sub(pattern, r'\\g<1>' + version, text))\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    return repository, bin_dir


def _prepare(
    repository: Path, bin_dir: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["./scripts/release.sh", *arguments],
        cwd=repository,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="release.sh is POSIX-only")
def test_release_script_prepares_branch_and_public_versions_without_mutating_history(
    tmp_path: Path,
) -> None:
    repository, bin_dir = _release_fixture(tmp_path)
    before = _git(repository, "rev-parse", "HEAD")

    completed = _prepare(repository, bin_dir, "--create", "1.1.0")

    assert completed.returncode == 0, completed.stderr
    assert _git(repository, "branch", "--show-current") == "release/v1.1.0"
    assert _git(repository, "rev-parse", "HEAD") == before
    assert _git(repository, "tag") == ""
    assert 'version = "1.1.0"' in (repository / "pyproject.toml").read_text()
    assert 'version = "1.1.0"' in (repository / "uv.lock").read_text()
    assert 'version: "1.1.0"' in (repository / "CITATION.cff").read_text()
    assert (repository / ".github/release-notes/v1.1.0.md").is_file()
    assert "## [1.1.0]" in (repository / "CHANGELOG.md").read_text()


@pytest.mark.parametrize(
    ("branch", "arguments", "dirty", "message"),
    [
        ("main", ("--create", "1.1.0"), False, "main"),
        ("develop", ("1.1.0",), False, "release/v1.1.0"),
        ("develop", ("--create", "01.1.0"), False, "version"),
        ("develop", ("--create", "1.1.0"), True, "clean"),
    ],
)
@pytest.mark.skipif(sys.platform == "win32", reason="release.sh is POSIX-only")
def test_release_script_fails_closed(
    tmp_path: Path,
    branch: str,
    arguments: tuple[str, ...],
    dirty: bool,
    message: str,
) -> None:
    repository, bin_dir = _release_fixture(tmp_path, branch)
    if dirty:
        (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    completed = _prepare(repository, bin_dir, *arguments)
    assert completed.returncode != 0
    assert message in (completed.stdout + completed.stderr).lower()
    assert _git(repository, "tag") == ""
