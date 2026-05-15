import json
from pathlib import Path

from .models import CodexSessionMeta


class CodexSessionFinder:
    def __init__(self, sessions_root: Path | None = None):
        self.sessions_root = sessions_root or Path("~/.codex/sessions").expanduser()

    def recent(self, limit: int = 10) -> list[CodexSessionMeta]:
        files = sorted(
            self.sessions_root.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        sessions: list[CodexSessionMeta] = []
        for path in files:
            meta = self._read_meta(path)
            if meta is not None:
                sessions.append(meta)
            if len(sessions) >= limit:
                break
        return sessions

    def _read_meta(self, path: Path) -> CodexSessionMeta | None:
        try:
            with path.open() as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("type") == "session_meta":
                        payload = event.get("payload") or {}
                        session_id = payload.get("id")
                        cwd = payload.get("cwd")
                        if session_id and cwd:
                            return CodexSessionMeta(
                                session_id=str(session_id),
                                cwd=Path(str(cwd)),
                                timestamp=str(payload.get("timestamp", "")),
                                source=str(payload.get("source", "")),
                                path=path,
                                runtime="codex",
                            )
                    if event.get("type") == "thread.started":
                        session_id = event.get("thread_id")
                        if session_id:
                            return CodexSessionMeta(
                                session_id=str(session_id),
                                cwd=Path(""),
                                timestamp="",
                                source="exec",
                                path=path,
                                runtime="codex",
                            )
        except (OSError, json.JSONDecodeError):
            return None
        return None


class RuntimeSessionFinder:
    def __init__(
        self,
        codex_sessions_root: Path | None = None,
        claude_transcripts_root: Path | None = None,
        claude_projects_root: Path | None = None,
    ):
        self.codex = CodexSessionFinder(codex_sessions_root)
        self.claude_transcripts_root = claude_transcripts_root or Path("~/.claude/transcripts").expanduser()
        self.claude_projects_root = claude_projects_root or Path("~/.claude/projects").expanduser()

    def recent(self, limit: int = 10, runtime: str = "codex") -> list[CodexSessionMeta]:
        if runtime == "codex":
            return self.codex.recent(limit)
        if runtime == "claude":
            return self._recent_claude(limit)
        return []

    def _recent_claude(self, limit: int) -> list[CodexSessionMeta]:
        try:
            files = [
                *self.claude_projects_root.glob("*/*.jsonl"),
                *self.claude_transcripts_root.glob("*.jsonl"),
            ]
            files = sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return []
        sessions: list[CodexSessionMeta] = []
        for path in files:
            stat = path.stat()
            session_id, cwd, timestamp = self._read_claude_meta(path)
            sessions.append(
                CodexSessionMeta(
                    session_id=session_id or path.stem,
                    cwd=cwd,
                    timestamp=timestamp or _mtime_iso(stat.st_mtime),
                    source="transcript",
                    path=path,
                    runtime="claude",
                )
            )
            if len(sessions) >= limit:
                break
        return sessions

    def _read_claude_meta(self, path: Path) -> tuple[str | None, Path, str]:
        session_id: str | None = None
        cwd: Path | None = None
        timestamp = ""
        try:
            with path.open() as fh:
                for _, line in zip(range(50), fh):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    session_id = event.get("sessionId") or event.get("session_id") or session_id
                    if event.get("cwd") and cwd is None:
                        cwd = Path(str(event["cwd"]))
                    timestamp = str(event.get("timestamp") or timestamp)
                    if session_id and cwd is not None:
                        break
        except (OSError, json.JSONDecodeError):
            return None, Path(""), ""
        return session_id, cwd or Path(""), timestamp


def _mtime_iso(timestamp: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
