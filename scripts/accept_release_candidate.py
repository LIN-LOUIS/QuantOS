#!/usr/bin/env python3
"""Run the canonical Phase 8A.2 clean-room release acceptance gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from quantos.clean_room import (
    AcceptanceError, Gate, create_tracked_snapshot, docker_smoke, make_manifest,
    parse_frontend_count, parse_pytest_count, public_payload_snapshot_identifier,
    require_pass, runtime_artifact_candidates, secret_candidates, stage_public_export,
    validate_final_export, validate_required_files, validate_version_consistency,
    run_checked,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repository", type=Path, default=Path.cwd())
    value.add_argument("--output", type=Path, default=Path("build/phase-8a2"))
    value.add_argument("--no-cache", action="store_true",
                       help="disable Docker build cache for the acceptance image")
    value.add_argument("--python", default=sys.executable,
                       help="Python executable used to create the clean verification venv")
    value.add_argument("--python310", type=Path,
                       help="optional Python 3.10 executable for the compatibility gate")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository = args.repository.resolve()
    output = args.output.resolve()
    try:
        result = accept(
            repository=repository, output=output, python=args.python,
            python310=args.python310, no_cache=args.no_cache,
        )
    except AcceptanceError as error:
        print(json.dumps({
            "status": "FAIL", "stage": error.stage, "reason": error.reason,
            "remediation": error.remediation,
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def accept(
    *, repository: Path, output: Path, python: str,
    python310: Path | None, no_cache: bool,
) -> dict[str, object]:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()
    source = output / "tracked-source"
    create_tracked_snapshot(repository, source)
    validate_required_files(source)

    verification = output / "verification-source"
    allowlist = source / "public_export_manifest.txt"
    entries = stage_public_export(source, verification, allowlist)
    _security_gate(verification)
    version = validate_version_consistency(verification)
    payload_id = public_payload_snapshot_identifier(verification)
    gates = [
        Gate("tracked_source", "PASS", "git archive HEAD"),
        Gate("required_files", "PASS", f"{len(entries)} allowlisted files"),
        Gate("secret_scan", "PASS", "0 credential-like values"),
        Gate("runtime_artifact_scan", "PASS", "0 forbidden runtime paths"),
        Gate("version_consistency", "PASS", version),
        Gate("export_diff", "PASS", "exact allowlist match"),
    ]

    venv = output / "python-venv"
    run_checked([python, "-m", "venv", str(venv)], stage="PYTHON_ENV",
                cwd=verification, remediation="Install a supported Python with venv support.",
                output=logs / "python-venv.log")
    py = venv / "bin" / "python"
    run_checked([str(py), "-m", "pip", "install", ".[dev]"],
                stage="PYTHON_INSTALL", cwd=verification,
                remediation="Resolve declared Python package dependencies.",
                output=logs / "python-install.log")
    pytest = run_checked([str(py), "-m", "pytest"], stage="PYTHON_TESTS",
                         cwd=verification, remediation="Fix the failing offline test.",
                         output=logs / "pytest.log")
    python_count = parse_pytest_count(pytest.stdout)
    run_checked([str(py), "-m", "pip", "check"], stage="PIP_CHECK", cwd=verification,
                remediation="Resolve broken Python requirements.", output=logs / "pip-check.log")
    run_checked([str(py), "-m", "compileall", "-q", "src", "scripts"],
                stage="COMPILEALL", cwd=verification,
                remediation="Fix Python syntax/import compilation failures.",
                output=logs / "compileall.log")
    gates.extend((Gate("python_tests", "PASS", f"{python_count} passed"),
                  Gate("pip_check", "PASS"), Gate("compileall", "PASS")))

    if python310 is not None:
        py310_venv = output / "python310-venv"
        run_checked([str(python310), "-m", "venv", str(py310_venv)],
                    stage="PYTHON310_ENV", cwd=verification,
                    remediation="Provide a working Python 3.10 interpreter.",
                    output=logs / "python310-venv.log")
        py310 = py310_venv / "bin" / "python"
        run_checked([str(py310), "-m", "pip", "install", ".[dev]"],
                    stage="PYTHON310_INSTALL", cwd=verification,
                    remediation="Resolve Python 3.10 dependencies.",
                    output=logs / "python310-install.log")
        result310 = run_checked([
            str(py310), "-m", "pytest", "tests/test_release_version.py",
            "tests/test_public_preview.py", "tests/test_clean_room.py",
        ], stage="PYTHON310_TESTS", cwd=verification,
            remediation="Restore declared Python 3.10 compatibility.",
            output=logs / "python310-tests.log")
        gates.append(Gate("python310", "PASS", f"{parse_pytest_count(result310.stdout)} passed"))

    web = verification / "web"
    run_checked(["npm", "ci"], stage="NPM_INSTALL", cwd=web,
                remediation="Restore the lockfile or npm registry access.",
                output=logs / "npm-ci.log")
    frontend = run_checked(["npm", "test", "--", "--reporter=dot"],
                           stage="FRONTEND_TESTS", cwd=web,
                           remediation="Fix the failing deterministic frontend test.",
                           output=logs / "frontend-tests.log")
    frontend_count = parse_frontend_count(frontend.stdout)
    run_checked(["npm", "run", "typecheck"], stage="TYPECHECK", cwd=web,
                remediation="Fix TypeScript contract errors.", output=logs / "typecheck.log")
    run_checked(["npm", "run", "build"], stage="FRONTEND_BUILD", cwd=web,
                remediation="Fix the production Workspace build.", output=logs / "frontend-build.log")
    run_checked(["npm", "audit"], stage="NPM_AUDIT", cwd=web,
                remediation="Review and resolve frontend dependency vulnerabilities.",
                output=logs / "npm-audit.log")
    gates.extend((Gate("frontend_tests", "PASS", f"{frontend_count} passed"),
                  Gate("typecheck", "PASS"), Gate("frontend_build", "PASS"),
                  Gate("npm_audit", "PASS")))

    image_suffix = payload_id.rsplit(":", 1)[-1][:12]
    smoke = docker_smoke(
        source=verification, image=f"quantos-clean-room:{image_suffix}",
        container=f"quantos-clean-room-{image_suffix}", snapshot_id=payload_id,
        no_cache=no_cache, logs=logs,
    )
    gates.extend((Gate("docker_build", "PASS"), Gate("container_health", "PASS"),
                  Gate("non_root_runtime", "PASS", smoke["runtime_user"]),
                  Gate("runtime_mode", "PASS", smoke["runtime_mode"]),
                  Gate("deployment_mode", "PASS", smoke["deployment_mode"]),
                  Gate("image_security", "PASS", "0 forbidden paths or credential keys"),
                  Gate("synthetic_demo_e2e", "PASS")))

    final_stage = output / "public-export"
    stage_public_export(source, final_stage, allowlist)
    if public_payload_snapshot_identifier(final_stage) != payload_id:
        raise AcceptanceError("EXPORT_DIFF", "final export content changed after acceptance",
                              "Regenerate from the same tracked snapshot.")
    gates.append(Gate("final_export_scan", "PASS"))
    manifest = make_manifest(
        root=final_stage, public_payload_snapshot_id=payload_id,
        python_tests=python_count,
        frontend_tests=frontend_count, gates=gates,
    )
    require_pass(manifest)
    manifest_path = final_stage / "PUBLIC_EXPORT_ACCEPTANCE.json"
    manifest_path.write_bytes(manifest.to_bytes())
    validate_final_export(final_stage, entries, payload_id)
    (output / "acceptance-result.json").write_bytes(manifest.to_bytes())
    return {
        "status": "PASS", "staging_directory": str(final_stage),
        "manifest_path": str(manifest_path),
        "public_payload_snapshot_id": payload_id,
        "python_test_count": python_count, "frontend_test_count": frontend_count,
        "private_sha_disclosed": False,
    }


def _security_gate(root: Path) -> None:
    secrets = secret_candidates(root)
    if secrets:
        raise AcceptanceError(
            "SECRET_SCAN", f"credential-like values found in {len(secrets)} file(s)",
            "Remove real credentials; scanner output intentionally omits their values.",
        )
    runtime = runtime_artifact_candidates(root)
    if runtime:
        raise AcceptanceError(
            "RUNTIME_ARTIFACT_SCAN", f"forbidden runtime paths found: {len(runtime)}",
            "Remove runtime data and regenerate the tracked-only export.",
        )


if __name__ == "__main__":
    raise SystemExit(main())
