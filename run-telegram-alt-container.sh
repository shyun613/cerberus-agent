#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env.telegram-alt}"
CONTAINER_NAME="${2:-telegram-openai-bot-alt}"
IMAGE="${3:-telegram-openai-bot:codex}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

token_line="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -n1 || true)"
TOKEN="${token_line#TELEGRAM_BOT_TOKEN=}"
if [[ -z "$TOKEN" || "$TOKEN" == "REPLACE_WITH_NEW_TELEGRAM_BOT_TOKEN" ]]; then
  echo "Set TELEGRAM_BOT_TOKEN in $ENV_FILE before running." >&2
  exit 1
fi

CLAUDE_CONFIG_FILE="$HOME/.claude.json"
CLAUDE_BACKUP_DIR="$HOME/.claude/backups"
if [[ ! -f "$CLAUDE_CONFIG_FILE" ]] && [[ -d "$CLAUDE_BACKUP_DIR" ]]; then
  latest_claude_backup="$(ls -1t "$CLAUDE_BACKUP_DIR"/.claude.json.backup.* 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_claude_backup" ]]; then
    cp "$latest_claude_backup" "$CLAUDE_CONFIG_FILE"
    echo "Restored Claude config from backup: $latest_claude_backup"
  fi
fi

if [[ -d "$CLAUDE_CONFIG_FILE" ]]; then
  echo "Warning: $CLAUDE_CONFIG_FILE is a directory. Expected a file for Claude config mount." >&2
fi

if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  echo "Replacing existing container: $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi

docker_args=(
  -d
  --name "$CONTAINER_NAME"
  --env-file "$ENV_FILE"
  -v "$ROOT_DIR:/app"
  -v "$HOME/.codex:/root/.codex"
  -v "$HOME/.claude:/root/.claude"
  -v "$HOME/.antigravity:/root/.antigravity"
  --restart unless-stopped
)

if [[ -f "$CLAUDE_CONFIG_FILE" ]]; then
  docker_args+=(-v "$CLAUDE_CONFIG_FILE:/root/.claude.json")
else
  echo "Warning: $CLAUDE_CONFIG_FILE not found. Container will use backup restore from /root/.claude/backups if available." >&2
fi

docker run "${docker_args[@]}" "$IMAGE" >/dev/null

echo "Started container: $CONTAINER_NAME"
docker ps --filter "name=^/${CONTAINER_NAME}$" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
