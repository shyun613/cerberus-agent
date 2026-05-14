#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${1:-telegram-openai-bot-alt}"

docker logs -f "$CONTAINER_NAME"
