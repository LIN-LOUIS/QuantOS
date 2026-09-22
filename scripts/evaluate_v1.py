#!/usr/bin/env python3
"""Run the offline QuantOS V1 multi-day evaluation harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from quantos.evaluation import CORE_FREEZE_COMMIT, run_synthetic_strict_evaluation


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
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=project_root,
        check=True, capture_output=True, text=True,
    )
    if completed.stdout.strip() != CORE_FREEZE_COMMIT:
        raise SystemExit("evaluation requires the frozen QuantOS V1 core commit")
    result = run_synthetic_strict_evaluation(
        args.output_dir,
        trading_days=args.trading_days,
    )
    print(json.dumps(result.summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
