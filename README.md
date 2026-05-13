# Telegram Codex Bot (Docker)

This bot uses local `codex` CLI (not direct OpenAI API calls).

To keep the same Codex session after container restart, two things must persist:

- `/root/.codex` in container: Codex auth + thread data
- `./telegram_openai_bot/data` in container: `chat_id -> thread_id` mapping

Uploaded Telegram documents are downloaded under:

- Container: `/app/data/uploads/<chat_id>/...`
- Host (default): `./telegram_openai_bot/data/uploads/<chat_id>/...`

Generated files from Codex are stored under:

- Container: `/app/data/generated/<chat_id>/...`
- Host (default): `./telegram_openai_bot/data/generated/<chat_id>/...`

## 1) Prepare env file

```bash
cp telegram_openai_bot/.env.example telegram_openai_bot/.env
```

Required:

- `TELEGRAM_BOT_TOKEN`

Optional:

- `CODEX_MODEL`
- `CODEX_SYSTEM_PROMPT`
- `CODEX_EXTRA_ARGS`
- `CODEX_WORKDIR`
- `UPLOAD_DIR` (default: `./data/uploads`)
- `GENERATED_FILES_DIR` (default: `./data/generated`)
- `MAX_RETURN_FILES` (default: `5`)
- `MAX_RETURN_FILE_SIZE_MB` (default: `20`)
- `TELEGRAM_API_TIMEOUT_SEC` (default: `30`)
- `TELEGRAM_SEND_RETRIES` (default: `3`)
- `TELEGRAM_SEND_RETRY_DELAY_SEC` (default: `1.0`)
- `ALLOWED_CHAT_IDS` (comma-separated)
- `ALLOWED_USER_IDS` (comma-separated)

## 2) Ensure host codex login exists

```bash
codex login status
```

Expected output includes `Logged in`.

## 3) Start with Docker Compose

`docker-compose.yml` mounts `${HOME}/.codex` into `/root/.codex`.
It also mounts `./telegram_openai_bot` into `/app`, so code/config changes are reflected without image rebuild.

```bash
docker compose up -d --build
```

If your environment uses legacy compose:

```bash
docker-compose up -d --build
```

## 4) Start without Compose (docker build/run)

```bash
docker build -t telegram-openai-bot:local ./telegram_openai_bot
docker run -d \
  --name telegram-openai-bot \
  --env-file ./telegram_openai_bot/.env \
  -v "$(pwd)/telegram_openai_bot:/app" \
  -v "$HOME/.codex:/root/.codex" \
  --restart unless-stopped \
  telegram-openai-bot:local
```

## 5) Check logs

```bash
docker compose logs -f telegram-openai-bot
```

Legacy compose:

```bash
docker-compose logs -f telegram-openai-bot
```

Without Compose:

```bash
docker logs -f telegram-openai-bot
```

## Update workflow (no rebuild for code changes)

- When only app code/docs change (`bot.py`, `README.md`, etc.): restart container only.
- When `.env` values change: recreate container so new environment variables are applied (no rebuild needed).
- Rebuild is required only when image-level dependencies change (`Dockerfile`, `requirements.txt`, base image / apt / npm install layers).

Restart example:

```bash
docker restart telegram-openai-bot
```

Recreate example (apply new `.env` without rebuild):

```bash
docker rm -f telegram-openai-bot
docker run -d \
  --name telegram-openai-bot \
  --env-file ./telegram_openai_bot/.env \
  -v "$(pwd)/telegram_openai_bot:/app" \
  -v "$HOME/.codex:/root/.codex" \
  --restart unless-stopped \
  telegram-openai-bot:local
```

## 6) Stop

```bash
docker compose down
```

Legacy compose:

```bash
docker-compose down
```

Without Compose:

```bash
docker stop telegram-openai-bot && docker rm telegram-openai-bot
```

## Telegram commands

- `/start`
- `/session`
- `/reset`
- `/whoami`

You can also upload a Telegram `document` (file). The bot downloads it and asks Codex to process it.

If you ask Codex to create a file (for example `txt`, `csv`, `md`, `html`), the bot will send newly created/updated files from `GENERATED_FILES_DIR` back as Telegram documents.

`/reset` only clears local mapping, so the next message starts a new Codex thread.
