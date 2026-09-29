#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${QUANTOS_PREVIEW_CONTAINER:-quantos-public-preview}"
PORT="${QUANTOS_PREVIEW_PORT:-8080}"
command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is not installed or not on PATH." >&2; exit 1; }
if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then echo "Container: not running"; exit 1; fi
owner="$(docker container inspect --format '{{ index .Config.Labels "org.quantos.preview" }}' "$CONTAINER")"
[[ "$owner" == "true" ]] || { echo "ERROR: Container '$CONTAINER' is not owned by the QuantOS Preview operator." >&2; exit 1; }
echo "Container: $(docker container inspect --format '{{.State.Status}}' "$CONTAINER")"
echo "URL: http://127.0.0.1:$PORT"
if health="$(curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/v1/health" 2>/dev/null)"; then
  HEALTH="$health" python - <<'PY'
import json, os
value = json.loads(os.environ["HEALTH"])
print("Health:", value.get("status", "unknown"))
print("Version:", value.get("quantos_version", "unknown"))
print("Build:", value.get("build_commit", "unknown"))
PY
else
  echo "Health: unavailable"
  exit 1
fi
