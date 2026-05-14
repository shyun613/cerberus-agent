#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${1:-telegram-openai-bot-alt}"

if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  docker rm -f "$CONTAINER_NAME" >/dev/null
  echo "Removed container: $CONTAINER_NAME"
else
  echo "Container not found: $CONTAINER_NAME"
fi
