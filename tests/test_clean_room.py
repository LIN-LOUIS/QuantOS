"""Deterministic Phase 8A.2 clean-room release-gate tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from quantos.clean_room import (
    AcceptanceError, Gate, create_tracked_snapshot, make_manifest, require_pass,
    run_checked, runtime_artifact_candidates, secret_candidates, snapshot_identifier,
    stage_public_export, validate_required_files,
    validate_container_inventory, validate_version_consistency,
)


def _release_tree(root: Path) -> None:
    values = {
        "pyproject.toml": '[project]\nname="quantos"\nversion="0.3.1"\n',
        "PUBLIC_EXPORT_METADATA.json": json.dumps({
            "project_version": "0.3.1", "public_parent_commit": "public-parent",
        }),
        "README.md": "# QuantOS\n", "LICENSE": "Apache-2.0\n",
        ".dockerignore": "data\n", "Dockerfile": "FROM scratch\n",
        "web/package.json": '{"version":"0.3.1"}\n',
        "web/package-lock.json": '{"version":"0.3.1","packages":{"":{"version":"0.3.1"}}}\n',
        "src/quantos/__init__.py": '__version__="0.3.1"\n',
        "src/quantos/api/app.py": "# app\n",
        "src/quantos/api/protection.py": "# guard\n",
        "public_export_manifest.txt": "README.md\npyproject.toml\n",
    }
    for name, content in values.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_git_archive_snapshot_excludes_untracked_runtime_files(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _release_tree(repository)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run([
        "git", "-c", "user.name=QuantOS Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "fixture",
    ], cwd=repository, check=True)
    (repository / ".env").write_text("TUSHARE_TOKEN=not-exported", encoding="utf-8")
    (repository / "data").mkdir()
    (repository / "data" / "market.parquet").write_bytes(b"runtime")

    snapshot = tmp_path / "snapshot"
    create_tracked_snapshot(repository, snapshot)

    assert (snapshot / "README.md").is_file()
    assert not (snapshot / ".git").exists()
    assert not (snapshot / ".env").exists()
    assert not (snapshot / "data").exists()


def test_missing_required_release_file_fails_closed(tmp_path):
    _release_tree(tmp_path)
    (tmp_path / "Dockerfile").unlink()
    with pytest.raises(AcceptanceError, match="REQUIRED_FILES"):
        validate_required_files(tmp_path)


def test_version_consistency_fails_closed(tmp_path):
    _release_tree(tmp_path)
    assert validate_version_consistency(tmp_path) == "0.3.1"
    (tmp_path / "web/package.json").write_text('{"version":"9.9.9"}', encoding="utf-8")
    with pytest.raises(AcceptanceError, match="VERSION_CONSISTENCY"):
        validate_version_consistency(tmp_path)


def test_secret_candidate_reports_path_without_value(tmp_path):
    secret = "TUSHARE_TOKEN=" + "A" * 40
    path = tmp_path / "config.txt"
    path.write_text(secret, encoding="utf-8")

    assert secret_candidates(tmp_path) == ("config.txt",)
    assert secret not in str(secret_candidates(tmp_path))


@pytest.mark.parametrize("name", (
    "data/market.parquet", "reports/ask-trace.json", "runtime.duckdb",
    "web/node_modules/package/index.js", "研发日报/private.md",
))
def test_runtime_artifact_candidates_fail_closed(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("runtime", encoding="utf-8")
    assert runtime_artifact_candidates(tmp_path)


def test_public_export_is_exact_allowlist_and_snapshot_is_deterministic(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# QuantOS\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("version='0.3.1'\n", encoding="utf-8")
    allowlist = source / "public_export_manifest.txt"
    allowlist.write_text("pyproject.toml\nREADME.md\n", encoding="utf-8")

    first = tmp_path / "first"
    second = tmp_path / "second"
    assert stage_public_export(source, first, allowlist) == (
        "README.md", "pyproject.toml",
    )
    stage_public_export(source, second, allowlist)
    assert snapshot_identifier(first) == snapshot_identifier(second)


def test_public_export_preserves_operator_executable_mode(tmp_path):
    source = tmp_path / "source"
    script = source / "scripts/preview-up.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    allowlist = source / "public_export_manifest.txt"
    allowlist.write_text("scripts/preview-up.sh\n", encoding="utf-8")

    destination = tmp_path / "public"
    stage_public_export(source, destination, allowlist)

    assert (destination / "scripts/preview-up.sh").stat().st_mode & 0o111


def test_manifest_is_deterministic_and_pass_requires_every_gate(tmp_path):
    _release_tree(tmp_path)
    gates = (Gate("source", "PASS"), Gate("docker", "PASS"))
    first = make_manifest(
        root=tmp_path, snapshot_id="public-snapshot-sha256:abc",
        python_tests=1600, frontend_tests=30, gates=gates,
    )
    second = make_manifest(
        root=tmp_path, snapshot_id="public-snapshot-sha256:abc",
        python_tests=1600, frontend_tests=30, gates=gates,
    )
    assert first.to_bytes() == second.to_bytes()
    require_pass(first)

    failed = make_manifest(
        root=tmp_path, snapshot_id="public-snapshot-sha256:abc",
        python_tests=1600, frontend_tests=30,
        gates=(Gate("source", "PASS"), Gate("health", "FAIL")),
    )
    with pytest.raises(AcceptanceError, match="health"):
        require_pass(failed)


def test_failed_external_gate_is_nonzero_and_actionable(tmp_path):
    with pytest.raises(AcceptanceError) as caught:
        run_checked(
            ["sh", "-c", "exit 7"], stage="DOCKER_BUILD", cwd=tmp_path,
            remediation="Start Docker and rebuild.",
        )
    assert caught.value.stage == "DOCKER_BUILD"
    assert "status 7" in caught.value.reason
    assert caught.value.remediation == "Start Docker and rebuild."


def test_container_inventory_rejects_runtime_data_and_credential_keys():
    validate_container_inventory(
        ("/app/web/dist/index.html", "/app/release-manifest.json"),
        ("PATH=/usr/bin", "PORT=8000"),
    )
    with pytest.raises(AcceptanceError, match="IMAGE_SECURITY"):
        validate_container_inventory(
            ("/app/data/market.parquet",), ("TUSHARE_TOKEN=redacted",),
        )
