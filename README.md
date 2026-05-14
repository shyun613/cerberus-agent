# Telegram Cerberus Agent (Docker)

This bot orchestrates three local CLIs:

- `codex`
- `claude`
- `gemini`

Task routing policy:

- `default`: direct `codex` response
- `code`: if code content is detected, run `codex` implement -> `claude` review/fix -> `gemini` assist, and prefix response with `[code]`
- `survey`: if user asks to research/investigate (`조사`, `research`, `survey`, etc.), run `gemini` + `codex` collect/summarize -> `claude` validate/judge, and prefix response with `[survey]`

## Commit status (verified)

- `11a0711` (HEAD): `model ensemble`
- `beee918`: `first commit telegram-codex-agent repo`

This README reflects behavior in `11a0711`.

## Persistence

For stable restarts, keep these mounted:

- `/root/.codex` (Codex auth/session)
- `/root/.claude` (Claude auth/session)
- `/root/.claude.json` (Claude CLI config file)
- `/root/.gemini` (Gemini auth/session)
- `/app/data` (chat session DB, uploads, generated files)

If `/root/.claude.json` is missing but `/root/.claude/backups` exists, `docker-entrypoint.sh` restores the latest backup automatically.

`/session` and `/reset` manage per-agent mapped sessions (`codex`, `claude`, `gemini`).

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
- `GEMINI_BIN`, `GEMINI_MODEL` (default: `gemini-3-pro-preview`), `GEMINI_EXTRA_ARGS`, `GEMINI_TIMEOUT_SEC`, `GEMINI_APPROVAL_MODE`, `GEMINI_CLI_TRUST_WORKSPACE` (default: `true`)
- `UPLOAD_DIR`, `GENERATED_FILES_DIR`, `MAX_RETURN_FILES`, `MAX_RETURN_FILE_SIZE_MB`
- `SESSION_DB_PATH`
- `ALLOWED_CHAT_IDS`, `ALLOWED_USER_IDS`
- `TELEGRAM_API_TIMEOUT_SEC`, `TELEGRAM_SEND_RETRIES`, `TELEGRAM_SEND_RETRY_DELAY_SEC`
- `LOG_LEVEL`

Model resolution priority (per agent):

- chat override via `/model <agent> <name>`
- `*_MODEL` from env (`CODEX_MODEL`, `CLAUDE_MODEL`, `GEMINI_MODEL`)
- each CLI's own default (if env/default is blank)

## 2) Verify host logins

```bash
codex login status
claude auth status
gemini
```

For `gemini`, a quick headless check also works:

```bash
gemini -p "Reply with exactly: OK" --output-format text
```

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
- `/model` (show codex/claude/gemini effective models and sources)
- `/model <name>` (legacy: set codex model)
- `/model clear` (legacy: clear codex override)
- `/model <agent> <name>` (`agent`: `codex|claude|gemini`)
- `/model <agent> clear`
- `/session` (show mapped session IDs for codex/claude/gemini)
- `/reset` (clear all mapped sessions)
- `/reset <agent>` (clear mapped session for `codex|claude|gemini`)
- `/reset all` (same as `/reset`)
- `/whoami`

Document uploads are supported. The bot downloads the file to `UPLOAD_DIR` and runs the same routing policy.

If generated files are created/updated under `GENERATED_FILES_DIR`, the bot sends them back as Telegram documents.
