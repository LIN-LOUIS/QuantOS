#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${QUANTOS_PREVIEW_CONTAINER:-quantos-public-preview}"
FOLLOW=0
case "${1:-}" in "") ;; --follow|-f) FOLLOW=1 ;; *) echo "Usage: scripts/preview-logs.sh [--follow]" >&2; exit 2 ;; esac
command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is not installed or not on PATH." >&2; exit 1; }
if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then echo "ERROR: QuantOS Preview is not running." >&2; exit 1; fi
owner="$(docker container inspect --format '{{ index .Config.Labels "org.quantos.preview" }}' "$CONTAINER")"
[[ "$owner" == "true" ]] || { echo "ERROR: Container '$CONTAINER' is not owned by the QuantOS Preview operator." >&2; exit 1; }
if [[ "$FOLLOW" == "1" ]]; then exec docker logs --tail 100 --follow "$CONTAINER"; fi
exec docker logs --tail 100 "$CONTAINER"
