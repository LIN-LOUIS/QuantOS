"""Deterministic, allowlisted local release-bundle construction."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib
import zipfile


_ROOT_FILES = ("LICENSE", "README.md", "pyproject.toml")
_TREES = ("docs", "scripts", "src")
_WEB_FILES = ("web/package.json", "web/package-lock.json")


class ReleaseBundleError(RuntimeError):
    """A safe release preparation error."""


def build_release_bundle(
    repository: Path, output_dir: Path, *, git_commit: str,
    build_timestamp: str,
) -> Path:
    """Create a reproducible source plus built-workspace archive.

    Runtime data is excluded by construction: only named root files, source trees,
    documentation, scripts, package metadata, and the built Workspace are eligible.
    """

    root = Path(repository).resolve()
    workspace = root / "web" / "dist"
    if not (workspace / "index.html").is_file():
        raise ReleaseBundleError(
            "WORKSPACE_ASSETS_MISSING: run `cd web && npm ci && npm run build`"
        )
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    requires_python = str(project["project"].get("requires-python", ""))
    timestamp = _timestamp(build_timestamp)
    files = _release_files(root)
    manifest = {
        "artifact_type": "quantos-local-product-bundle",
        "build_timestamp": build_timestamp,
        "frontend_build": "vite-production",
        "git_commit": git_commit,
        "python_requires": requires_python,
        "quantos_version": version,
        "workspace": "web/dist",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"quantos-{version}-{git_commit[:12]}.zip"
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
    ) as archive:
        _write(archive, "release-manifest.json", _json_bytes(manifest), timestamp)
        for relative in files:
            _write(archive, relative.as_posix(), (root / relative).read_bytes(), timestamp)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output_dir / f"{archive_path.name}.sha256").write_text(
        f"{checksum}  {archive_path.name}\n", encoding="ascii",
    )
    return archive_path


def repository_metadata(repository: Path) -> tuple[str, str]:
    """Return commit and commit time without embedding local paths."""

    root = Path(repository).resolve()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        timestamp = subprocess.run(
            ["git", "show", "-s", "--format=%cI", "HEAD"], cwd=root,
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseBundleError("GIT_METADATA_UNAVAILABLE") from error
    return commit, timestamp


def _release_files(root: Path) -> tuple[Path, ...]:
    found: set[Path] = set()
    for name in (*_ROOT_FILES, *_WEB_FILES):
        path = root / name
        if path.is_file():
            found.add(Path(name))
    for tree in _TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                found.add(path.relative_to(root))
    for path in (root / "web" / "dist").rglob("*"):
        if path.is_file():
            found.add(path.relative_to(root))
    return tuple(sorted(found, key=lambda value: value.as_posix()))


def _timestamp(value: str) -> tuple[int, int, int, int, int, int]:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    year = max(1980, parsed.year)
    return year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write(
    archive: zipfile.ZipFile, name: str, content: bytes,
    timestamp: tuple[int, int, int, int, int, int],
) -> None:
    info = zipfile.ZipInfo(name, timestamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
