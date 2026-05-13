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
                            )
        except (OSError, json.JSONDecodeError):
            return None
        return None
