#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${QUANTOS_PREVIEW_IMAGE:-quantos-public-preview:local}"

command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is not installed or not on PATH." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon is unavailable to the current user." >&2; exit 1; }
COMMIT="${QUANTOS_BUILD_COMMIT:-}"
if [[ -z "$COMMIT" ]]; then
  COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null)" || {
    echo "ERROR: Cannot determine build identity. Set QUANTOS_BUILD_COMMIT for an exported source snapshot." >&2
    exit 1
  }
fi

echo "Building $IMAGE from ${COMMIT:0:12}..."
docker build --build-arg "QUANTOS_BUILD_COMMIT=$COMMIT" --tag "$IMAGE" "$ROOT"
echo "Built $IMAGE"
