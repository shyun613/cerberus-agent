# Telegram Cerberus Agent (Docker)

This bot orchestrates three local CLIs:

- `codex`
- `claude`
- `gemini`

Task routing policy:

- `default`: direct `codex` response
- `code`: if code content is detected, run `codex` implement -> `claude` review/fix -> `gemini` assist, and prefix response with `[code]`
- `survey`: if user asks to research/investigate (`조사`, `research`, `survey`, etc.), run `gemini` + `codex` collect/summarize -> `claude` validate/judge, and prefix response with `[survey]`

## Persistence

For stable restarts, keep these mounted:

- `/root/.codex` (Codex auth/session)
- `/root/.claude` (Claude auth/session)
- `/root/.claude.json` (Claude CLI config file)
- `/root/.gemini` (Gemini auth/session)
- `/app/data` (chat session DB, uploads, generated files)

`/session` and `/reset` are for the Codex coding thread mapping only.

## 1) Prepare env

```bash
cp .env.example .env
```

Required:

- `TELEGRAM_BOT_TOKEN`

Main optional vars:

- `CODEX_BIN`, `CODEX_MODEL`, `CODEX_EXTRA_ARGS`, `CODEX_WORKDIR`, `CODEX_TIMEOUT_SEC`
- `CLAUDE_BIN`, `CLAUDE_MODEL` (default: `claude-sonnet-4-6`), `CLAUDE_EXTRA_ARGS`, `CLAUDE_TIMEOUT_SEC`, `CLAUDE_PERMISSION_MODE` (default: `acceptEdits`)
- `GEMINI_BIN`, `GEMINI_MODEL` (default: `gemini-3-pro-preview`), `GEMINI_EXTRA_ARGS`, `GEMINI_TIMEOUT_SEC`, `GEMINI_APPROVAL_MODE`, `GEMINI_CLI_TRUST_WORKSPACE` (default: `true`)
- `UPLOAD_DIR`, `GENERATED_FILES_DIR`, `MAX_RETURN_FILES`, `MAX_RETURN_FILE_SIZE_MB`
- `SESSION_DB_PATH`
- `ALLOWED_CHAT_IDS`, `ALLOWED_USER_IDS`

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
- `/session`
- `/reset`
- `/whoami`

Document uploads are supported. The bot downloads the file to `UPLOAD_DIR` and runs the same routing policy.

If generated files are created/updated under `GENERATED_FILES_DIR`, the bot sends them back as Telegram documents.
