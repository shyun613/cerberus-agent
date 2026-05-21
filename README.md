# Telegram Cerberus Agent (Docker)

This bot orchestrates three local CLIs:

- `codex`
- `claude`
- `antigravity` (CLI: `agy`)

Task routing policy:

- `default`: direct `codex` response
- `code`: if code content is detected, run `codex` implement -> `claude` review/fix -> `antigravity` assist, and prefix response with `[code]`
- `survey`: if user asks to research/investigate (`조사`, `research`, `survey`, etc.), run `antigravity` + `codex` collect/summarize -> `claude` validate/judge, and prefix response with `[survey]`

## Commit status (verified)

- `11a0711` (HEAD): `model ensemble`
- `beee918`: `first commit telegram-codex-agent repo`

This README reflects behavior in `11a0711`.

## Persistence

For stable restarts, keep these mounted:

- `/root/.codex` (Codex auth/session)
- `/root/.claude` (Claude auth/session)
- `/root/.claude.json` (Claude CLI config file)
- `/root/.antigravity` (Antigravity auth/session)
- `/root/.gemini` (AGY runtime state: auth/cache/conversations under `~/.gemini/antigravity-cli`)
- `/app/data` (chat session DB, uploads, generated files)

If `/root/.claude.json` is missing but `/root/.claude/backups` exists, `docker-entrypoint.sh` restores the latest backup automatically.

`/session` and `/reset` manage per-agent mapped sessions (`codex`, `claude`, `antigravity`).

## 1) Prepare env

```bash
cp .env.example .env.telegram-alt
# optional (for plain docker run examples below)
cp .env.example .env
```

`docker-compose.telegram-alt.yml` and helper scripts use `.env.telegram-alt` by default.

Required:

- `TELEGRAM_BOT_TOKEN`

Main optional vars:

- `CODEX_BIN`, `CODEX_MODEL`, `CODEX_EXTRA_ARGS`, `CODEX_WORKDIR`, `CODEX_TIMEOUT_SEC`
- `CODEX_SYSTEM_PROMPT`
- `CLAUDE_BIN`, `CLAUDE_MODEL` (default: `claude-sonnet-4-6`), `CLAUDE_EXTRA_ARGS`, `CLAUDE_TIMEOUT_SEC`, `CLAUDE_PERMISSION_MODE` (default: `acceptEdits`)
- `ANTIGRAVITY_BIN` (default: `agy`), `ANTIGRAVITY_EXTRA_ARGS`, `ANTIGRAVITY_TIMEOUT_SEC`, `ANTIGRAVITY_APPROVAL_MODE`, `ANTIGRAVITY_CLI_TRUST_WORKSPACE` (default: `true`), `ANTIGRAVITY_FORCE_FILE_AUTH` (default: `true` for headless container auth)
- `SSH_CONNECTION` (optional; set to `127.0.0.1 0 127.0.0.1 0` in headless containers to force AGY file-token auth path)
- `REQUEST_DEDUPE_WINDOW_SEC` (default: `20`, skip duplicate update/payload execution in short window)
- `UPLOAD_DIR`, `GENERATED_FILES_DIR`, `MAX_RETURN_FILES`, `MAX_RETURN_FILE_SIZE_MB`
- `SESSION_DB_PATH`
- `SESSION_COMPACT_EVERY_TURNS` (default: `5`, auto compact per agent session)
- `SESSION_IDLE_CLEAR_AFTER_SEC` (default: `3600`, auto-clear mapped sessions after idle gap; `0` disables)
- `ALLOWED_CHAT_IDS`, `ALLOWED_USER_IDS`
- `TELEGRAM_API_TIMEOUT_SEC`, `TELEGRAM_SEND_RETRIES`, `TELEGRAM_SEND_RETRY_DELAY_SEC`
- `LOG_LEVEL`

Model resolution priority (per agent):

- `codex` / `claude`: chat override via `/model <agent> <name>` -> `*_MODEL` from env -> CLI default
- `antigravity`: AGY print mode does not expose `--model`; bot-side model override/env model is not applied

## 2) Verify host logins

```bash
codex login status
claude auth status
agy --version
```

For `antigravity`, a quick headless check also works:

```bash
agy --prompt "Reply with exactly: OK"
```

When `ANTIGRAVITY_FORCE_FILE_AUTH=true`, the bot process uses a synthetic `SSH_CONNECTION` to keep AGY on mounted local file auth in headless mode. Setting `SSH_CONNECTION` in env file applies this to manual `docker exec` checks as well.

## 3) Start with Docker Compose

```bash
docker compose -f docker-compose.telegram-alt.yml up -d --build
```

Legacy compose:

```bash
docker-compose -f docker-compose.telegram-alt.yml up -d --build
```

Helper runner (default args: `.env.telegram-alt`, `telegram-openai-bot-alt`, `telegram-openai-bot:codex`):

```bash
./run-telegram-alt-container.sh [ENV_FILE] [CONTAINER_NAME] [IMAGE]
```

## 4) Start without Compose

```bash
docker build -t telegram-openai-bot:local .
docker run -d \
  --name telegram-openai-bot \
  --env-file ./.env \
  -v "$(pwd):/app" \
  -v "$HOME/.codex:/root/.codex" \
  -v "$HOME/.claude:/root/.claude" \
  -v "$HOME/.antigravity:/root/.antigravity" \
  -v "$HOME/.gemini:/root/.gemini" \
  --restart unless-stopped \
  telegram-openai-bot:local
```

## 5) Logs

```bash
docker compose -f docker-compose.telegram-alt.yml logs -f telegram-openai-bot-alt
```

Helper script:

```bash
./logs-telegram-alt-container.sh [CONTAINER_NAME]
```

Without Compose:

```bash
docker logs -f telegram-openai-bot
```

## 6) Update workflow

- Code/doc change only: container restart is enough.
- `.env` change: recreate container.
- Rebuild required when `Dockerfile` or dependency layers changed.

## 7) Stop

```bash
docker compose -f docker-compose.telegram-alt.yml down
```

Helper script:

```bash
./stop-telegram-alt-container.sh [CONTAINER_NAME]
```

Without Compose:

```bash
docker stop telegram-openai-bot && docker rm telegram-openai-bot
```

## Telegram commands

- `/start`
- `/model` (show codex/claude/antigravity effective models and sources)
- `/model <name>` (legacy: set codex model)
- `/model clear` (legacy: clear codex override)
- `/model <agent> <name>` (`agent`: `codex|claude`; `antigravity` model override unsupported)
- `/model <agent> clear`
- `/session` (show mapped session IDs + per-agent turn counters)
- `/reset` (clear all mapped sessions)
- `/reset <agent>` (clear mapped session for `codex|claude|antigravity`)
- `/reset all` (same as `/reset`)
- `/whoami`

Session compaction:

- Every `SESSION_COMPACT_EVERY_TURNS` turns (default `5`), each agent session auto-compacts.
- Commands: `codex=/compact`, `claude=/compact`, `antigravity=/compress`.

Session idle auto-clear:

- If no new user request arrives for `SESSION_IDLE_CLEAR_AFTER_SEC` seconds (default `3600`), all mapped sessions are cleared before processing the next request.

Document uploads are supported. The bot downloads the file to `UPLOAD_DIR` and runs the same routing policy.

If generated files are created/updated under `GENERATED_FILES_DIR`, the bot sends them back as Telegram documents.
