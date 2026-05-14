#!/usr/bin/env bash
set -euo pipefail

CLAUDE_CONFIG_FILE="/root/.claude.json"
CLAUDE_BACKUP_DIR="/root/.claude/backups"

if [[ ! -f "$CLAUDE_CONFIG_FILE" ]] && [[ -d "$CLAUDE_BACKUP_DIR" ]]; then
  latest_claude_backup="$(ls -1t "$CLAUDE_BACKUP_DIR"/.claude.json.backup.* 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_claude_backup" ]]; then
    cp "$latest_claude_backup" "$CLAUDE_CONFIG_FILE"
    echo "Restored Claude config from backup: $latest_claude_backup"
  fi
fi

exec python bot.py
