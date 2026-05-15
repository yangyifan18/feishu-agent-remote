import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Confirmation, RemoteAgent, RunRecord, SessionBinding


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id TEXT NOT NULL,
                    thread_key TEXT NOT NULL,
                    agent_id TEXT,
                    repo_alias TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    codex_session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_key)
                )
                """
            )
            self._ensure_column(conn, "sessions", "agent_id", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmations (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    requester_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS remote_agents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    repo_alias TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    codex_session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_key TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "remote_agents", "last_run_id", "TEXT")
            self._ensure_column(conn, "remote_agents", "last_error", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    repo_alias TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    codex_session_id TEXT,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    summary TEXT,
                    error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    started_at DATETIME,
                    finished_at DATETIME
                )
                """
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

    def upsert_session(
        self,
        chat_id: str,
        thread_key: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str,
        status: str = "idle",
        agent_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (chat_id, thread_key, agent_id, repo_alias, repo_path, codex_session_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_key) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    repo_alias=excluded.repo_alias,
                    repo_path=excluded.repo_path,
                    codex_session_id=excluded.codex_session_id,
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, thread_key, agent_id, repo_alias, str(repo_path), codex_session_id, status),
            )

    def get_session(self, chat_id: str, thread_key: str) -> SessionBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sessions.*, remote_agents.title AS agent_title,
                       remote_agents.last_run_id AS agent_last_run_id,
                       remote_agents.last_error AS agent_last_error
                FROM sessions
                LEFT JOIN remote_agents ON remote_agents.id = sessions.agent_id
                WHERE sessions.chat_id = ? AND sessions.thread_key = ?
                """,
                (chat_id, thread_key),
            ).fetchone()
        return self._row_to_binding(row)

    def list_sessions(self) -> list[SessionBinding]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sessions.*, remote_agents.title AS agent_title,
                       remote_agents.last_run_id AS agent_last_run_id,
                       remote_agents.last_error AS agent_last_error
                FROM sessions
                LEFT JOIN remote_agents ON remote_agents.id = sessions.agent_id
                ORDER BY sessions.updated_at DESC LIMIT 20
                """
            ).fetchall()
        return [self._row_to_binding(row) for row in rows if row is not None]

    def _row_to_binding(self, row: sqlite3.Row | None) -> SessionBinding | None:
        if row is None:
            return None
        return SessionBinding(
            chat_id=row["chat_id"],
            thread_key=row["thread_key"],
            repo_alias=row["repo_alias"],
            repo_path=Path(row["repo_path"]),
            codex_session_id=row["codex_session_id"],
            status=row["status"],
            agent_id=row["agent_id"],
            title=row["agent_title"],
            last_run_id=row["agent_last_run_id"],
            last_error=row["agent_last_error"],
        )

    def create_remote_agent(
        self,
        title: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str,
        chat_id: str,
        thread_key: str,
        status: str = "idle",
    ) -> RemoteAgent:
        agent_id = "rc_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_agents (id, title, repo_alias, repo_path, codex_session_id, status, chat_id, thread_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, title, repo_alias, str(repo_path), codex_session_id, status, chat_id, thread_key),
            )
        agent = self.get_remote_agent(agent_id)
        if agent is None:
            raise RuntimeError("failed to create remote agent")
        return agent

    def get_remote_agent(self, agent_id: str) -> RemoteAgent | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM remote_agents WHERE id = ?", (agent_id,)).fetchone()
        return self._row_to_remote_agent(row)

    def list_remote_agents(self, limit: int = 20) -> list[RemoteAgent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_agents ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_remote_agent(row) for row in rows if row is not None]

    def rename_remote_agent(self, agent_id: str, title: str) -> RemoteAgent | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE remote_agents SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, agent_id),
            )
        return self.get_remote_agent(agent_id)

    def delete_remote_agents(self, agent_ids: list[str]) -> list[RemoteAgent]:
        unique_ids = list(dict.fromkeys(agent_id.strip() for agent_id in agent_ids if agent_id.strip()))
        if not unique_ids:
            return []

        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM remote_agents WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            agents = [self._row_to_remote_agent(row) for row in rows if row is not None]
            conn.execute(
                f"DELETE FROM sessions WHERE agent_id IN ({placeholders})",
                unique_ids,
            )
            conn.execute(
                f"DELETE FROM remote_agents WHERE id IN ({placeholders})",
                unique_ids,
            )

        deleted = {agent.id: agent for agent in agents if agent is not None}
        return [deleted[agent_id] for agent_id in unique_ids if agent_id in deleted]

    def touch_remote_agent(
        self,
        agent_id: str,
        status: str | None = None,
        last_run_id: str | None = None,
        last_error: str | None = None,
        clear_last_error: bool = False,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE remote_agents
                SET status = COALESCE(?, status),
                    last_run_id = COALESCE(?, last_run_id),
                    last_error = CASE
                        WHEN ? THEN NULL
                        WHEN ? IS NOT NULL THEN ?
                        ELSE last_error
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, last_run_id, clear_last_error, last_error, last_error, agent_id),
            )


    def update_remote_agent_repo(self, agent_id: str, repo_alias: str, repo_path: Path) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE remote_agents
                SET repo_alias = ?, repo_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (repo_alias, str(repo_path), agent_id),
            )

    def update_remote_agent_session(self, agent_id: str, codex_session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE remote_agents SET codex_session_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (codex_session_id, agent_id),
            )

    def _row_to_remote_agent(self, row: sqlite3.Row | None) -> RemoteAgent | None:
        if row is None:
            return None
        return RemoteAgent(
            id=row["id"],
            title=row["title"],
            repo_alias=row["repo_alias"],
            repo_path=Path(row["repo_path"]),
            codex_session_id=row["codex_session_id"],
            status=row["status"],
            chat_id=row["chat_id"],
            thread_key=row["thread_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_run_id=row["last_run_id"],
            last_error=row["last_error"],
        )

    def close_session(self, chat_id: str, thread_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE chat_id = ? AND thread_key = ?", (chat_id, thread_key))

    def create_run(
        self,
        *,
        agent_id: str | None,
        chat_id: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str | None,
        prompt: str,
        status: str = "queued",
    ) -> RunRecord:
        run_id = "run_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, agent_id, chat_id, message_id, repo_alias, repo_path, codex_session_id, prompt, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    agent_id,
                    chat_id,
                    message_id,
                    repo_alias,
                    str(repo_path),
                    codex_session_id,
                    _redact_secrets(prompt),
                    status,
                ),
            )
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError("failed to create run")
        return run

    def attach_run_to_agent(self, run_id: str, agent_id: str, codex_session_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET agent_id = ?, codex_session_id = COALESCE(?, codex_session_id)
                WHERE id = ?
                """,
                (agent_id, codex_session_id, run_id),
            )

    def mark_run_running(self, run_id: str, pid: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = 'running', pid = ?, started_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'queued'
                """,
                (pid, run_id),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: str | None = None,
        error: str | None = None,
        codex_session_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    summary = ?,
                    error = ?,
                    codex_session_id = COALESCE(?, codex_session_id),
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, summary, error, codex_session_id, run_id),
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row)

    def get_running_run_for_agent(self, agent_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE agent_id = ? AND status IN ('queued', 'running')
                ORDER BY started_at DESC, created_at DESC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return self._row_to_run(row)

    def list_runs(self, agent_id: str | None = None, limit: int = 10) -> list[RunRecord]:
        with self._connect() as conn:
            if agent_id:
                rows = conn.execute(
                    """
                    SELECT * FROM runs WHERE agent_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (agent_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_run(row) for row in rows if row is not None]

    def _row_to_run(self, row: sqlite3.Row | None) -> RunRecord | None:
        if row is None:
            return None
        return RunRecord(
            id=row["id"],
            agent_id=row["agent_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            repo_alias=row["repo_alias"],
            repo_path=Path(row["repo_path"]),
            codex_session_id=row["codex_session_id"],
            prompt=row["prompt"],
            status=row["status"],
            pid=row["pid"],
            summary=row["summary"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def create_confirmation(
        self,
        action: str,
        requester_id: str,
        chat_id: str,
        message_id: str,
        payload: dict,
    ) -> Confirmation:
        confirmation_id = uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO confirmations (id, action, requester_id, chat_id, message_id, payload, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (confirmation_id, action, requester_id, chat_id, message_id, json.dumps(payload, ensure_ascii=False)),
            )
        return Confirmation(confirmation_id, action, requester_id, chat_id, message_id, payload, "pending")

    def get_confirmation(self, confirmation_id: str) -> Confirmation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE id = ?",
                (confirmation_id,),
            ).fetchone()
        return self._row_to_confirmation(row)

    def list_confirmations(self, requester_id: str | None = None, chat_id: str | None = None) -> list[Confirmation]:
        query = "SELECT * FROM confirmations WHERE status = 'pending'"
        params: list[str] = []
        if requester_id:
            query += " AND requester_id = ?"
            params.append(requester_id)
        if chat_id:
            query += " AND chat_id = ?"
            params.append(chat_id)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_confirmation(row) for row in rows if row is not None]

    def mark_confirmation(self, confirmation_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE confirmations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, confirmation_id),
            )

    def check_writable(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS far_write_check (id INTEGER)")
                conn.execute("INSERT INTO far_write_check (id) VALUES (1)")
                conn.execute("DELETE FROM far_write_check")
            return os.access(self.path.parent, os.W_OK)
        except sqlite3.Error:
            return False

    def _row_to_confirmation(self, row: sqlite3.Row | None) -> Confirmation | None:
        if row is None:
            return None
        return Confirmation(
            id=row["id"],
            action=row["action"],
            requester_id=row["requester_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            payload=json.loads(row["payload"]),
            status=row["status"],
        )


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*([^\s]+)"),
    re.compile(r"(?i)(bearer)\s+([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"),
)


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
