#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${QUANTOS_PREVIEW_CONTAINER:-quantos-public-preview}"
command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is not installed or not on PATH." >&2; exit 1; }
if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then echo "QuantOS Preview is not running."; exit 0; fi
owner="$(docker container inspect --format '{{ index .Config.Labels "org.quantos.preview" }}' "$CONTAINER")"
[[ "$owner" == "true" ]] || { echo "ERROR: Container '$CONTAINER' is not owned by the QuantOS Preview operator." >&2; exit 1; }
docker rm -f "$CONTAINER" >/dev/null
echo "QuantOS Preview stopped."
