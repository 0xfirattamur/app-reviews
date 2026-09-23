#!/usr/bin/env python3
"""Validate release archives and an optional wheel rebuilt from the sdist."""

from __future__ import annotations

import argparse
import hashlib
import tarfile
import tomllib
import zipfile
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

FORBIDDEN_PARTS = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "dist",
    "site",
}


def _one(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {label} in {directory}: {matches}")
    return matches[0]


def _metadata(data: bytes, label: str) -> Message:
    parsed = BytesParser(policy=policy.default).parsebytes(data)
    for field in ("Metadata-Version", "Name", "Version", "Requires-Python"):
        if not parsed[field]:
            raise ValueError(f"{label} metadata is missing {field}")
    return parsed


def _verify_identity(metadata: Message, name: str, version: str, label: str) -> None:
    if metadata["Name"] != name:
        raise ValueError(f"{label} name is {metadata['Name']!r}, expected {name!r}")
    if metadata["Version"] != version:
        raise ValueError(
            f"{label} version is {metadata['Version']!r}, expected {version!r}"
        )


def _wheel_payload(wheel: Path, name: str, version: str) -> dict[str, str]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"wheel has corrupt member: {bad}")
            names = archive.namelist()
            metadata_names = [
                item for item in names if item.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError(f"wheel must contain one METADATA: {metadata_names}")
            _verify_identity(
                _metadata(archive.read(metadata_names[0]), "wheel"),
                name,
                version,
                "wheel",
            )
            for required in ("app_reviews/__init__.py", "app_reviews/py.typed"):
                if required not in names:
                    raise ValueError(f"wheel is missing {required}")
            return {
                item: hashlib.sha256(archive.read(item)).hexdigest()
                for item in names
                if not item.endswith(".dist-info/RECORD")
            }
    except zipfile.BadZipFile as error:
        raise ValueError(f"invalid wheel archive: {wheel}") from error


def _sdist_payload(sdist: Path, name: str, version: str) -> Message:
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            pkg_info = [item for item in names if item.endswith("/PKG-INFO")]
            if len(pkg_info) != 1:
                raise ValueError(f"sdist must contain one PKG-INFO: {pkg_info}")
            stream = archive.extractfile(pkg_info[0])
            if stream is None:
                raise ValueError("sdist PKG-INFO is not readable")
            metadata = _metadata(stream.read(), "sdist")
            _verify_identity(metadata, name, version, "sdist")
            if not any(item.endswith("/src/app_reviews/__init__.py") for item in names):
                raise ValueError("sdist is missing app_reviews sources")
            for item in names:
                relative = Path(item).parts[1:]
                if any(part in FORBIDDEN_PARTS for part in relative):
                    raise ValueError(f"generated workspace content leaked: {item}")
                if any(part.startswith(".coverage") for part in relative):
                    raise ValueError(f"coverage data leaked: {item}")
            return metadata
    except tarfile.TarError as error:
        raise ValueError(f"invalid sdist archive: {sdist}") from error


def verify_artifacts(
    dist_dir: Path, project_file: Path, rebuilt_wheel_dir: Path | None = None
) -> None:
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    name = project["name"]
    version = project["version"]
    wheel = _one(dist_dir, "*.whl", "wheel")
    sdist = _one(dist_dir, "*.tar.gz", "sdist")
    wheel_payload = _wheel_payload(wheel, name, version)
    sdist_metadata = _sdist_payload(sdist, name, version)

    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            item for item in archive.namelist() if item.endswith(".dist-info/METADATA")
        )
        wheel_metadata = _metadata(archive.read(metadata_name), "wheel")
    for field in ("Requires-Python", "Requires-Dist", "Project-URL"):
        if wheel_metadata.get_all(field, []) != sdist_metadata.get_all(field, []):
            raise ValueError(f"wheel and sdist disagree on {field}")

    if rebuilt_wheel_dir is not None:
        rebuilt = _one(rebuilt_wheel_dir, "*.whl", "rebuilt wheel")
        rebuilt_payload = _wheel_payload(rebuilt, name, version)
        if rebuilt_payload != wheel_payload:
            missing = sorted(wheel_payload.keys() - rebuilt_payload.keys())
            extra = sorted(rebuilt_payload.keys() - wheel_payload.keys())
            changed = sorted(
                item
                for item in wheel_payload.keys() & rebuilt_payload.keys()
                if wheel_payload[item] != rebuilt_payload[item]
            )
            raise ValueError(
                "wheel rebuilt from sdist differs from source wheel: "
                f"missing={missing}, extra={extra}, changed={changed}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--rebuilt-wheel-dir", type=Path)
    arguments = parser.parse_args()
    verify_artifacts(arguments.dist_dir, arguments.project, arguments.rebuilt_wheel_dir)


if __name__ == "__main__":
    main()
