"""Public release metadata and documentation contracts for v1.0.0."""

from __future__ import annotations

import subprocess
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/0xfirattamur/app-reviews"
DOCUMENTATION = "https://0xfirattamur.github.io/app-reviews/"


def _project() -> dict[str, object]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]


def test_v1_metadata_is_stable_and_discoverable() -> None:
    project = _project()

    assert project["version"] == "1.3.0"
    assert project["description"] == (
        "Typed sync and async Python client for App Store and Google Play "
        "reviews, app search, metadata, and resumable cursors"
    )
    assert "Development Status :: 5 - Production/Stable" in project["classifiers"]
    assert "Development Status :: 3 - Alpha" not in project["classifiers"]
    assert {
        "app-store",
        "google-play",
        "app-reviews",
        "review-scraper",
        "asyncio",
    } <= set(project["keywords"])

    assert project["urls"] == {
        "Homepage": REPOSITORY,
        "Documentation": DOCUMENTATION,
        "Changelog": f"{REPOSITORY}/blob/main/CHANGELOG.md",
        "Repository": REPOSITORY,
        "Issues": f"{REPOSITORY}/issues",
    }


def test_lockfile_carries_the_root_v1_version() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == "app-reviews")
    assert package["version"] == "1.3.0"


def test_public_text_has_no_stale_owner_links() -> None:
    public_files = [
        ROOT / name
        for name in (
            "README.md",
            "CHANGELOG.md",
            "CITATION.cff",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "mkdocs.yml",
            "pyproject.toml",
            "uv.lock",
        )
    ]
    public_files.extend((ROOT / "docs").rglob("*.md"))
    public_files.extend((ROOT / ".github").rglob("*.md"))
    public_files.extend((ROOT / ".github").rglob("*.yml"))
    stale_owner = b"firat" + b"tamurcw"
    stale = [
        str(path.relative_to(ROOT))
        for path in public_files
        if path.is_file() and stale_owner in path.read_bytes()
    ]
    assert sorted(stale) == []


def test_every_public_docs_page_has_a_description() -> None:
    missing: list[str] = []
    for path in (ROOT / "docs").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        front_matter = text.split("---", 2)
        if len(front_matter) < 3 or "description:" not in front_matter[1]:
            missing.append(str(path.relative_to(ROOT)))
    assert missing == []


def test_docs_discovery_plugins_and_local_public_pages_are_configured() -> None:
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = pyproject["dependency-groups"]["dev"]
    assert "- search" in config
    assert "- social:" in config
    assert "- llmstxt:" in config
    assert "Changelog: changelog.md" in config
    assert "Contributing: contributing.md" in config
    assert (ROOT / "docs/changelog.md").is_file()
    assert (ROOT / "docs/contributing.md").is_file()
    assert any(item.startswith("mkdocs-material[imaging]") for item in dev_dependencies)
    assert any(item.startswith("mkdocs-llmstxt") for item in dev_dependencies)


def test_social_cards_require_an_explicit_opt_in() -> None:
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "enabled: !ENV [ENABLE_SOCIAL_CARDS, false]" in config
    assert "enabled: !ENV [CI, false]" not in config


def test_docs_deploy_installs_social_card_libraries_and_opts_in() -> None:
    workflow = (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8")
    for package in (
        "libcairo2-dev",
        "libfreetype6-dev",
        "libffi-dev",
        "libjpeg-dev",
        "libpng-dev",
        "zlib1g-dev",
    ):
        assert package in workflow
    assert workflow.count('ENABLE_SOCIAL_CARDS: "true"') == 2
    assert "uv run mkdocs build --strict" in workflow
    assert "uv run mkdocs gh-deploy --force" in workflow


def test_citation_metadata_is_present_and_versioned() -> None:
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    assert "cff-version: 1.2.0" in citation
    assert "title: app-reviews" in citation
    assert 'version: "1.3.0"' in citation
    assert "repository-code: https://github.com/0xfirattamur/app-reviews" in citation


def test_sdist_excludes_generated_workspace_content(tmp_path: Path) -> None:
    sentinels = [
        ROOT / ".cache/release-artifact-sentinel",
        ROOT / ".coverage-release-sentinel",
        ROOT / ".mypy_cache/release-artifact-sentinel",
        ROOT / ".pytest_cache/release-artifact-sentinel",
        ROOT / ".ruff_cache/release-artifact-sentinel",
        ROOT / ".venv/release-artifact-sentinel",
        ROOT / "dist/release-artifact-sentinel",
        ROOT / "site/release-artifact-sentinel",
    ]
    try:
        for sentinel in sentinels:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("must not ship", encoding="utf-8")
        subprocess.run(
            ["uv", "build", "--sdist", "--out-dir", str(tmp_path)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        archive = next(tmp_path.glob("app_reviews-*.tar.gz"))
        with tarfile.open(archive, "r:gz") as built:
            names = built.getnames()
    finally:
        for sentinel in sentinels:
            sentinel.unlink(missing_ok=True)

    relative_names = [name.split("/", 1)[1] for name in names]
    assert "README.md" in relative_names
    assert "CHANGELOG.md" in relative_names
    assert "CITATION.cff" in relative_names
    assert "docs/index.md" in relative_names
    assert "tests/app_reviews/test_release_content.py" in relative_names
    assert not any("release-artifact-sentinel" in name for name in relative_names)
    assert not any(
        name == blocked or name.startswith(f"{blocked}/")
        for name in relative_names
        for blocked in (
            ".cache",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "dist",
            "site",
        )
    )
    assert not any(name.startswith(".coverage") for name in relative_names)
