import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
from telegram.ext import MessageHandler, filters

TASK_DEFAULT = "default"
TASK_CODE = "code"
TASK_SURVEY = "survey"
AGENT_CODEX = "codex"
AGENT_CLAUDE = "claude"
AGENT_GEMINI = "gemini"
SUPPORTED_AGENTS = (AGENT_CODEX, AGENT_CLAUDE, AGENT_GEMINI)


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


def truncate_for_prompt(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    return clipped + "\n\n[Truncated due to size limit]"


def looks_like_code_content(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False

    if "```" in text:
        return True

    code_patterns = [
        r"\bdef\s+\w+\s*\(",
        r"\bclass\s+\w+",
        r"\bimport\s+\w+",
        r"\bfrom\s+\w+\s+import\b",
        r"\bfunction\s+\w+\s*\(",
        r"\bconst\s+\w+\s*=",
        r"\blet\s+\w+\s*=",
        r"\bvar\s+\w+\s*=",
        r"\bSELECT\b.+\bFROM\b",
        r"\bpublic\s+static\s+void\s+main\b",
        r"\bconsole\.log\s*\(",
        r"\bTraceback\b",
        r"\bException\b",
        r"\bpytest\b",
    ]
    for pattern in code_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            return True

    if re.search(r"\b\w+\.(py|js|ts|tsx|java|go|rs|cpp|c|cs|sql|sh|yaml|yml|json|toml)\b", text):
        return True

    return False


def is_survey_request(user_text: str) -> bool:
    lowered = user_text.lower()
    survey_keywords = [
        "조사",
        "리서치",
        "research",
        "survey",
        "서베이",
        "검색해",
        "찾아봐",
        "웹서칭",
        "논문",
        "arxiv",
    ]
    return any(token in lowered for token in survey_keywords)


def classify_task(user_text: str) -> str:
    if looks_like_code_content(user_text):
        return TASK_CODE
    if is_survey_request(user_text):
        return TASK_SURVEY
    return TASK_DEFAULT


def strip_cli_noise(text: str) -> str:
    noise_prefixes = (
        "Warning: True color",
        "Ripgrep is not available",
        "YOLO mode is enabled.",
        "Reading additional input from stdin",
    )
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(noise_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_agent_sessions (
                    chat_id INTEGER NOT NULL,
                    agent TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, agent)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_models (
                    chat_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_agent_models (
                    chat_id INTEGER NOT NULL,
                    agent TEXT NOT NULL,
                    model TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, agent)
                )
                """
            )
            # Backfill legacy codex thread mappings if present.
            conn.execute(
                """
                INSERT INTO chat_agent_sessions (chat_id, agent, session_id, updated_at)
                SELECT chat_id, 'codex', thread_id, updated_at
                FROM sessions
                WHERE thread_id IS NOT NULL AND TRIM(thread_id) != ''
                ON CONFLICT(chat_id, agent) DO NOTHING
                """
            )
            # Backfill legacy codex overrides if present.
            conn.execute(
                """
                INSERT INTO chat_agent_models (chat_id, agent, model, updated_at)
                SELECT chat_id, 'codex', model, updated_at
                FROM chat_models
                WHERE 1
                ON CONFLICT(chat_id, agent) DO NOTHING
                """
            )
            conn.commit()

    def get_agent_session_id(self, chat_id: int, agent: str) -> Optional[str]:
        normalized_agent = self._validate_agent(agent)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM chat_agent_sessions WHERE chat_id = ? AND agent = ?",
                (chat_id, normalized_agent),
            ).fetchone()
        if row is None:
            return None
        session_id = str(row[0]).strip()
        return session_id if session_id else None

    def set_agent_session_id(self, chat_id: int, agent: str, session_id: str) -> None:
        normalized_agent = self._validate_agent(agent)
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_agent_sessions (chat_id, agent, session_id, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, agent) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, normalized_agent, normalized_session_id),
            )
            conn.commit()

    def clear_agent_session_id(self, chat_id: int, agent: str) -> bool:
        normalized_agent = self._validate_agent(agent)
        deleted_rows = 0
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM chat_agent_sessions WHERE chat_id = ? AND agent = ?",
                (chat_id, normalized_agent),
            )
            deleted_rows += cursor.rowcount
            if normalized_agent == AGENT_CODEX:
                legacy_cursor = conn.execute(
                    "DELETE FROM sessions WHERE chat_id = ?",
                    (chat_id,),
                )
                deleted_rows += legacy_cursor.rowcount
            conn.commit()
        return deleted_rows > 0

    def list_agent_sessions(self, chat_id: int) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent, session_id FROM chat_agent_sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            agent = str(row[0]).strip().lower()
            session_id = str(row[1]).strip()
            if agent in SUPPORTED_AGENTS and session_id:
                result[agent] = session_id
        return result

    def clear_all_sessions(self, chat_id: int) -> bool:
        deleted_rows = 0
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM chat_agent_sessions WHERE chat_id = ?",
                (chat_id,),
            )
            deleted_rows += cursor.rowcount
            legacy_cursor = conn.execute(
                "DELETE FROM sessions WHERE chat_id = ?",
                (chat_id,),
            )
            deleted_rows += legacy_cursor.rowcount
            conn.commit()
        return deleted_rows > 0

    def get_thread_id(self, chat_id: int) -> Optional[str]:
        mapped = self.get_agent_session_id(chat_id, AGENT_CODEX)
        if mapped:
            return mapped

        # Legacy fallback for pre-agent-session rows.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        thread_id = str(row[0]).strip()
        if not thread_id:
            return None
        self.set_agent_session_id(chat_id, AGENT_CODEX, thread_id)
        return thread_id

    def set_thread_id(self, chat_id: int, thread_id: str) -> None:
        normalized_thread_id = thread_id.strip()
        if not normalized_thread_id:
            raise ValueError("thread_id must not be empty")

        self.set_agent_session_id(chat_id, AGENT_CODEX, normalized_thread_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (chat_id, thread_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, normalized_thread_id),
            )
            conn.commit()

    def delete(self, chat_id: int) -> bool:
        return self.clear_all_sessions(chat_id)

    @staticmethod
    def _validate_agent(agent: str) -> str:
        normalized = agent.strip().lower()
        if normalized not in SUPPORTED_AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
        return normalized

    def get_agent_model(self, chat_id: int, agent: str) -> Optional[str]:
        normalized_agent = self._validate_agent(agent)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT model FROM chat_agent_models WHERE chat_id = ? AND agent = ?",
                (chat_id, normalized_agent),
            ).fetchone()
        if row is None:
            return None
        model = str(row[0]).strip()
        return model if model else None

    def set_agent_model(self, chat_id: int, agent: str, model: str) -> None:
        normalized_agent = self._validate_agent(agent)
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("model must not be empty")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_agent_models (chat_id, agent, model, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, agent) DO UPDATE SET
                    model = excluded.model,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, normalized_agent, normalized_model),
            )
            conn.commit()

    def clear_agent_model(self, chat_id: int, agent: str) -> bool:
        normalized_agent = self._validate_agent(agent)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM chat_agent_models WHERE chat_id = ? AND agent = ?",
                (chat_id, normalized_agent),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_chat_model(self, chat_id: int) -> Optional[str]:
        model = self.get_agent_model(chat_id, AGENT_CODEX)
        if model:
            return model

        # Legacy fallback for old DB rows before migration.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT model FROM chat_models WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        model = str(row[0]).strip()
        return model if model else None

    def set_chat_model(self, chat_id: int, model: str) -> None:
        self.set_agent_model(chat_id, AGENT_CODEX, model)

    def clear_chat_model(self, chat_id: int) -> bool:
        return self.clear_agent_model(chat_id, AGENT_CODEX)


class CodexRunner:
    def __init__(
        self,
        codex_bin: str,
        default_model: str,
        extra_args: list[str],
        workdir: Path,
        timeout_sec: int,
    ) -> None:
        self.codex_bin = codex_bin
        self.default_model = default_model.strip()
        self.extra_args = extra_args
        self.workdir = workdir
        self.timeout_sec = timeout_sec

    def _build_command(
        self,
        prompt: str,
        thread_id: Optional[str],
        output_last_message_path: str,
        model: str,
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

        if model:
            cmd.extend(["--model", model])
        cmd.extend(self.extra_args)

        if thread_id:
            cmd.extend([thread_id, prompt])
        else:
            cmd.append(prompt)
        return cmd

    async def run_prompt(
        self,
        prompt: str,
        thread_id: Optional[str],
        model: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        output_fd, output_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
        os.close(output_fd)

        try:
            resolved_model = (model or "").strip() or self.default_model
            cmd = self._build_command(prompt, thread_id, output_path, model=resolved_model)
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

            return strip_cli_noise(reply_text), new_thread_id
        finally:
            try:
                os.remove(output_path)
            except OSError:
                pass


class CliTextRunner:
    def __init__(
        self,
        label: str,
        cli_bin: str,
        default_model: str,
        extra_args: list[str],
        workdir: Path,
        timeout_sec: int,
        base_args: list[str],
    ) -> None:
        self.label = label
        self.cli_bin = cli_bin
        self.default_model = default_model.strip()
        self.extra_args = extra_args
        self.workdir = workdir
        self.timeout_sec = timeout_sec
        self.base_args = list(base_args)

    def _build_command(
        self,
        prompt: str,
        model: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[str]:
        cmd = [self.cli_bin, *self.base_args]
        resolved_resume_session_id = (resume_session_id or "").strip()
        resolved_session_id = (session_id or "").strip()
        if resolved_resume_session_id:
            cmd.extend(["--resume", resolved_resume_session_id])
        elif resolved_session_id:
            cmd.extend(["--session-id", resolved_session_id])

        resolved_model = (model or "").strip() or self.default_model
        if resolved_model:
            if self.label == "gemini":
                cmd.extend(["-m", resolved_model])
            else:
                cmd.extend(["--model", resolved_model])
        cmd.extend(self.extra_args)
        if self.label == "gemini":
            cmd.extend(["-p", prompt])
        else:
            cmd.append(prompt)
        return cmd

    async def run_prompt(
        self,
        prompt: str,
        model: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        cmd = self._build_command(
            prompt=prompt,
            model=model,
            resume_session_id=resume_session_id,
            session_id=session_id,
        )
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        if self.label == "gemini":
            env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_sec
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"{self.label} command timed out after {self.timeout_sec}s")

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err = stderr_text.strip() or stdout_text.strip() or f"Unknown {self.label} error"
            raise RuntimeError(err)

        cleaned = strip_cli_noise(stdout_text)
        if cleaned:
            return cleaned

        fallback = strip_cli_noise(stderr_text)
        if fallback:
            return fallback

        return f"{self.label} returned no text output."


class TelegramCodexBot:
    def __init__(self) -> None:
        load_dotenv()

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        self.logger = logging.getLogger("telegram-cerberus-bot")

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
        codex_model = os.getenv("CODEX_MODEL", "gpt-5.5").strip()
        codex_extra_args = shlex.split(os.getenv("CODEX_EXTRA_ARGS", ""))
        codex_workdir = Path(os.getenv("CODEX_WORKDIR", ".")).expanduser().resolve()
        codex_timeout = int(os.getenv("CODEX_TIMEOUT_SEC", "300"))

        claude_bin = os.getenv("CLAUDE_BIN", "claude").strip() or "claude"
        claude_model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip()
        claude_extra_args = shlex.split(os.getenv("CLAUDE_EXTRA_ARGS", ""))
        claude_timeout = int(os.getenv("CLAUDE_TIMEOUT_SEC", str(codex_timeout)))
        claude_permission_mode = os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()
        is_root_user = hasattr(os, "geteuid") and os.geteuid() == 0
        if claude_permission_mode == "bypassPermissions" and is_root_user:
            self.logger.warning(
                "CLAUDE_PERMISSION_MODE=bypassPermissions is unsupported as root; "
                "falling back to acceptEdits."
            )
            claude_permission_mode = "acceptEdits"

        gemini_bin = os.getenv("GEMINI_BIN", "gemini").strip() or "gemini"
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-3-pro-preview").strip()
        gemini_extra_args = shlex.split(os.getenv("GEMINI_EXTRA_ARGS", ""))
        gemini_timeout = int(os.getenv("GEMINI_TIMEOUT_SEC", str(codex_timeout)))
        gemini_approval_mode = os.getenv("GEMINI_APPROVAL_MODE", "yolo").strip()

        telegram_api_timeout = float(os.getenv("TELEGRAM_API_TIMEOUT_SEC", "30"))
        telegram_send_retries = int(os.getenv("TELEGRAM_SEND_RETRIES", "3"))
        telegram_send_retry_delay = float(os.getenv("TELEGRAM_SEND_RETRY_DELAY_SEC", "1.0"))

        self.codex_runner = CodexRunner(
            codex_bin=codex_bin,
            default_model=codex_model,
            extra_args=codex_extra_args,
            workdir=codex_workdir,
            timeout_sec=codex_timeout,
        )

        claude_base_args = ["-p", "--output-format", "text"]
        if claude_permission_mode:
            claude_base_args.extend(["--permission-mode", claude_permission_mode])

        self.claude_runner = CliTextRunner(
            label="claude",
            cli_bin=claude_bin,
            default_model=claude_model,
            extra_args=claude_extra_args,
            workdir=codex_workdir,
            timeout_sec=claude_timeout,
            base_args=claude_base_args,
        )

        gemini_base_args = ["--output-format", "text"]
        if gemini_approval_mode:
            gemini_base_args.extend(["--approval-mode", gemini_approval_mode])

        self.gemini_runner = CliTextRunner(
            label="gemini",
            cli_bin=gemini_bin,
            default_model=gemini_model,
            extra_args=gemini_extra_args,
            workdir=codex_workdir,
            timeout_sec=gemini_timeout,
            base_args=gemini_base_args,
        )

        self.codex_timeout_sec = codex_timeout
        self.codex_workdir = codex_workdir
        self.claude_timeout_sec = claude_timeout
        self.gemini_timeout_sec = gemini_timeout
        self.default_model_by_agent = {
            AGENT_CODEX: self.codex_runner.default_model,
            AGENT_CLAUDE: self.claude_runner.default_model,
            AGENT_GEMINI: self.gemini_runner.default_model,
        }

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

    async def _send_typing_action(self, update: Update) -> None:
        message = update.effective_message
        if message is None:
            return
        try:
            await message.reply_chat_action(action=ChatAction.TYPING)
        except Exception:
            # Typing indicator failures should not affect main request handling.
            self.logger.debug("typing action failed", exc_info=True)

    async def _typing_heartbeat(self, update: Update, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._send_typing_action(update)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue

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

    def _runtime_context(self, artifact_dir: Path) -> str:
        return (
            "[Runtime context]\n"
            f"- codex_workdir={self.codex_workdir}\n"
            f"- artifact_dir={artifact_dir}\n"
            "- If a file output is requested, create it under artifact_dir and mention the path."
        )

    def _build_codex_direct_prompt(
        self,
        thread_id: Optional[str],
        user_text: str,
        artifact_dir: Path,
    ) -> str:
        body = (
            f"{self._runtime_context(artifact_dir)}\n\n"
            "[User request]\n"
            f"{user_text}"
        )
        if not thread_id and self.system_prompt:
            return (
                "[System instruction]\n"
                f"{self.system_prompt}\n\n"
                f"{body}"
            )
        return body

    def _build_codex_coding_prompt(
        self,
        thread_id: Optional[str],
        user_text: str,
        artifact_dir: Path,
    ) -> str:
        instructions = (
            "[Role]\n"
            "You are the Codex implementation stage in a 3-agent pipeline.\n"
            "Your job: implement the user's coding request directly in the workspace, run minimal validation, and summarize concrete changes.\n"
            "Do not skip implementation unless blocked."
        )
        body = (
            f"{instructions}\n\n"
            f"{self._runtime_context(artifact_dir)}\n\n"
            "[User request]\n"
            f"{user_text}"
        )

        if not thread_id and self.system_prompt:
            return (
                "[System instruction]\n"
                f"{self.system_prompt}\n\n"
                f"{body}"
            )
        return body

    def _build_codex_survey_prompt(self, user_text: str, artifact_dir: Path) -> str:
        return (
            "[Role]\n"
            "You are Codex collector for survey task.\n"
            "Collect high-value facts and output only structured sections.\n"
            "If source verification is weak, state uncertainty explicitly.\n\n"
            f"{self._runtime_context(artifact_dir)}\n\n"
            "[Required output]\n"
            "1) Key Findings\n"
            "2) Evidence (bullet list with source title + URL + date if available)\n"
            "3) Open Risks / Uncertainty\n"
            "4) Confidence (0-100)\n\n"
            "[User request]\n"
            f"{user_text}"
        )

    def _build_claude_coding_review_prompt(
        self,
        user_text: str,
        codex_result: str,
        artifact_dir: Path,
    ) -> str:
        return (
            "You are Claude review-and-fix stage in a coding pipeline.\n"
            "Inputs:\n"
            "- User request\n"
            "- Codex implementation summary\n\n"
            "Task:\n"
            "1) Review codex output for bugs, regressions, and missing validation.\n"
            "2) Apply required fixes directly in workspace when needed.\n"
            "3) Run or propose focused checks.\n"
            "4) Return concise report with sections:\n"
            "- Findings\n"
            "- Fixes Applied\n"
            "- Remaining Risks\n"
            "- Final Status\n\n"
            f"artifact_dir={artifact_dir}\n\n"
            "[User request]\n"
            f"{user_text}\n\n"
            "[Codex implementation summary]\n"
            f"{truncate_for_prompt(codex_result)}"
        )

    def _build_gemini_coding_assist_prompt(
        self,
        user_text: str,
        codex_result: str,
        claude_result: str,
    ) -> str:
        return (
            "You are Gemini assist stage in a coding pipeline.\n"
            "Do not rewrite the full solution. Provide high-leverage assistance only.\n"
            "Output sections:\n"
            "- Edge Cases\n"
            "- Test Ideas\n"
            "- Simplification Opportunities\n"
            "- Confidence (0-100)\n\n"
            "[User request]\n"
            f"{user_text}\n\n"
            "[Codex summary]\n"
            f"{truncate_for_prompt(codex_result, 8000)}\n\n"
            "[Claude review/fix]\n"
            f"{truncate_for_prompt(claude_result, 8000)}"
        )

    def _build_gemini_survey_prompt(self, user_text: str) -> str:
        return (
            "You are Gemini collector in a two-collector plus one-validator pipeline.\n"
            "Task type: survey\n"
            "Return only:\n"
            "1) Key Findings\n"
            "2) Evidence (source title + URL + date)\n"
            "3) Open Risks / Uncertainty\n"
            "4) Confidence (0-100)\n\n"
            "[User request]\n"
            f"{user_text}"
        )

    def _build_claude_validation_prompt(
        self,
        user_text: str,
        codex_result: str,
        gemini_result: str,
    ) -> str:
        return (
            "You are Claude final validator and judge.\n"
            "Task type: survey\n"
            "You are given two independent collector outputs (Codex and Gemini).\n"
            "Cross-check them, resolve conflicts, and produce the best final answer.\n"
            "When uncertain, explicitly say uncertain.\n\n"
            "Output sections:\n"
            "1) Final Answer\n"
            "2) Verified Facts\n"
            "3) Conflicts and Resolution\n"
            "4) Sources (URL list)\n"
            "5) Residual Uncertainty\n\n"
            "[User request]\n"
            f"{user_text}\n\n"
            "[Codex collector]\n"
            f"{truncate_for_prompt(codex_result)}\n\n"
            "[Gemini collector]\n"
            f"{truncate_for_prompt(gemini_result)}"
        )

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

    @staticmethod
    def _normalize_agent_name(token: str) -> Optional[str]:
        normalized = token.strip().lower()
        if normalized in SUPPORTED_AGENTS:
            return normalized
        return None

    def _resolve_agent_model_for_chat(self, chat_id: int, agent: str) -> tuple[str, str]:
        normalized_agent = self._normalize_agent_name(agent)
        if normalized_agent is None:
            return "", "unsupported_agent"

        chat_model = self.sessions.get_agent_model(chat_id, normalized_agent)
        if chat_model:
            return chat_model, "chat_override"

        default_model = self.default_model_by_agent.get(normalized_agent, "").strip()
        if default_model:
            return default_model, f"{normalized_agent.upper()}_MODEL"

        return "", f"{normalized_agent}_default"

    def _format_model_status(self, chat_id: int) -> str:
        lines: list[str] = []
        for agent in SUPPORTED_AGENTS:
            effective_model, source = self._resolve_agent_model_for_chat(chat_id, agent)
            override_model = self.sessions.get_agent_model(chat_id, agent)
            default_model = self.default_model_by_agent.get(agent, "")

            lines.extend(
                [
                    f"[{agent}]",
                    f"effective_model={effective_model or '(none)'}",
                    f"effective_source={source}",
                    f"chat_override={override_model or '(none)'}",
                    f"default_env_model={default_model or '(none)'}",
                    "",
                ]
            )

        lines.append("usage: /model <name> (legacy: codex)")
        lines.append("usage: /model <agent> <name> | /model <agent> clear")
        return "\n".join(lines)

    @staticmethod
    def _looks_like_missing_session_error(error_text: str) -> bool:
        normalized = error_text.lower()
        tokens = (
            "no conversation found",
            "session not found",
            "unknown session",
            "invalid session",
            "not a resumable session",
            "failed to resume",
            "cannot resume",
        )
        return any(token in normalized for token in tokens)

    async def _run_stateful_text_agent(
        self,
        chat_id: int,
        agent: str,
        runner: CliTextRunner,
        prompt: str,
        model: str,
    ) -> str:
        existing_session_id = self.sessions.get_agent_session_id(chat_id, agent)
        if existing_session_id:
            try:
                return await runner.run_prompt(
                    prompt=prompt,
                    model=model,
                    resume_session_id=existing_session_id,
                )
            except RuntimeError as exc:
                if not self._looks_like_missing_session_error(str(exc)):
                    raise
                self.logger.warning(
                    "%s resume failed for chat_id=%s session_id=%s; rotating session: %s",
                    agent,
                    chat_id,
                    existing_session_id,
                    exc,
                )

        new_session_id = str(uuid.uuid4())
        reply = await runner.run_prompt(
            prompt=prompt,
            model=model,
            session_id=new_session_id,
        )
        self.sessions.set_agent_session_id(chat_id, agent, new_session_id)
        return reply

    async def _run_coding_pipeline(self, update: Update, chat_id: int, user_text: str) -> str:
        artifact_dir = self.generated_files_dir / str(chat_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        thread_id = self.sessions.get_thread_id(chat_id)
        codex_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_CODEX)
        claude_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_CLAUDE)
        gemini_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_GEMINI)

        codex_prompt = self._build_codex_coding_prompt(
            thread_id=thread_id,
            user_text=user_text,
            artifact_dir=artifact_dir,
        )

        codex_result, new_thread_id = await self.codex_runner.run_prompt(
            prompt=codex_prompt,
            thread_id=thread_id,
            model=codex_model,
        )
        if new_thread_id:
            self.sessions.set_thread_id(chat_id, new_thread_id)

        claude_prompt = self._build_claude_coding_review_prompt(
            user_text=user_text,
            codex_result=codex_result,
            artifact_dir=artifact_dir,
        )
        claude_result = await self._run_stateful_text_agent(
            chat_id=chat_id,
            agent=AGENT_CLAUDE,
            runner=self.claude_runner,
            prompt=claude_prompt,
            model=claude_model,
        )

        gemini_prompt = self._build_gemini_coding_assist_prompt(
            user_text=user_text,
            codex_result=codex_result,
            claude_result=claude_result,
        )
        gemini_result = await self._run_stateful_text_agent(
            chat_id=chat_id,
            agent=AGENT_GEMINI,
            runner=self.gemini_runner,
            prompt=gemini_prompt,
            model=gemini_model,
        )

        return (
            "[Routing]\n"
            "category=code\n"
            "pipeline=codex(implement) -> claude(review/fix) -> gemini(assist)\n\n"
            "[Codex]\n"
            f"{codex_result}\n\n"
            "[Claude]\n"
            f"{claude_result}\n\n"
            "[Gemini]\n"
            f"{gemini_result}"
        )

    async def _run_codex_direct(self, chat_id: int, user_text: str) -> str:
        artifact_dir = self.generated_files_dir / str(chat_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        thread_id = self.sessions.get_thread_id(chat_id)
        codex_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_CODEX)
        prompt = self._build_codex_direct_prompt(
            thread_id=thread_id,
            user_text=user_text,
            artifact_dir=artifact_dir,
        )
        reply_text, new_thread_id = await self.codex_runner.run_prompt(
            prompt=prompt,
            thread_id=thread_id,
            model=codex_model,
        )
        if new_thread_id:
            self.sessions.set_thread_id(chat_id, new_thread_id)
        return reply_text

    async def _run_survey_pipeline(self, update: Update, chat_id: int, user_text: str) -> str:
        del update
        artifact_dir = self.generated_files_dir / str(chat_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        codex_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_CODEX)
        claude_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_CLAUDE)
        gemini_model, _ = self._resolve_agent_model_for_chat(chat_id, AGENT_GEMINI)
        codex_prompt = self._build_codex_survey_prompt(user_text=user_text, artifact_dir=artifact_dir)
        gemini_prompt = self._build_gemini_survey_prompt(user_text=user_text)

        codex_task = self.codex_runner.run_prompt(
            prompt=codex_prompt,
            thread_id=None,
            model=codex_model,
        )
        gemini_task = self._run_stateful_text_agent(
            chat_id=chat_id,
            agent=AGENT_GEMINI,
            runner=self.gemini_runner,
            prompt=gemini_prompt,
            model=gemini_model,
        )

        codex_result = ""
        gemini_result = ""

        codex_exc: Optional[BaseException] = None
        gemini_exc: Optional[BaseException] = None

        codex_out, gemini_out = await asyncio.gather(codex_task, gemini_task, return_exceptions=True)

        if isinstance(codex_out, Exception):
            codex_exc = codex_out
            self.logger.warning("survey codex collector failed: %s", codex_exc)
        else:
            codex_result = codex_out[0]

        if isinstance(gemini_out, Exception):
            gemini_exc = gemini_out
            self.logger.warning("survey gemini collector failed: %s", gemini_exc)
        else:
            gemini_result = gemini_out

        if codex_exc and gemini_exc:
            raise RuntimeError(
                f"collector failed: codex={codex_exc}; gemini={gemini_exc}"
            )

        if not codex_result:
            codex_result = f"Codex collector failed: {codex_exc}"
        if not gemini_result:
            gemini_result = f"Gemini collector failed: {gemini_exc}"

        claude_prompt = self._build_claude_validation_prompt(
            user_text=user_text,
            codex_result=codex_result,
            gemini_result=gemini_result,
        )
        claude_result = await self._run_stateful_text_agent(
            chat_id=chat_id,
            agent=AGENT_CLAUDE,
            runner=self.claude_runner,
            prompt=claude_prompt,
            model=claude_model,
        )

        return (
            "[Routing]\n"
            "category=survey\n"
            "pipeline=gemini+codex(collect) -> claude(validate/judge)\n\n"
            "[Final by Claude]\n"
            f"{claude_result}"
        )

    async def _run_pipeline_for_task(
        self,
        update: Update,
        chat_id: int,
        user_text: str,
        task_kind: str,
    ) -> None:
        artifact_dir = self.generated_files_dir / str(chat_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        before_state = collect_file_state(artifact_dir)
        reply_text = ""

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_heartbeat(update, stop_typing))
        try:
            if task_kind == TASK_CODE:
                reply_text = "[code]\n" + await self._run_coding_pipeline(update, chat_id, user_text)
            elif task_kind == TASK_SURVEY:
                reply_text = "[survey]\n" + await self._run_survey_pipeline(update, chat_id, user_text)
            elif task_kind == TASK_DEFAULT:
                reply_text = await self._run_codex_direct(chat_id, user_text)
            else:
                await self._reply_update_text(
                    update,
                    "Unsupported task kind.",
                )
                return
        except Exception as exc:
            self.logger.exception("pipeline execution failed")
            await self._reply_update_text(update, f"Pipeline execution failed: {exc}")
            return
        finally:
            stop_typing.set()
            try:
                await typing_task
            except Exception:
                self.logger.debug("typing heartbeat cleanup failed", exc_info=True)

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

    def _resolve_task_kind(self, user_text: str) -> str:
        return classify_task(user_text)

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
            "Send any text and I will route it automatically.\n"
            "- default: codex direct response\n"
            "- code task (code content detected): codex -> claude -> gemini, prefixed with [code]\n"
            "- survey task (조사/리서치 request): gemini+codex -> claude, prefixed with [survey]\n"
            "Use /model <agent> <name> to override model (agent: codex|claude|gemini).\n"
            "Use /model <agent> clear to reset override.\n"
            "Use /session to view mapped sessions.\n"
            "Use /reset [agent|all] to clear mapped sessions.",
        )

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_allowed(update):
            return
        if not update.effective_chat or not update.message:
            return

        chat_id = update.effective_chat.id
        args = context.args or []
        if not args:
            await self._reply_update_text(update, self._format_model_status(chat_id))
            return

        clear_tokens = {"clear", "reset", "default", "unset"}
        first = args[0].strip().lower()
        agent = self._normalize_agent_name(first)

        # New syntax: /model <agent> <name> | /model <agent> clear
        if agent is not None:
            if len(args) == 1:
                await self._reply_update_text(
                    update,
                    "Usage:\n"
                    "/model <agent> <name>\n"
                    "/model <agent> clear\n"
                    f"agents={','.join(SUPPORTED_AGENTS)}\n\n"
                    + self._format_model_status(chat_id),
                )
                return

            requested_model = " ".join(args[1:]).strip()
            if not requested_model:
                await self._reply_update_text(update, self._format_model_status(chat_id))
                return

            if requested_model.lower() in clear_tokens:
                cleared = self.sessions.clear_agent_model(chat_id, agent)
                prefix = (
                    f"{agent} model override cleared."
                    if cleared
                    else f"No {agent} model override to clear."
                )
                await self._reply_update_text(
                    update,
                    f"{prefix}\n{self._format_model_status(chat_id)}",
                )
                return

            self.sessions.set_agent_model(chat_id, agent, requested_model)
            await self._reply_update_text(
                update,
                f"{agent} model override updated.\n{self._format_model_status(chat_id)}",
            )
            return

        # Legacy syntax: /model <name> (codex only)
        requested_model = " ".join(args).strip()
        if not requested_model:
            await self._reply_update_text(update, self._format_model_status(chat_id))
            return

        if requested_model.lower() in clear_tokens:
            cleared = self.sessions.clear_agent_model(chat_id, AGENT_CODEX)
            prefix = "codex model override cleared." if cleared else "No codex model override to clear."
            await self._reply_update_text(
                update,
                f"{prefix}\n{self._format_model_status(chat_id)}",
            )
            return

        self.sessions.set_agent_model(chat_id, AGENT_CODEX, requested_model)
        await self._reply_update_text(
            update,
            "codex model override updated (legacy syntax).\n" + self._format_model_status(chat_id),
        )

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_allowed(update):
            return
        if not update.effective_chat or not update.message:
            return

        chat_id = update.effective_chat.id
        args = context.args or []
        target = (args[0].strip().lower() if args else "all")

        if target in {"all", "*"}:
            deleted = self.sessions.clear_all_sessions(chat_id)
            if deleted:
                await self._reply_update_text(
                    update,
                    "All mapped sessions cleared. Next request per agent starts a new session.",
                )
            else:
                await self._reply_update_text(update, "No mapped sessions to clear.")
            return

        agent = self._normalize_agent_name(target)
        if agent is None:
            await self._reply_update_text(
                update,
                "Usage:\n/reset\n/reset all\n/reset <agent>\n"
                f"agents={','.join(SUPPORTED_AGENTS)}",
            )
            return

        deleted = self.sessions.clear_agent_session_id(chat_id, agent)
        if deleted:
            await self._reply_update_text(
                update,
                f"{agent} session cleared. Next {agent} request starts a new session.",
            )
        else:
            await self._reply_update_text(update, f"No mapped {agent} session to clear.")

    async def cmd_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self.ensure_allowed(update):
            return
        if not update.effective_chat or not update.message:
            return

        chat_id = update.effective_chat.id
        mapped = self.sessions.list_agent_sessions(chat_id)
        lines: list[str] = []
        for agent in SUPPORTED_AGENTS:
            lines.append(f"{agent}_session_id={mapped.get(agent, '(none)')}")
        await self._reply_update_text(update, "\n".join(lines))

    async def cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not update.message:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None

        codex_model = ""
        codex_source = ""
        claude_model = ""
        claude_source = ""
        gemini_model = ""
        gemini_source = ""
        mapped_sessions: dict[str, str] = {}
        if chat_id is not None:
            codex_model, codex_source = self._resolve_agent_model_for_chat(chat_id, AGENT_CODEX)
            claude_model, claude_source = self._resolve_agent_model_for_chat(chat_id, AGENT_CLAUDE)
            gemini_model, gemini_source = self._resolve_agent_model_for_chat(chat_id, AGENT_GEMINI)
            mapped_sessions = self.sessions.list_agent_sessions(chat_id)

        await self._reply_update_text(
            update,
            (
                f"chat_id={chat_id}\n"
                f"user_id={user_id}\n"
                f"codex_workdir={self.codex_workdir}\n"
                f"codex_timeout_sec={self.codex_timeout_sec}\n"
                f"claude_timeout_sec={self.claude_timeout_sec}\n"
                f"gemini_timeout_sec={self.gemini_timeout_sec}\n"
                f"codex_model={codex_model or '(codex default)'}\n"
                f"codex_model_source={codex_source or '(none)'}\n"
                f"claude_model={claude_model or '(none)'}\n"
                f"claude_model_source={claude_source or '(none)'}\n"
                f"gemini_model={gemini_model or '(none)'}\n"
                f"gemini_model_source={gemini_source or '(none)'}\n"
                f"codex_session_id={mapped_sessions.get(AGENT_CODEX, '(none)')}\n"
                f"claude_session_id={mapped_sessions.get(AGENT_CLAUDE, '(none)')}\n"
                f"gemini_session_id={mapped_sessions.get(AGENT_GEMINI, '(none)')}"
            ),
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
        task_kind = self._resolve_task_kind(user_text)

        await self._run_pipeline_for_task(
            update=update,
            chat_id=chat_id,
            user_text=user_text,
            task_kind=task_kind,
        )

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
            "",
            "[User instruction]",
            caption if caption else "Analyze this file and summarize key points.",
        ]

        user_text = "\n".join(prompt_lines)
        task_kind = self._resolve_task_kind(user_text)

        await self._run_pipeline_for_task(
            update=update,
            chat_id=chat_id,
            user_text=user_text,
            task_kind=task_kind,
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
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("session", self.cmd_session))
        app.add_handler(CommandHandler("whoami", self.cmd_whoami))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app

    def run(self) -> None:
        app = self.build_application()
        self.logger.info("Bot started with multi-agent backend (codex+claude+gemini)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    bot = TelegramCodexBot()
    bot.run()


if __name__ == "__main__":
    main()
