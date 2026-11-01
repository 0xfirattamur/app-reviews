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
EXPRESSION = re.compile(r"\$\{\{")


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


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in _steps(job) if "run" in step]


def _step_index(job: dict[str, Any], marker: str) -> int:
    """Index of the first step whose command or action contains `marker`."""
    for index, step in enumerate(_steps(job)):
        if marker in str(step.get("run", "")) + str(step.get("uses", "")):
            return index
    raise AssertionError(f"no step mentions {marker!r}")


def _steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    """The job's steps that run `action` (`owner/name`), at whatever revision."""
    prefix = f"{action}@"
    return [
        step for step in _steps(job) if str(step.get("uses", "")).startswith(prefix)
    ]


def _is_exact_version(version: object) -> bool:
    """Whether `version` names one release, `X.Y.Z`, not a range or an alias."""
    return isinstance(version, str) and FULL_VERSION.fullmatch(version) is not None


def _job_writes(job: dict[str, Any]) -> set[str]:
    permissions = job.get("permissions", {})
    assert isinstance(permissions, dict)
    return {scope for scope, level in permissions.items() if level == "write"}


def _publish_jobs(jobs: dict[str, Any]) -> list[str]:
    return [name for name, job in jobs.items() if _job_writes(job)]


def _all_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """Every job of every workflow, as (workflow file, job name, job)."""
    all_jobs: list[tuple[str, str, dict[str, Any]]] = []
    for path in _workflow_paths():
        workflow, _ = _workflow(path.name)
        for job_name, job in workflow["jobs"].items():
            all_jobs.append((path.name, job_name, job))
    return all_jobs


def _all_commands(workflow: dict[str, Any]) -> list[str]:
    """Every shell command run by any job of `workflow`."""
    commands: list[str] = []
    for job in workflow["jobs"].values():
        commands.extend(_runs(job))
    return commands


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


def test_no_workflow_uses_pull_request_target() -> None:
    for path in _workflow_paths():
        workflow, _ = _workflow(path.name)
        # PyYAML reads the bare `on` key as boolean True.
        triggers = workflow.get("on", workflow.get(True))
        names = triggers if isinstance(triggers, (dict, list)) else [triggers]
        assert "pull_request_target" not in names, path.name


def test_every_workflow_declares_explicit_top_level_permissions() -> None:
    for path in _workflow_paths():
        workflow, _ = _workflow(path.name)
        permissions = workflow.get("permissions")
        assert isinstance(permissions, dict), f"{path.name}: permissions missing"
        assert permissions.get("contents") in {"read", "none"}, path.name


def test_docs_deploy_never_persists_the_write_token_in_the_checkout() -> None:
    docs, _ = _workflow("docs.yml")
    for job in docs["jobs"].values():
        checkouts = _steps_using(job, "actions/checkout")
        assert checkouts
        assert all(
            step.get("with", {}).get("persist-credentials") is False
            for step in checkouts
        )


def test_only_the_publish_jobs_hold_publication_privileges() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    publishing = _publish_jobs(jobs)

    assert publishing
    for name in publishing:
        assert _job_writes(jobs[name]) <= {"id-token", "contents"}, name
    for workflow_file, job_name, job in _all_jobs():
        if workflow_file != "release.yml":
            assert "id-token" not in _job_writes(job), f"{workflow_file}:{job_name}"


def test_pypi_publish_job_holds_only_the_oidc_permission() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    pypi_jobs = [
        name
        for name, job in jobs.items()
        if _steps_using(job, "pypa/gh-action-pypi-publish")
    ]

    assert len(pypi_jobs) == 1
    job = jobs[pypi_jobs[0]]
    assert job["permissions"] == {"id-token": "write"}
    publish = _steps_using(job, "pypa/gh-action-pypi-publish")[0]
    assert publish["with"]["attestations"] is True


def test_expressions_reach_shell_commands_only_through_env() -> None:
    violations = [
        f"{workflow_file}:{job_name}"
        for workflow_file, job_name, job in _all_jobs()
        if any(EXPRESSION.search(command) for command in _runs(job))
    ]
    assert violations == []


def test_every_setup_uv_step_uses_an_exact_tool_version() -> None:
    violations: list[str] = []
    for workflow_file, job_name, job in _all_jobs():
        setups = _steps_using(job, "astral-sh/setup-uv")
        versions = [step.get("with", {}).get("version") for step in setups]
        violations.extend(
            f"{workflow_file}:{job_name}: {version!r}"
            for version in versions
            if not _is_exact_version(version)
        )
    assert violations == []


def test_release_matrix_is_complete_and_gates_build_and_publication() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    matrix_jobs = [
        name
        for name, job in jobs.items()
        if any("pytest" in command for command in _runs(job))
    ]
    assert len(matrix_jobs) == 1
    matrix = jobs[matrix_jobs[0]]["strategy"]["matrix"]["include"]

    assert {(cell["os"], cell["python-version"]) for cell in matrix} == (
        SUPPORTED_RELEASE_CELLS
    )
    for publisher in _publish_jobs(jobs):
        assert matrix_jobs[0] in _dependency_closure(jobs, publisher)


def test_release_identity_gate_runs_on_full_history_and_precedes_everything() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    gates = [
        name
        for name, job in jobs.items()
        if any("scripts/verify_release.py" in command for command in _runs(job))
    ]
    assert len(gates) == 1
    gate = jobs[gates[0]]
    checkout = _steps_using(gate, "actions/checkout")[0]

    assert checkout["with"]["fetch-depth"] == 0
    assert _needs(gate) == set()
    for name in jobs:
        if name != gates[0]:
            assert gates[0] in _dependency_closure(jobs, name), name


def test_release_verification_job_builds_once_and_gates_every_publisher() -> None:
    release, _ = _workflow("release.yml")
    jobs = release["jobs"]
    verifiers = [
        name
        for name, job in jobs.items()
        if any("scripts/verify_artifacts.py" in command for command in _runs(job))
    ]
    assert len(verifiers) == 1
    verify = jobs[verifiers[0]]

    assert verify["permissions"] == {"contents": "read"}
    assert sum("uv build" in command for command in _runs(verify)) == 1
    build = _step_index(verify, "uv build")
    assert build < _step_index(verify, "scripts/verify_artifacts.py")
    assert build < _step_index(verify, "actions/upload-artifact@")
    for publisher in _publish_jobs(jobs):
        assert verifiers[0] in _dependency_closure(jobs, publisher)
        assert all("uv build" not in command for command in _runs(jobs[publisher]))


def test_release_verification_checks_wheel_smoke_and_rebuild_from_sdist() -> None:
    release, _ = _workflow("release.yml")
    commands = " ".join(_all_commands(release))

    for script in (
        "check-wheel-contents",
        "scripts/rebuild_wheel_from_sdist.py",
        "scripts/smoke_test_wheel.py",
    ):
        assert script in commands
    assert "--no-build-isolation" not in commands.split("rebuild_wheel_from_sdist")[1]


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


def test_ci_tests_every_supported_python_and_platform() -> None:
    ci, _ = _workflow("ci.yml")
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]["include"]

    assert {(cell["os"], cell["python-version"]) for cell in matrix} == (
        SUPPORTED_RELEASE_CELLS
    )


def test_latest_dependency_signal_runs_only_on_schedule_without_the_lockfile() -> None:
    ci, _ = _workflow("ci.yml")
    latest = ci["jobs"]["compatibility-latest"]

    assert latest["if"] == "github.event_name == 'schedule'"
    assert latest["continue-on-error"] is True
    assert all("--locked" not in command for command in _runs(latest))


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
