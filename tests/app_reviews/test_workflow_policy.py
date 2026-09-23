"""Structural supply-chain and release-gate contracts for GitHub Actions."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.verify_release import is_semver_tag, verify_release, verify_tag_ancestry

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
FULL_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SUPPORTED_RELEASE_CELLS = {
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.12"),
    ("ubuntu-latest", "3.13"),
    ("ubuntu-latest", "3.14"),
    ("macos-latest", "3.13"),
    ("windows-latest", "3.13"),
}
PYPI_PUBLISH_ACTION = (
    "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
)


def _workflow_paths() -> list[Path]:
    return sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))


def _workflow(name: str) -> tuple[dict[str, Any], str]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded, text


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _named_steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        step["name"]: step for step in _steps(job) if isinstance(step.get("name"), str)
    }


def _external_uses(workflow: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for job in workflow["jobs"].values():
        if uses := job.get("uses"):
            found.append(uses)
        for step in _steps(job):
            if uses := step.get("uses"):
                found.append(uses)
    return [uses for uses in found if not uses.startswith("./")]


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    if isinstance(value, str):
        return {value}
    return set(value)


def _dependency_closure(jobs: dict[str, Any], job_name: str) -> set[str]:
    result: set[str] = set()
    pending = list(_needs(jobs[job_name]))
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(_needs(jobs[dependency]))
    return result


def test_every_workflow_parses_and_every_external_action_is_commit_pinned() -> None:
    assert _workflow_paths()
    violations: list[str] = []
    for path in _workflow_paths():
        workflow, text = _workflow(path.name)
        for uses in _external_uses(workflow):
            action, separator, revision = uses.rpartition("@")
            matching_lines = [line for line in text.splitlines() if uses in line]
            has_version_comment = bool(matching_lines) and all(
                re.fullmatch(
                    rf"\s*(?:-\s*)?uses:\s*{re.escape(uses)}\s+#\s+v?\d[^#]*",
                    line,
                )
                for line in matching_lines
            )
            if not action or separator != "@" or not FULL_SHA.fullmatch(revision):
                violations.append(f"{path.name}: unpinned {uses}")
            elif not has_version_comment:
                violations.append(f"{path.name}: uncommented {uses}")
    assert violations == []


def test_every_setup_uv_step_uses_an_exact_tool_version() -> None:
    violations: list[str] = []
    for path in _workflow_paths():
        workflow, _ = _workflow(path.name)
        for job_name, job in workflow["jobs"].items():
            for step in _steps(job):
                if not str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    continue
                version = step.get("with", {}).get("version")
                if not isinstance(version, str) or not FULL_VERSION.fullmatch(version):
                    violations.append(f"{path.name}:{job_name}: {version!r}")
    assert violations == []


def test_release_matrix_is_complete_and_gates_build_and_publication() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    matrix = jobs["test-matrix"]["strategy"]["matrix"]["include"]
    cells = {(cell["os"], cell["python-version"]) for cell in matrix}

    assert cells == SUPPORTED_RELEASE_CELLS
    matrix_runs = [step.get("run") for step in _steps(jobs["test-matrix"])]
    assert "uv sync --locked --group dev" in matrix_runs
    assert "uv run pytest" in matrix_runs
    assert _needs(jobs["test-matrix"]) == {"validate-release"}
    assert _needs(jobs["verify"]) == {"test-matrix"}
    assert "validate-release" in _dependency_closure(jobs, "test-matrix")
    assert "validate-release" in _dependency_closure(jobs, "verify")
    assert "test-matrix" in _dependency_closure(jobs, "publish-pypi")
    assert "validate-release" in _dependency_closure(jobs, "publish-pypi")
    assert "test-matrix" in _dependency_closure(jobs, "publish-github")
    assert "validate-release" in _dependency_closure(jobs, "publish-github")


def test_release_ancestry_gate_uses_full_history_and_trusted_context() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    validate = jobs["validate-release"]
    steps = _steps(validate)
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    gate = _named_steps(validate)["Validate release identity and ancestry"]

    assert checkout["with"] == {"fetch-depth": 0}
    assert gate["env"] == {
        "RELEASE_TAG": "${{ github.ref_name }}",
        "GITHUB_DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
    }
    assert gate["run"] == (
        "python scripts/verify_release.py "
        '--tag "$RELEASE_TAG" --ref "$GITHUB_REF" '
        '--sha "$GITHUB_SHA" --default-branch "$GITHUB_DEFAULT_BRANCH"'
    )


def test_release_validation_and_verification_steps_are_ordered_structurally() -> None:
    release, _ = _workflow("release.yml")
    verify = release["jobs"]["verify"]
    steps = _steps(verify)
    named = _named_steps(verify)
    ordered_names = [step.get("name") for step in steps]

    assert verify["permissions"] == {"contents": "read"}
    assert named["Install locked release dependencies"]["run"] == (
        "uv sync --locked --group dev --group release"
    )
    for command in ("make lint", "make typecheck", "make docs"):
        assert command in [step.get("run") for step in steps]
    assert named["Build distributions once"]["run"] == (
        "uv build --no-build-isolation --out-dir dist"
    )
    build_index = ordered_names.index("Build distributions once")
    for prerequisite in (
        "Install locked release dependencies",
        "Lint",
        "Type check",
        "Build documentation",
    ):
        assert ordered_names.index(prerequisite) < build_index
    assert ordered_names.index("Inspect distribution contents") < ordered_names.index(
        "Upload immutable distributions"
    )


def test_release_checks_both_distribution_metadata_and_clean_install() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    verify_steps = _named_steps(jobs["verify"])
    assert verify_steps["Check wheel structure"]["run"] == (
        "uv run --locked --group release check-wheel-contents dist/*.whl"
    )
    assert verify_steps["Inspect distribution contents"]["run"] == (
        "uv run --locked --group release python scripts/verify_artifacts.py "
        "--dist-dir dist --project pyproject.toml "
        "--rebuilt-wheel-dir dist-from-sdist"
    )
    assert verify_steps["Smoke test installed wheel"]["run"] == (
        "uv run --locked --group release python scripts/smoke_test_wheel.py "
        '--dist-dir dist --expected-version "$RELEASE_VERSION"'
    )
    build_count = sum(
        "uv build" in str(step.get("run", "")) for step in _steps(jobs["verify"])
    )
    assert build_count == 1
    for job_name in ("publish-pypi", "publish-github"):
        assert all(
            "uv build" not in str(step.get("run", ""))
            for step in _steps(jobs[job_name])
        )


def test_release_toolchain_is_exactly_locked() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    release = pyproject["dependency-groups"]["release"]

    assert len(release) == 2
    assert all(
        re.fullmatch(r"[a-z0-9-]+==\d+(?:\.\d+)+(?:\.post\d+)?", item)
        for item in release
    )
    assert any(item.startswith("hatchling==") for item in release)
    assert any(item.startswith("check-wheel-contents==") for item in release)


def test_release_workflow_rebuilds_wheel_from_sdist_without_isolation() -> None:
    release, _ = _workflow("release.yml")
    named = _named_steps(release["jobs"]["verify"])
    rebuild = named["Rebuild wheel from sdist"]["run"]

    assert "scripts/rebuild_wheel_from_sdist.py" in rebuild
    assert "--no-build-isolation" not in rebuild  # enforced inside the tested script
    assert "dist-from-sdist" in rebuild


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX executable mode is unavailable"
)
def test_release_scripts_are_checked_in_and_executable() -> None:
    for relative in (
        "scripts/verify_artifacts.py",
        "scripts/rebuild_wheel_from_sdist.py",
        "scripts/smoke_test_wheel.py",
        "scripts/prepare_release.py",
        "scripts/release.sh",
    ):
        path = ROOT / relative
        assert path.is_file(), relative
        assert path.stat().st_mode & 0o111, relative


def test_release_publication_permissions_and_dependency_order_are_exact() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    pypi = jobs["publish-pypi"]
    github = jobs["publish-github"]

    assert _needs(pypi) == {"verify"}
    assert pypi["permissions"] == {"id-token": "write"}
    assert _needs(github) == {"publish-pypi"}
    assert github["permissions"] == {"contents": "write"}
    pypi_steps = _named_steps(pypi)
    github_steps = _named_steps(github)
    publish = pypi_steps["Publish distributions to PyPI"]
    assert publish["uses"] == PYPI_PUBLISH_ACTION
    assert publish["with"] == {"packages-dir": "dist", "attestations": True}
    assert github_steps["Create GitHub release"]["with"] == {
        "body_path": "release/release-notes.md",
        "files": "dist/*",
        "generate_release_notes": False,
    }


def test_ci_matrix_and_latest_dependency_signal_are_structural() -> None:
    ci, _ = _workflow("ci.yml")
    jobs = ci["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]["include"]
    cells = {(cell["os"], cell["python-version"]) for cell in matrix}
    latest = jobs["compatibility-latest"]
    latest_runs = [step.get("run", "") for step in _steps(latest)]

    assert cells == SUPPORTED_RELEASE_CELLS
    assert jobs["test"]["runs-on"] == "${{ matrix.os }}"
    assert latest["if"] == "github.event_name == 'schedule'"
    assert latest["continue-on-error"] is True
    assert any(
        "uv pip install" in command and "-e ." in command for command in latest_runs
    )
    assert all("--locked" not in command for command in latest_runs)


def test_release_validator_script_exists() -> None:
    assert (ROOT / "scripts/verify_release.py").is_file()


def test_release_tag_validator_accepts_strict_semver() -> None:
    valid = (
        "v0.0.0",
        "v1.0.0",
        "v1.2.3-alpha",
        "v1.2.3-alpha.1",
        "v1.2.3-0",
        "v1.2.3-x-y.z+build.01",
    )
    assert all(is_semver_tag(tag) for tag in valid)


def test_release_tag_validator_rejects_leading_zero_and_malformed_versions() -> None:
    invalid = (
        "1.0.0",
        "v01.0.0",
        "v1.01.0",
        "v1.0.01",
        "v1.0",
        "v1.0.0-01",
        "v1.0.0-alpha.01",
        "v1.0.0-alpha..1",
        "v1.0.0+",
    )
    assert not any(is_semver_tag(tag) for tag in invalid)


def test_release_validator_requires_matching_ref_project_and_nonempty_notes(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app-reviews"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    notes_dir = tmp_path / ".github/release-notes"
    notes_dir.mkdir(parents=True)
    notes = notes_dir / "v1.2.3.md"
    notes.write_text("# v1.2.3\n", encoding="utf-8")

    assert verify_release("v1.2.3", "refs/tags/v1.2.3", tmp_path) == notes
    with pytest.raises(ValueError, match="tag ref"):
        verify_release("v1.2.3", "refs/heads/main", tmp_path)
    with pytest.raises(ValueError, match="project version"):
        verify_release("v1.2.4", "refs/tags/v1.2.4", tmp_path)
    notes.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="release notes"):
        verify_release("v1.2.3", "refs/tags/v1.2.3", tmp_path)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_release_ancestry_validator_rejects_an_off_main_tag(tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    remote.mkdir()
    repository.mkdir()
    _git(remote, "init", "--bare")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.invalid")
    (repository / "tracked.txt").write_text("main\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "main")
    main_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    _git(repository, "switch", "-c", "off-main")
    (repository / "tracked.txt").write_text("off main\n", encoding="utf-8")
    _git(repository, "commit", "-am", "off main")
    off_main_sha = _git(repository, "rev-parse", "HEAD")

    verify_tag_ancestry(main_sha, "main", repository)
    with pytest.raises(ValueError, match="invalid default branch"):
        verify_tag_ancestry(main_sha, "--upload-pack=unsafe", repository)
    with pytest.raises(ValueError, match="invalid release commit SHA"):
        verify_tag_ancestry("not-a-sha", "main", repository)
    with pytest.raises(ValueError, match="not an ancestor"):
        verify_tag_ancestry(off_main_sha, "main", repository)
