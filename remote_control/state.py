import json
import sqlite3
import uuid
from pathlib import Path

from .models import Confirmation, RemoteAgent, SessionBinding


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

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
                SELECT sessions.*, remote_agents.title AS agent_title
                FROM sessions
                LEFT JOIN remote_agents ON remote_agents.id = sessions.agent_id
                WHERE sessions.chat_id = ? AND sessions.thread_key = ?
                """,
                (chat_id, thread_key),
            ).fetchone()
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
        )

    def list_sessions(self) -> list[SessionBinding]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sessions.*, remote_agents.title AS agent_title
                FROM sessions
                LEFT JOIN remote_agents ON remote_agents.id = sessions.agent_id
                ORDER BY sessions.updated_at DESC LIMIT 20
                """
            ).fetchall()
        return [
            SessionBinding(
                chat_id=row["chat_id"],
                thread_key=row["thread_key"],
                repo_alias=row["repo_alias"],
                repo_path=Path(row["repo_path"]),
                codex_session_id=row["codex_session_id"],
                status=row["status"],
                agent_id=row["agent_id"],
                title=row["agent_title"],
            )
            for row in rows
        ]

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

    def touch_remote_agent(self, agent_id: str, status: str = "idle") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE remote_agents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, agent_id),
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
        )

    def close_session(self, chat_id: str, thread_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE chat_id = ? AND thread_key = ?", (chat_id, thread_key))

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

    def list_confirmations(self) -> list[Confirmation]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM confirmations WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_confirmation(row) for row in rows if row is not None]

    def mark_confirmation(self, confirmation_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE confirmations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, confirmation_id),
            )

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
