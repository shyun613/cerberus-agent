import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
from telegram.ext import MessageHandler, filters


def parse_id_set(raw: str) -> Set[int]:
    values: Set[int] = set()
    if not raw.strip():
        return values
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        values.add(int(token))
    return values


def chunk_text(text: str, size: int = 4000) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks


def parse_thread_id_from_jsonl(stdout_text: str) -> Optional[str]:
    thread_id: Optional[str] = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started":
            value = payload.get("thread_id")
            if isinstance(value, str) and value.strip():
                thread_id = value.strip()
    return thread_id


def parse_last_agent_message_from_jsonl(stdout_text: str) -> str:
    last_text = ""
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item", {})
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            last_text = text
    return last_text.strip()


def read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def sanitize_filename(name: str, max_len: int = 120) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not cleaned:
        return "file"
    return cleaned[:max_len]


def parse_positive_int(raw: str, default: int, min_value: int) -> int:
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return max(parsed, min_value)


def collect_file_state(root_dir: Path) -> dict[str, tuple[int, int]]:
    if not root_dir.exists():
        return {}

    root_resolved = root_dir.resolve()
    state: dict[str, tuple[int, int]] = {}

    for path in root_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(root_resolved):
                continue
            stat = resolved.stat()
        except OSError:
            continue
        state[str(resolved)] = (stat.st_mtime_ns, stat.st_size)
    return state


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id INTEGER PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def get_thread_id(self, chat_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def set_thread_id(self, chat_id: int, thread_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (chat_id, thread_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, thread_id),
            )
            conn.commit()

    def delete(self, chat_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE chat_id = ?",
                (chat_id,),
            )
            conn.commit()
            return cursor.rowcount > 0


class CodexRunner:
    def __init__(
        self,
        codex_bin: str,
        model: str,
        extra_args: list[str],
        workdir: Path,
        timeout_sec: int,
    ) -> None:
        self.codex_bin = codex_bin
        self.model = model.strip()
        self.extra_args = extra_args
        self.workdir = workdir
        self.timeout_sec = timeout_sec

    def _build_command(
        self,
        prompt: str,
        thread_id: Optional[str],
        output_last_message_path: str,
    ) -> list[str]:
        if thread_id:
            cmd = [
                self.codex_bin,
                "exec",
                "resume",
                "--skip-git-repo-check",
                "--json",
                "--output-last-message",
                output_last_message_path,
            ]
        else:
            cmd = [
                self.codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--json",
                "--output-last-message",
                output_last_message_path,
            ]

        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(self.extra_args)

        if thread_id:
            cmd.extend([thread_id, prompt])
        else:
            cmd.append(prompt)
        return cmd

    async def run_prompt(self, prompt: str, thread_id: Optional[str]) -> tuple[str, Optional[str]]:
        output_fd, output_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
        os.close(output_fd)

        try:
            cmd = self._build_command(prompt, thread_id, output_path)
            env = dict(os.environ)
            env.setdefault("NO_COLOR", "1")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout_sec,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(f"codex command timed out after {self.timeout_sec}s")

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            new_thread_id = parse_thread_id_from_jsonl(stdout_text) or thread_id

            if proc.returncode != 0:
                err = stderr_text.strip() or stdout_text.strip() or "Unknown codex error"
                raise RuntimeError(err)

            reply_text = read_text_file(output_path)
            if not reply_text:
                reply_text = parse_last_agent_message_from_jsonl(stdout_text)
            if not reply_text:
                reply_text = "Codex returned no text output."

            return reply_text, new_thread_id
        finally:
            try:
                os.remove(output_path)
            except OSError:
                pass


class TelegramCodexBot:
    def __init__(self) -> None:
        load_dotenv()

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        self.logger = logging.getLogger("telegram-codex-bot")

        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")

        self.telegram_token = telegram_token
        self.system_prompt = os.getenv("CODEX_SYSTEM_PROMPT", "").strip()
        self.allowed_chat_ids = parse_id_set(os.getenv("ALLOWED_CHAT_IDS", ""))
        self.allowed_user_ids = parse_id_set(os.getenv("ALLOWED_USER_IDS", ""))

        db_path = Path(os.getenv("SESSION_DB_PATH", "./data/sessions.sqlite3")).expanduser()
        self.sessions = SessionStore(db_path)
        self.upload_dir = Path(os.getenv("UPLOAD_DIR", "./data/uploads")).expanduser().resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.generated_files_dir = Path(
            os.getenv("GENERATED_FILES_DIR", "./data/generated")
        ).expanduser().resolve()
        self.generated_files_dir.mkdir(parents=True, exist_ok=True)
        self.max_return_files = parse_positive_int(
            os.getenv("MAX_RETURN_FILES", "5"),
            default=5,
            min_value=0,
        )
        self.max_return_file_size_mb = parse_positive_int(
            os.getenv("MAX_RETURN_FILE_SIZE_MB", "20"),
            default=20,
            min_value=1,
        )
        self.max_return_file_size_bytes = self.max_return_file_size_mb * 1024 * 1024

        codex_bin = os.getenv("CODEX_BIN", "codex").strip() or "codex"
        codex_model = os.getenv("CODEX_MODEL", "").strip()
        codex_extra_args = shlex.split(os.getenv("CODEX_EXTRA_ARGS", ""))
        codex_workdir = Path(os.getenv("CODEX_WORKDIR", ".")).expanduser().resolve()
        codex_timeout = int(os.getenv("CODEX_TIMEOUT_SEC", "300"))
        telegram_api_timeout = float(os.getenv("TELEGRAM_API_TIMEOUT_SEC", "30"))
        telegram_send_retries = int(os.getenv("TELEGRAM_SEND_RETRIES", "3"))
        telegram_send_retry_delay = float(os.getenv("TELEGRAM_SEND_RETRY_DELAY_SEC", "1.0"))

        self.runner = CodexRunner(
            codex_bin=codex_bin,
            model=codex_model,
            extra_args=codex_extra_args,
            workdir=codex_workdir,
            timeout_sec=codex_timeout,
        )
        self.codex_timeout_sec = codex_timeout
        self.telegram_api_timeout = max(telegram_api_timeout, 1.0)
        self.telegram_send_retries = max(telegram_send_retries, 0)
        self.telegram_send_retry_delay = max(telegram_send_retry_delay, 0.1)

    async def _safe_reply_text(self, message: Optional[Message], text: str) -> bool:
        if message is None:
            return False

        max_attempts = self.telegram_send_retries + 1
        for attempt in range(max_attempts):
            try:
                await message.reply_text(text)
                return True
            except RetryAfter as exc:
                if attempt >= max_attempts - 1:
                    self.logger.warning("reply_text failed after retries due to RetryAfter")
                    return False
                wait_sec = float(getattr(exc, "retry_after", self.telegram_send_retry_delay))
                wait_sec = max(wait_sec, self.telegram_send_retry_delay)
                self.logger.warning(
                    "reply_text rate limited. retrying in %.1fs (%s/%s)",
                    wait_sec,
                    attempt + 1,
                    max_attempts - 1,
                )
                await asyncio.sleep(wait_sec)
            except (TimedOut, NetworkError) as exc:
                if attempt >= max_attempts - 1:
                    self.logger.warning("reply_text failed after retries: %s", exc)
                    return False
                wait_sec = self.telegram_send_retry_delay * (2 ** attempt)
                self.logger.warning(
                    "reply_text transient error. retrying in %.1fs (%s/%s): %s",
                    wait_sec,
                    attempt + 1,
                    max_attempts - 1,
                    exc,
                )
                await asyncio.sleep(wait_sec)
            except Exception:
                self.logger.exception("reply_text unexpected failure")
                return False
        return False

    async def _reply_update_text(self, update: Update, text: str) -> bool:
        return await self._safe_reply_text(update.effective_message, text)

    async def _safe_reply_document(
        self,
        message: Optional[Message],
        file_path: Path,
        caption: Optional[str] = None,
    ) -> bool:
        if message is None:
            return False
        if not file_path.is_file():
            return False

        max_attempts = self.telegram_send_retries + 1
        for attempt in range(max_attempts):
            try:
                with file_path.open("rb") as handle:
                    await message.reply_document(
                        document=handle,
                        filename=file_path.name,
                        caption=caption or None,
                    )
                return True
            except RetryAfter as exc:
                if attempt >= max_attempts - 1:
                    self.logger.warning("reply_document failed after retries due to RetryAfter")
                    return False
                wait_sec = float(getattr(exc, "retry_after", self.telegram_send_retry_delay))
                wait_sec = max(wait_sec, self.telegram_send_retry_delay)
                self.logger.warning(
                    "reply_document rate limited. retrying in %.1fs (%s/%s)",
                    wait_sec,
                    attempt + 1,
                    max_attempts - 1,
                )
                await asyncio.sleep(wait_sec)
            except (TimedOut, NetworkError) as exc:
                if attempt >= max_attempts - 1:
                    self.logger.warning("reply_document failed after retries: %s", exc)
                    return False
                wait_sec = self.telegram_send_retry_delay * (2 ** attempt)
                self.logger.warning(
                    "reply_document transient error. retrying in %.1fs (%s/%s): %s",
                    wait_sec,
                    attempt + 1,
                    max_attempts - 1,
                    exc,
                )
                await asyncio.sleep(wait_sec)
            except Exception:
                self.logger.exception("reply_document unexpected failure")
                return False
        return False

    async def _reply_update_document(
        self,
        update: Update,
        file_path: Path,
        caption: Optional[str] = None,
    ) -> bool:
        return await self._safe_reply_document(update.effective_message, file_path, caption)

    def _build_prompt(self, thread_id: Optional[str], user_text: str, artifact_dir: Path) -> str:
        runtime_context = (
            "[Runtime context]\n"
            f"- codex_workdir={self.runner.workdir}\n"
            f"- artifact_dir={artifact_dir}\n"
            "- If the user asks for a file output, create the file in artifact_dir "
            "and mention the path in your response."
        )
        if not thread_id and self.system_prompt:
            return (
                "[System instruction]\n"
                f"{self.system_prompt}\n\n"
                f"{runtime_context}\n\n"
                "[User message]\n"
                f"{user_text}"
            )
        return f"{runtime_context}\n\n[User message]\n{user_text}"

    def _select_artifacts_to_send(
        self,
        before_state: dict[str, tuple[int, int]],
        after_state: dict[str, tuple[int, int]],
    ) -> tuple[list[Path], int, int]:
        changed: list[tuple[str, tuple[int, int]]] = []
        for path, meta in after_state.items():
            if before_state.get(path) != meta:
                changed.append((path, meta))

        changed.sort(key=lambda item: item[1][0], reverse=True)

        files_to_send: list[Path] = []
        skipped_count_limit = 0
        skipped_count_size = 0
        for file_path, (_, size_bytes) in changed:
            if size_bytes > self.max_return_file_size_bytes:
                skipped_count_size += 1
                continue
            if len(files_to_send) >= self.max_return_files:
                skipped_count_limit += 1
                continue
            files_to_send.append(Path(file_path))

        return files_to_send, skipped_count_limit, skipped_count_size

    async def _send_generated_files_for_chat(
        self,
        update: Update,
        before_state: dict[str, tuple[int, int]],
        after_state: dict[str, tuple[int, int]],
    ) -> None:
        if self.max_return_files <= 0:
            return

        files_to_send, skipped_limit, skipped_size = self._select_artifacts_to_send(
            before_state=before_state,
            after_state=after_state,
        )

        for file_path in files_to_send:
            sent = await self._reply_update_document(update, file_path)
            if not sent:
                self.logger.warning("Failed to send generated file to telegram: %s", file_path)

        if skipped_limit > 0 or skipped_size > 0:
            details: list[str] = []
            if skipped_limit > 0:
                details.append(f"limit-exceeded={skipped_limit}")
            if skipped_size > 0:
                details.append(
                    f"too-large={skipped_size} (max {self.max_return_file_size_mb}MB each)"
                )
            await self._reply_update_text(
                update,
                "Some generated files were not sent: " + ", ".join(details),
            )

    async def _run_codex_for_chat(self, update: Update, chat_id: int, user_text: str) -> None:
        artifact_dir = self.generated_files_dir / str(chat_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        before_state = collect_file_state(artifact_dir)

        thread_id = self.sessions.get_thread_id(chat_id)
        prompt = self._build_prompt(
            thread_id=thread_id,
            user_text=user_text,
            artifact_dir=artifact_dir,
        )

        try:
            reply_text, new_thread_id = await self.runner.run_prompt(prompt=prompt, thread_id=thread_id)
        except Exception as exc:
            self.logger.exception("codex execution failed")
            await self._reply_update_text(update, f"Codex execution failed: {exc}")
            return

        if new_thread_id:
            self.sessions.set_thread_id(chat_id, new_thread_id)

        after_state = collect_file_state(artifact_dir)

        for part in chunk_text(reply_text):
            sent = await self._reply_update_text(update, part)
            if not sent:
                self.logger.warning("Failed to send one or more reply chunks to telegram.")
                break

        await self._send_generated_files_for_chat(
            update=update,
            before_state=before_state,
            after_state=after_state,
        )

    def is_allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        user = update.effective_user

        if self.allowed_chat_ids:
            if chat is None or chat.id not in self.allowed_chat_ids:
                return False

        if self.allowed_user_ids:
            if user is None or user.id not in self.allowed_user_ids:
                return False

        return True

    async def ensure_allowed(self, update: Update) -> bool:
        if self.is_allowed(update):
            return True
        await self._reply_update_text(update, "Access denied for this user/chat.")
        return False

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self.ensure_allowed(update):
            return
        if not update.message:
            return
        await self._reply_update_text(
            update,
            "Send any text and I will reply using codex.\n"
            "If you ask for a file, I will generate and send it as a Telegram document.\n"
            "Use /reset to clear the mapped codex session.\n"
            "Use /session to show current codex thread_id.\n"
            "Use /whoami to print chat_id and user_id.",
        )

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self.ensure_allowed(update):
            return
        if not update.effective_chat or not update.message:
            return

        deleted = self.sessions.delete(update.effective_chat.id)
        if deleted:
            await self._reply_update_text(
                update,
                "Session mapping cleared. Next message starts a new codex thread.",
            )
        else:
            await self._reply_update_text(update, "No mapped session to clear.")

    async def cmd_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self.ensure_allowed(update):
            return
        if not update.effective_chat or not update.message:
            return

        thread_id = self.sessions.get_thread_id(update.effective_chat.id)
        if thread_id:
            await self._reply_update_text(update, f"thread_id={thread_id}")
        else:
            await self._reply_update_text(update, "No mapped codex thread yet.")

    async def cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not update.message:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        await self._reply_update_text(
            update,
            f"chat_id={chat_id}\nuser_id={user_id}\ncodex_timeout_sec={self.codex_timeout_sec}",
        )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self.ensure_allowed(update):
            return
        if not update.message or not update.effective_chat:
            return

        user_text = (update.message.text or "").strip()
        if not user_text:
            return

        chat_id = update.effective_chat.id
        await self._run_codex_for_chat(update=update, chat_id=chat_id, user_text=user_text)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self.ensure_allowed(update):
            return
        if not update.message or not update.effective_chat or not update.message.document:
            return

        chat_id = update.effective_chat.id
        document = update.message.document

        chat_upload_dir = self.upload_dir / str(chat_id)
        chat_upload_dir.mkdir(parents=True, exist_ok=True)

        original_name = (document.file_name or "").strip()
        safe_name = sanitize_filename(original_name) if original_name else ""
        if safe_name:
            saved_name = f"{document.file_unique_id}__{safe_name}"
        else:
            saved_name = document.file_unique_id
        save_path = chat_upload_dir / saved_name

        try:
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=str(save_path))
        except Exception as exc:
            self.logger.exception("document download failed")
            await self._reply_update_text(update, f"Failed to download file: {exc}")
            return

        caption = (update.message.caption or "").strip()
        prompt_lines = [
            "[Uploaded file]",
            f"path={save_path}",
            f"name={original_name or saved_name}",
            f"mime_type={document.mime_type or 'unknown'}",
            f"size_bytes={document.file_size if document.file_size is not None else 'unknown'}",
        ]
        if caption:
            prompt_lines.extend(
                [
                    "",
                    "[User instruction]",
                    caption,
                ]
            )
        else:
            prompt_lines.extend(
                [
                    "",
                    "[User instruction]",
                    "Analyze this file and summarize key points.",
                ]
            )

        await self._run_codex_for_chat(
            update=update,
            chat_id=chat_id,
            user_text="\n".join(prompt_lines),
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.exception("Unhandled telegram exception", exc_info=context.error)
        if isinstance(update, Update):
            await self._reply_update_text(update, "Temporary error occurred. Please try again.")

    def build_application(self) -> Application:
        builder = (
            ApplicationBuilder()
            .token(self.telegram_token)
            .connect_timeout(self.telegram_api_timeout)
            .read_timeout(self.telegram_api_timeout)
            .write_timeout(self.telegram_api_timeout)
            .pool_timeout(self.telegram_api_timeout)
            .media_write_timeout(self.telegram_api_timeout)
            .get_updates_connect_timeout(self.telegram_api_timeout)
            .get_updates_read_timeout(self.telegram_api_timeout)
            .get_updates_write_timeout(self.telegram_api_timeout)
            .get_updates_pool_timeout(self.telegram_api_timeout)
        )
        app = builder.build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("session", self.cmd_session))
        app.add_handler(CommandHandler("whoami", self.cmd_whoami))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app

    def run(self) -> None:
        app = self.build_application()
        self.logger.info("Bot started with codex backend")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    bot = TelegramCodexBot()
    bot.run()


if __name__ == "__main__":
    main()
