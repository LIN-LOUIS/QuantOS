"""Deterministic Phase 8A.2 clean-room release-gate tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

import quantos.clean_room as clean_room
from quantos.clean_room import (
    AcceptanceError, Gate, create_tracked_snapshot, make_manifest, require_pass,
    parse_frontend_count, run_checked, runtime_artifact_candidates, secret_candidates, snapshot_identifier,
    stage_public_export, validate_required_files,
    validate_container_inventory, validate_version_consistency,
)


def _release_tree(root: Path) -> None:
    values = {
        "pyproject.toml": '[project]\nname="quantos"\nversion="0.3.1"\n',
        "PUBLIC_EXPORT_METADATA.json": json.dumps({
            "project_version": "0.3.1", "public_parent_commit": "public-parent",
            "accepted_source_snapshot_id": "public-snapshot-sha256:source",
            "public_payload_snapshot_id": "public-payload-sha256:candidate",
        }),
        "PUBLIC_SOURCE_ACCEPTANCE.json": json.dumps({
            "schema_version": "quantos-clean-room-acceptance-v1",
            "release_snapshot_id": "public-snapshot-sha256:source",
        }),
        "PUBLIC_EXPORT_ACCEPTANCE.json": json.dumps({
            "schema_version": "quantos-clean-room-acceptance-v2",
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


def _set_candidate_payload(root: Path, payload_id: str) -> None:
    path = root / "PUBLIC_EXPORT_METADATA.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["public_payload_snapshot_id"] = payload_id
    metadata["python_test_count"] = 1609
    metadata["frontend_test_count"] = 30
    metadata["clean_room"] = "PASS"
    path.write_text(json.dumps(metadata), encoding="utf-8")


def test_manifest_is_deterministic_and_pass_requires_every_gate(tmp_path):
    _release_tree(tmp_path)
    gates = (Gate("source", "PASS"), Gate("docker", "PASS"))
    first = make_manifest(
        root=tmp_path, public_payload_snapshot_id="public-payload-sha256:abc",
        python_tests=1600, frontend_tests=30, gates=gates,
    )
    second = make_manifest(
        root=tmp_path, public_payload_snapshot_id="public-payload-sha256:abc",
        python_tests=1600, frontend_tests=30, gates=gates,
    )
    assert first.to_bytes() == second.to_bytes()
    require_pass(first)

    failed = make_manifest(
        root=tmp_path, public_payload_snapshot_id="public-payload-sha256:abc",
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


def test_frontend_count_accepts_github_actions_ansi_output():
    output = "\x1b[2m Test Files \x1b[22m \x1b[1m4 passed\x1b[22m\n" \
             "\x1b[2m      Tests \x1b[22m \x1b[1m30 passed\x1b[22m\n"

    assert parse_frontend_count(output) == 30


def test_container_inventory_rejects_runtime_data_and_credential_keys():
    validate_container_inventory(
        ("/app/web/dist/index.html", "/app/release-manifest.json"),
        ("PATH=/usr/bin", "PORT=8000"),
    )
    with pytest.raises(AcceptanceError, match="IMAGE_SECURITY"):
        validate_container_inventory(
            ("/app/data/market.parquet",), ("TUSHARE_TOKEN=redacted",),
        )


def test_public_payload_digest_changes_when_payload_changes(tmp_path):
    (tmp_path / "README.md").write_text("first\n", encoding="utf-8")
    first = clean_room.public_payload_snapshot_identifier(tmp_path)

    (tmp_path / "README.md").write_text("second\n", encoding="utf-8")

    assert clean_room.public_payload_snapshot_identifier(tmp_path) != first


def test_public_payload_digest_explicitly_excludes_attestations(tmp_path):
    (tmp_path / "README.md").write_text("payload\n", encoding="utf-8")
    for name in clean_room.ATTESTATION_FILES:
        (tmp_path / name).write_text("first attestation\n", encoding="utf-8")
    first = clean_room.public_payload_snapshot_identifier(tmp_path)

    for name in clean_room.ATTESTATION_FILES:
        (tmp_path / name).write_text("changed attestation\n", encoding="utf-8")

    assert clean_room.public_payload_snapshot_identifier(tmp_path) == first


def test_manifest_payload_id_matches_independent_recomputation(tmp_path):
    _release_tree(tmp_path)
    (tmp_path / "README.md").write_text("candidate payload\n", encoding="utf-8")
    payload_id = clean_room.public_payload_snapshot_identifier(tmp_path)
    _set_candidate_payload(tmp_path, payload_id)
    manifest = clean_room.make_manifest(
        root=tmp_path, public_payload_snapshot_id=payload_id,
        python_tests=1609, frontend_tests=30,
        gates=(Gate("source", "PASS"),),
    )
    (tmp_path / "PUBLIC_EXPORT_ACCEPTANCE.json").write_bytes(manifest.to_bytes())

    clean_room.validate_payload_attestation(tmp_path, payload_id)
    assert manifest.public_payload_snapshot_id == payload_id


def test_final_export_validation_runs_after_manifest_generation(tmp_path):
    _release_tree(tmp_path)
    allowlist = tmp_path / "public_export_manifest.txt"
    entries = tuple(sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*") if path.is_file()
    ))
    allowlist.write_text("\n".join(entries) + "\n", encoding="utf-8")
    entries = tuple(sorted(set(entries) | {"public_export_manifest.txt"}))
    payload_id = clean_room.public_payload_snapshot_identifier(tmp_path)
    _set_candidate_payload(tmp_path, payload_id)
    manifest = clean_room.make_manifest(
        root=tmp_path, public_payload_snapshot_id=payload_id,
        python_tests=1609, frontend_tests=30,
        gates=(Gate("source", "PASS"),),
    )
    (tmp_path / "PUBLIC_EXPORT_ACCEPTANCE.json").write_bytes(manifest.to_bytes())

    clean_room.validate_final_export(tmp_path, entries, payload_id)


def test_source_and_candidate_snapshot_ids_cannot_be_conflated(tmp_path):
    _release_tree(tmp_path)
    payload_id = clean_room.public_payload_snapshot_identifier(tmp_path)
    metadata = json.loads(
        (tmp_path / "PUBLIC_EXPORT_METADATA.json").read_text(encoding="utf-8")
    )
    metadata["accepted_source_snapshot_id"] = payload_id
    metadata["public_payload_snapshot_id"] = payload_id
    (tmp_path / "PUBLIC_EXPORT_METADATA.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    with pytest.raises(AcceptanceError, match="PROVENANCE"):
        clean_room.validate_payload_attestation(tmp_path, payload_id)


def test_missing_source_snapshot_id_fails_with_actionable_acceptance_error(tmp_path):
    _release_tree(tmp_path)
    metadata_path = tmp_path / "PUBLIC_EXPORT_METADATA.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("accepted_source_snapshot_id")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(AcceptanceError, match="accepted source snapshot ID is missing"):
        clean_room.accepted_source_snapshot(tmp_path)
