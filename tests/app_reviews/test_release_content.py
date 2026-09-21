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

    assert project["version"] == "1.0.0"
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
    assert package["version"] == "1.0.0"


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


def test_readme_documents_bounded_automation_without_claiming_a_tool_server() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Agents and automation" in readme
    assert "Scrape App Store and Google Play reviews in Python" in readme
    assert "result.to_dict()" in readme
    assert "max_pages=" in readme
    assert "with AppStoreReviews() as client:" in readme
    assert "MCP server" not in readme
    assert "command-line interface" not in readme
    assert "<summary>" not in readme


def test_readme_does_not_index_an_empty_export() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "fieldnames=list(rows[0])" not in readme


def test_v1_release_notes_and_changelog_exist() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = (ROOT / ".github/release-notes/v1.0.0.md").read_text(encoding="utf-8")

    assert "## [1.0.0]" in changelog
    assert "## Breaking changes" in notes
    assert "Migration" in notes
    assert "FetchResult.to_dict()" in notes
    assert "RequestError" in notes
    assert "ReviewProvider" in notes


def test_public_docs_do_not_claim_custom_provider_client_integration() -> None:
    public = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "CHANGELOG.md",
            ROOT / ".github/release-notes/v1.0.0.md",
        )
    ).lower()
    assert "provider-extension" not in public
    assert "provider extensions" not in public
    assert "custom providers must" not in public
    assert "plug custom" not in public


def test_google_play_review_docs_reject_country_but_search_keeps_storefront() -> None:
    public = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    )
    assert "Google Play review clients reject `country` and `countries`" in public
    assert "GooglePlayReviews().fetch" not in public
    assert "GooglePlayReviews().afetch" not in public
    assert "GooglePlayReviews().iter" not in public
    assert "GooglePlayReviews().aiter" not in public
    assert "GooglePlayReviews().fetch(app.app_id, countries=" not in public
    assert "Google Play search and metadata" in public
    assert "storefront" in public


def test_timestamp_docs_match_preserved_aware_offsets() -> None:
    public = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "CHANGELOG.md",
            ROOT / ".github/release-notes/v1.0.0.md",
            ROOT / "docs/reference/models.md",
        )
    )
    assert "timestamps are normalized to timezone-aware UTC" not in public
    assert "Naive timestamps get UTC attached" in public
    assert "aware offsets are preserved" in public


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
    assert 'version: "1.0.0"' in citation
    assert "repository-code: https://github.com/0xfirattamur/app-reviews" in citation


def test_comparison_covers_the_audited_options_and_decision_rows() -> None:
    comparison = (ROOT / "docs/comparison.md").read_text(encoding="utf-8")
    for option in (
        "`app-reviews`",
        "`google-play-scraper`",
        "`app-store-scraper`",
        "`app-store-web-scraper`",
        "Apple RSS directly",
        "App Store Connect directly",
        "Google Play Developer API directly",
    ):
        assert option in comparison
    for capability in (
        "Both stores",
        "Maintained",
        "Native async",
        "Typed interface",
        "Country behavior",
        "Resumable cursor exposed",
        "Runtime dependency count",
        "CLI",
    ):
        assert capability in comparison
    assert "## When not to use app-reviews" in comparison


def test_comparison_does_not_claim_third_party_pep561_markers() -> None:
    comparison = (ROOT / "docs/comparison.md").read_text(encoding="utf-8")
    typed_row = next(
        line for line in comparison.splitlines() if line.startswith("| Typed interface")
    )
    cells = [cell.strip() for cell in typed_row.strip("|").split("|")]
    headings = [
        cell.strip()
        for cell in next(
            line
            for line in comparison.splitlines()
            if line.startswith("| Capability |")
        )
        .strip("|")
        .split("|")
    ]
    typed_by_option = dict(zip(headings, cells, strict=True))

    assert "Ships `py.typed`" in typed_by_option["`app-reviews`"]
    for option in ("`google-play-scraper`", "`app-store-web-scraper`"):
        claim = typed_by_option[option]
        assert "Ships `py.typed`" not in claim
        assert "no `py.typed` marker" in claim


def test_model_reference_documents_the_v1_result_and_error_contracts() -> None:
    models = (ROOT / "docs/reference/models.md").read_text(encoding="utf-8")
    internals = (ROOT / "docs/reference/how-it-works.md").read_text(encoding="utf-8")

    assert '| `"request"` |' in models
    assert "`RequestError`" in models
    assert "permanent" in models
    assert "401/403" in models
    assert "credential-free" in models
    assert "official" in models
    assert "`RequestError`" in internals
    assert "401/403" in internals
    assert "credential-free" in internals.lower()

    assert "`FetchResult.to_dict(include_raw=False)`" in models
    assert "`skipped_reviews`" in models
    assert "`CountryOutcome.to_dict()`" in models
    assert "`PageResult.to_dict(include_raw=False)`" in models
    assert "`max_pages=`" in models
    assert "Raise `MAX_PAGES`" not in models
    assert "`tuple[int, ...]`" in models
    assert "`(500, 502, 503, 504, 429)`" in models


def test_metadata_price_docs_do_not_guess_currency() -> None:
    models = " ".join(
        (ROOT / "docs/reference/models.md").read_text(encoding="utf-8").split()
    )
    assert "formatted with `$` regardless of storefront" not in models
    assert "localized formatted price" in models
    assert "Absent price data" in models
    assert 'numeric zero are `"Free"`' in models
    assert "positive numeric amount" in models
    assert "non-empty ISO currency code" in models
    assert "Malformed or non-finite amounts" in models
    assert "positive amounts with a missing currency" in models


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
