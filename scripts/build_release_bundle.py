#!/usr/bin/env python3
"""Build the v0.3 release candidate locally without publishing it."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from quantos.release import build_release_bundle, repository_metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("build/release"))
    parser.add_argument("--skip-frontend-build", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    if not args.skip_frontend_build:
        subprocess.run(["npm", "ci"], cwd=root / "web", check=True)
        subprocess.run(["npm", "run", "build"], cwd=root / "web", check=True)
    commit, timestamp = repository_metadata(root)
    output = args.output_dir
    if not output.is_absolute():
        output = root / output
    archive = build_release_bundle(
        root, output, git_commit=commit, build_timestamp=timestamp,
    )
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
