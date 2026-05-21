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

# AGY in headless containers may ignore file-based local auth unless it detects SSH context.
# Force a synthetic SSH_CONNECTION when explicitly enabled.
force_file_auth="${ANTIGRAVITY_FORCE_FILE_AUTH:-true}"
if [[ "$force_file_auth" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])$ ]] && [[ -z "${SSH_CONNECTION:-}" ]]; then
  export SSH_CONNECTION="127.0.0.1 0 127.0.0.1 0"
fi

exec python bot.py
