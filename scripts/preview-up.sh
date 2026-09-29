#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${QUANTOS_PREVIEW_IMAGE:-quantos-public-preview:local}"
CONTAINER="${QUANTOS_PREVIEW_CONTAINER:-quantos-public-preview}"
PORT="${QUANTOS_PREVIEW_PORT:-8080}"
BIND="127.0.0.1"
MODE="LOCAL"

case "${1:-}" in
  "") ;;
  --lan) BIND="0.0.0.0"; MODE="LAN" ;;
  *) echo "Usage: scripts/preview-up.sh [--lan]" >&2; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is not installed or not on PATH." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required for the startup health gate." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon is unavailable to the current user." >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  "$ROOT/scripts/preview-build.sh"
fi

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
owner="$(docker container inspect --format '{{ index .Config.Labels "org.quantos.preview" }}' "$CONTAINER")"
  [[ "$owner" == "true" ]] || { echo "ERROR: Container '$CONTAINER' is not owned by the QuantOS Preview operator." >&2; exit 1; }
  docker rm -f "$CONTAINER" >/dev/null
fi

docker run -d --name "$CONTAINER" --label org.quantos.preview=true \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  -p "$BIND:$PORT:8000" "$IMAGE" >/dev/null

ready=0
for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/v1/health" >/dev/null; then ready=1; break; fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "ERROR: QuantOS Preview did not become healthy within 30 seconds." >&2
  docker logs --tail 50 "$CONTAINER" >&2 || true
  docker rm -f "$CONTAINER" >/dev/null || true
  exit 1
fi

echo "QuantOS Preview is healthy."
echo "Mode: $MODE"
echo "URL: http://127.0.0.1:$PORT"
if [[ "$MODE" == "LAN" ]]; then
  echo "WARNING: LAN mode exposes the synthetic demo to devices on your local network."
  if command -v hostname >/dev/null 2>&1; then
    for address in $(hostname -I 2>/dev/null || true); do
      [[ "$address" == *:* || "$address" == 127.* ]] && continue
      echo "LAN URL: http://$address:$PORT"
    done
  fi
fi
