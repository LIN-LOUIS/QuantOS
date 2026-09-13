#!/usr/bin/env python3
"""Run the offline QuantOS V1 multi-day evaluation harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from quantos.evaluation import CORE_FREEZE_COMMIT, run_synthetic_strict_evaluation

PUBLIC_METADATA_SCHEMA_VERSION = "quantos-public-export-v1"
PUBLIC_PROJECT_VERSION = "0.1.0"
PUBLIC_SOURCE_PRIVATE_COMMIT = "0a0b87cce459d8b5d5cb0a3a89a408fc08a025ec"
PUBLIC_CORE_FREEZE_TAG = "v0.1.0-core"
PUBLIC_DISTRIBUTION = "PUBLIC_BACKEND"


def validate_public_provenance(project_root: Path) -> str:
    """Validate curated-export metadata or the legacy private Git baseline."""
    metadata_path = project_root / "PUBLIC_EXPORT_METADATA.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SystemExit("public export provenance metadata is invalid") from None
        expected = {
            "schema_version": PUBLIC_METADATA_SCHEMA_VERSION,
            "project_version": PUBLIC_PROJECT_VERSION,
            "source_private_commit": PUBLIC_SOURCE_PRIVATE_COMMIT,
            "core_freeze_commit": CORE_FREEZE_COMMIT,
            "core_freeze_tag": PUBLIC_CORE_FREEZE_TAG,
            "distribution": PUBLIC_DISTRIBUTION,
        }
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            raise SystemExit("public export provenance metadata is invalid")
        return "PUBLIC_EXPORT_METADATA"

    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=project_root,
        check=True, capture_output=True, text=True,
    )
    if completed.stdout.strip() != CORE_FREEZE_COMMIT:
        raise SystemExit("evaluation requires the frozen QuantOS V1 core commit")
    return "PRIVATE_GIT_HEAD"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a network-free V1 POST_CLOSE STRICT evaluation.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/v1-strict-10d"))
    parser.add_argument("--trading-days", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    validate_public_provenance(project_root)
    result = run_synthetic_strict_evaluation(
        args.output_dir,
        trading_days=args.trading_days,
    )
    print(json.dumps(result.summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
