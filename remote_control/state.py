import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Confirmation, RemoteAgent, RunRecord, SessionBinding


_COLUMN_MIGRATIONS: dict[tuple[str, str], str] = {
    ("sessions", "agent_id"): "ALTER TABLE sessions ADD COLUMN agent_id TEXT",
    ("sessions", "runtime"): "ALTER TABLE sessions ADD COLUMN runtime TEXT DEFAULT 'codex'",
    ("sessions", "runtime_session_id"): "ALTER TABLE sessions ADD COLUMN runtime_session_id TEXT",
    ("sessions", "workspace_id"): "ALTER TABLE sessions ADD COLUMN workspace_id TEXT DEFAULT 'default'",
    ("remote_agents", "last_run_id"): "ALTER TABLE remote_agents ADD COLUMN last_run_id TEXT",
    ("remote_agents", "last_error"): "ALTER TABLE remote_agents ADD COLUMN last_error TEXT",
    ("remote_agents", "runtime"): "ALTER TABLE remote_agents ADD COLUMN runtime TEXT DEFAULT 'codex'",
    ("remote_agents", "runtime_session_id"): "ALTER TABLE remote_agents ADD COLUMN runtime_session_id TEXT",
    ("runs", "runtime"): "ALTER TABLE runs ADD COLUMN runtime TEXT DEFAULT 'codex'",
    ("runs", "runtime_session_id"): "ALTER TABLE runs ADD COLUMN runtime_session_id TEXT",
    ("runs", "thread_key"): "ALTER TABLE runs ADD COLUMN thread_key TEXT",
    ("remote_agents", "workspace_id"): "ALTER TABLE remote_agents ADD COLUMN workspace_id TEXT DEFAULT 'default'",
    ("runs", "workspace_id"): "ALTER TABLE runs ADD COLUMN workspace_id TEXT DEFAULT 'default'",
    ("confirmations", "workspace_id"): "ALTER TABLE confirmations ADD COLUMN workspace_id TEXT DEFAULT 'default'",
}


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
            self._ensure_column(conn, "sessions", "agent_id")
            self._ensure_column(conn, "sessions", "runtime")
            self._ensure_column(conn, "sessions", "runtime_session_id")
            self._ensure_column(conn, "sessions", "workspace_id")
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
            self._ensure_column(conn, "remote_agents", "last_run_id")
            self._ensure_column(conn, "remote_agents", "last_error")
            self._ensure_column(conn, "remote_agents", "runtime")
            self._ensure_column(conn, "remote_agents", "runtime_session_id")
            self._ensure_column(conn, "remote_agents", "workspace_id")
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
            self._ensure_column(conn, "runs", "runtime")
            self._ensure_column(conn, "runs", "runtime_session_id")
            self._ensure_column(conn, "runs", "thread_key")
            self._ensure_column(conn, "runs", "workspace_id")
            self._ensure_column(conn, "confirmations", "workspace_id")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS binding_reservations (
                    chat_id TEXT NOT NULL,
                    thread_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS binding_reservations_v2 (
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    chat_id TEXT NOT NULL,
                    thread_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (workspace_id, chat_id, thread_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions_v2 (
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    chat_id TEXT NOT NULL,
                    thread_key TEXT NOT NULL,
                    agent_id TEXT,
                    repo_alias TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    codex_session_id TEXT NOT NULL,
                    runtime TEXT DEFAULT 'codex',
                    runtime_session_id TEXT,
                    status TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (workspace_id, chat_id, thread_key)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions_v2 (
                    workspace_id, chat_id, thread_key, agent_id, repo_alias, repo_path,
                    codex_session_id, runtime, runtime_session_id, status, updated_at
                )
                SELECT workspace_id, chat_id, thread_key, agent_id, repo_alias, repo_path,
                       codex_session_id, runtime, runtime_session_id, status, updated_at
                FROM sessions
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_replies (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    mode TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    card_id TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str) -> None:
        migration = _COLUMN_MIGRATIONS.get((table, column))
        if migration is None:
            raise ValueError(f"unknown column migration: {table}.{column}")
        if not self._column_exists(conn, table, column):
            conn.execute(migration)

    def _column_exists(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table, column),
        ).fetchone()
        return row is not None

    def upsert_session(
        self,
        chat_id: str,
        thread_key: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str,
        status: str = "idle",
        agent_id: str | None = None,
        runtime: str = "codex",
        workspace_id: str = "default",
    ) -> None:
        runtime_session_id = codex_session_id
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions_v2 (
                    workspace_id, chat_id, thread_key, agent_id, repo_alias, repo_path, codex_session_id,
                    runtime, runtime_session_id, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, chat_id, thread_key) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    repo_alias=excluded.repo_alias,
                    repo_path=excluded.repo_path,
                    codex_session_id=excluded.codex_session_id,
                    runtime=excluded.runtime,
                    runtime_session_id=excluded.runtime_session_id,
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (workspace_id, chat_id, thread_key, agent_id, repo_alias, str(repo_path), codex_session_id, runtime, runtime_session_id, status),
            )

    def get_session(self, chat_id: str, thread_key: str, workspace_id: str = "default") -> SessionBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sessions_v2.*, remote_agents.title AS agent_title,
                       remote_agents.last_run_id AS agent_last_run_id,
                       remote_agents.last_error AS agent_last_error
                FROM sessions_v2
                LEFT JOIN remote_agents
                    ON remote_agents.id = sessions_v2.agent_id
                   AND remote_agents.workspace_id = sessions_v2.workspace_id
                WHERE sessions_v2.workspace_id = ? AND sessions_v2.chat_id = ? AND sessions_v2.thread_key = ?
                """,
                (workspace_id, chat_id, thread_key),
            ).fetchone()
        return self._row_to_binding(row)

    def list_sessions(self, workspace_id: str = "default") -> list[SessionBinding]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sessions_v2.*, remote_agents.title AS agent_title,
                       remote_agents.last_run_id AS agent_last_run_id,
                       remote_agents.last_error AS agent_last_error
                FROM sessions_v2
                LEFT JOIN remote_agents
                    ON remote_agents.id = sessions_v2.agent_id
                   AND remote_agents.workspace_id = sessions_v2.workspace_id
                WHERE sessions_v2.workspace_id = ?
                ORDER BY sessions_v2.updated_at DESC LIMIT 20
                """,
                (workspace_id,),
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
            runtime=_row_value(row, "runtime", "codex") or "codex",
            runtime_session_id=_row_value(row, "runtime_session_id") or row["codex_session_id"],
            workspace_id=_row_value(row, "workspace_id", "default") or "default",
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
        runtime: str = "codex",
        workspace_id: str = "default",
    ) -> RemoteAgent:
        runtime_session_id = codex_session_id
        agent_id = "rc_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_agents (
                    id, title, repo_alias, repo_path, codex_session_id, runtime,
                    runtime_session_id, status, chat_id, thread_key, workspace_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, title, repo_alias, str(repo_path), codex_session_id, runtime, runtime_session_id, status, chat_id, thread_key, workspace_id),
            )
        agent = self.get_remote_agent(agent_id)
        if agent is None:
            raise RuntimeError("failed to create remote agent")
        return agent

    def get_remote_agent(self, agent_id: str, workspace_id: str | None = None) -> RemoteAgent | None:
        with self._connect() as conn:
            if workspace_id is None:
                row = conn.execute("SELECT * FROM remote_agents WHERE id = ?", (agent_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM remote_agents WHERE id = ? AND workspace_id = ?",
                    (agent_id, workspace_id),
                ).fetchone()
        return self._row_to_remote_agent(row)

    def list_remote_agents(self, limit: int = 20, workspace_id: str = "default") -> list[RemoteAgent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_agents WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        return [self._row_to_remote_agent(row) for row in rows if row is not None]

    def rename_remote_agent(self, agent_id: str, title: str, workspace_id: str = "default") -> RemoteAgent | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE remote_agents SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND workspace_id = ?",
                (title, agent_id, workspace_id),
            )
        return self.get_remote_agent(agent_id, workspace_id)

    def delete_remote_agents(self, agent_ids: list[str], workspace_id: str = "default") -> list[RemoteAgent]:
        unique_ids = list(dict.fromkeys(agent_id.strip() for agent_id in agent_ids if agent_id.strip()))
        if not unique_ids:
            return []

        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM remote_agents WHERE workspace_id = ? AND id IN ({placeholders})",
                [workspace_id, *unique_ids],
            ).fetchall()
            agents = [self._row_to_remote_agent(row) for row in rows if row is not None]
            conn.execute(
                f"DELETE FROM sessions_v2 WHERE workspace_id = ? AND agent_id IN ({placeholders})",
                [workspace_id, *unique_ids],
            )
            conn.execute(
                f"DELETE FROM remote_agents WHERE workspace_id = ? AND id IN ({placeholders})",
                [workspace_id, *unique_ids],
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
        workspace_id: str = "default",
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
                WHERE id = ? AND workspace_id = ?
                """,
                (status, last_run_id, clear_last_error, last_error, last_error, agent_id, workspace_id),
            )


    def update_remote_agent_repo(self, agent_id: str, repo_alias: str, repo_path: Path, workspace_id: str = "default") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE remote_agents
                SET repo_alias = ?, repo_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND workspace_id = ?
                """,
                (repo_alias, str(repo_path), agent_id, workspace_id),
            )

    def update_remote_agent_session(self, agent_id: str, codex_session_id: str, runtime: str | None = None, workspace_id: str = "default") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE remote_agents
                SET codex_session_id = ?,
                    runtime = COALESCE(?, runtime),
                    runtime_session_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND workspace_id = ?
                """,
                (codex_session_id, runtime, codex_session_id, agent_id, workspace_id),
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
            runtime=_row_value(row, "runtime", "codex") or "codex",
            runtime_session_id=_row_value(row, "runtime_session_id") or row["codex_session_id"],
            workspace_id=_row_value(row, "workspace_id", "default") or "default",
        )

    def close_session(self, chat_id: str, thread_key: str, workspace_id: str = "default") -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sessions_v2 WHERE workspace_id = ? AND chat_id = ? AND thread_key = ?",
                (workspace_id, chat_id, thread_key),
            )

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
        thread_key: str | None = None,
        status: str = "queued",
        runtime: str = "codex",
        workspace_id: str = "default",
    ) -> RunRecord:
        run_id = "run_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            self._insert_run(
                conn,
                run_id=run_id,
                agent_id=agent_id,
                chat_id=chat_id,
                thread_key=thread_key,
                message_id=message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                codex_session_id=codex_session_id,
                prompt=prompt,
                status=status,
                runtime=runtime,
                workspace_id=workspace_id,
            )
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError("failed to create run")
        return run

    def _insert_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        agent_id: str | None,
        chat_id: str,
        thread_key: str | None,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str | None,
        prompt: str,
        status: str,
        runtime: str,
        workspace_id: str,
    ) -> None:
        runtime_session_id = codex_session_id
        conn.execute(
            """
            INSERT INTO runs (
                id, agent_id, chat_id, thread_key, message_id, repo_alias, repo_path,
                codex_session_id, runtime, runtime_session_id, prompt, status
                , workspace_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                agent_id,
                chat_id,
                thread_key,
                message_id,
                repo_alias,
                str(repo_path),
                codex_session_id,
                runtime,
                runtime_session_id,
                _redact_secrets(prompt),
                status,
                workspace_id,
            ),
        )

    def create_reserved_run_for_binding(
        self,
        *,
        agent_id: str | None = None,
        chat_id: str,
        thread_key: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str | None = None,
        prompt: str,
        runtime: str = "codex",
        workspace_id: str = "default",
    ) -> RunRecord:
        run_id = "run_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO binding_reservations_v2 (workspace_id, chat_id, thread_key, run_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (workspace_id, chat_id, thread_key, run_id),
                )
            except sqlite3.IntegrityError as exc:
                existing_row = conn.execute(
                    """
                    SELECT run_id FROM binding_reservations_v2
                    WHERE workspace_id = ? AND chat_id = ? AND thread_key = ?
                    """,
                    (workspace_id, chat_id, thread_key),
                ).fetchone()
                raise BindingAlreadyReserved(existing_row["run_id"] if existing_row else None) from exc
            self._insert_run(
                conn,
                run_id=run_id,
                agent_id=agent_id,
                chat_id=chat_id,
                thread_key=thread_key,
                message_id=message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                codex_session_id=codex_session_id,
                prompt=prompt,
                status="queued",
                runtime=runtime,
                workspace_id=workspace_id,
            )
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError("failed to create reserved run")
        return run

    def release_binding_reservation(self, chat_id: str, thread_key: str, run_id: str | None = None, workspace_id: str = "default") -> None:
        with self._connect() as conn:
            if run_id:
                conn.execute(
                    "DELETE FROM binding_reservations_v2 WHERE workspace_id = ? AND chat_id = ? AND thread_key = ? AND run_id = ?",
                    (workspace_id, chat_id, thread_key, run_id),
                )
            else:
                conn.execute(
                    "DELETE FROM binding_reservations_v2 WHERE workspace_id = ? AND chat_id = ? AND thread_key = ?",
                    (workspace_id, chat_id, thread_key),
                )

    def attach_run_to_agent(
        self,
        run_id: str,
        agent_id: str,
        codex_session_id: str | None = None,
        runtime: str | None = None,
        workspace_id: str = "default",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET agent_id = ?,
                    codex_session_id = COALESCE(?, codex_session_id),
                    runtime = COALESCE(?, runtime),
                    runtime_session_id = COALESCE(?, runtime_session_id)
                WHERE id = ? AND workspace_id = ?
                """,
                (agent_id, codex_session_id, runtime, codex_session_id, run_id, workspace_id),
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
        runtime: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    summary = ?,
                    error = ?,
                    codex_session_id = COALESCE(?, codex_session_id),
                    runtime = COALESCE(?, runtime),
                    runtime_session_id = COALESCE(?, runtime_session_id),
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, summary, error, codex_session_id, runtime, codex_session_id, run_id),
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row)

    def get_running_run_for_binding(self, chat_id: str, thread_key: str) -> RunRecord | None:
        return self.get_running_run_for_binding_in_workspace(chat_id, thread_key, "default")

    def get_running_run_for_binding_in_workspace(self, chat_id: str, thread_key: str, workspace_id: str = "default") -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE workspace_id = ? AND chat_id = ? AND thread_key = ? AND status IN ('queued', 'running')
                ORDER BY started_at DESC, created_at DESC LIMIT 1
                """,
                (workspace_id, chat_id, thread_key),
            ).fetchone()
        return self._row_to_run(row)

    def get_running_run_for_agent(self, agent_id: str, workspace_id: str = "default") -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE workspace_id = ? AND agent_id = ? AND status IN ('queued', 'running')
                ORDER BY started_at DESC, created_at DESC LIMIT 1
                """,
                (workspace_id, agent_id),
            ).fetchone()
        return self._row_to_run(row)

    def list_runs(self, agent_id: str | None = None, limit: int = 10, workspace_id: str = "default") -> list[RunRecord]:
        with self._connect() as conn:
            if agent_id:
                rows = conn.execute(
                    """
                    SELECT * FROM runs WHERE workspace_id = ? AND agent_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (workspace_id, agent_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
                    (workspace_id, limit),
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
            runtime=_row_value(row, "runtime", "codex") or "codex",
            runtime_session_id=_row_value(row, "runtime_session_id") or row["codex_session_id"],
            thread_key=_row_value(row, "thread_key"),
            workspace_id=_row_value(row, "workspace_id", "default") or "default",
        )

    def create_confirmation(
        self,
        action: str,
        requester_id: str,
        chat_id: str,
        message_id: str,
        payload: dict,
        workspace_id: str = "default",
    ) -> Confirmation:
        confirmation_id = uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO confirmations (id, action, requester_id, chat_id, message_id, payload, status, workspace_id)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (confirmation_id, action, requester_id, chat_id, message_id, json.dumps(payload, ensure_ascii=False), workspace_id),
            )
        return Confirmation(confirmation_id, action, requester_id, chat_id, message_id, payload, "pending", workspace_id)

    def get_confirmation(self, confirmation_id: str, workspace_id: str | None = None) -> Confirmation | None:
        with self._connect() as conn:
            if workspace_id is None:
                row = conn.execute(
                    "SELECT * FROM confirmations WHERE id = ?",
                    (confirmation_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM confirmations WHERE id = ? AND workspace_id = ?",
                    (confirmation_id, workspace_id),
                ).fetchone()
        return self._row_to_confirmation(row)

    def list_confirmations(self, requester_id: str | None = None, chat_id: str | None = None, workspace_id: str = "default") -> list[Confirmation]:
        query = "SELECT * FROM confirmations WHERE status = 'pending' AND workspace_id = ?"
        params: list[str] = [workspace_id]
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

    def mark_confirmation(self, confirmation_id: str, status: str, workspace_id: str | None = None) -> None:
        with self._connect() as conn:
            if workspace_id is None:
                conn.execute(
                    "UPDATE confirmations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, confirmation_id),
                )
            else:
                conn.execute(
                    "UPDATE confirmations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND workspace_id = ?",
                    (status, confirmation_id, workspace_id),
                )

    def save_run_reply(self, run_id: str, handle: object) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_replies (run_id, workspace_id, mode, message_id, card_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    mode=excluded.mode,
                    message_id=excluded.message_id,
                    card_id=excluded.card_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    run_id,
                    getattr(handle, "workspace_id"),
                    getattr(handle, "mode"),
                    getattr(handle, "message_id"),
                    getattr(handle, "card_id", None),
                ),
            )

    def get_run_reply(self, run_id: str):
        from .replies import ReplyHandle

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM run_replies WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return ReplyHandle(
            mode=row["mode"],
            workspace_id=row["workspace_id"],
            message_id=row["message_id"],
            card_id=row["card_id"],
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
            workspace_id=_row_value(row, "workspace_id", "default") or "default",
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


def _row_value(row: sqlite3.Row, key: str, default: str | None = None) -> str | None:
    return row[key] if key in row.keys() else default


class BindingAlreadyReserved(Exception):
    def __init__(self, run_id: str | None):
        super().__init__(f"binding is already reserved by {run_id or 'unknown run'}")
        self.run_id = run_id
