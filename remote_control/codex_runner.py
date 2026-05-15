import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import CodexRunResult


@dataclass(frozen=True)
class ParsedCodexOutput:
    session_id: str | None
    last_message: str


class CodexRunner:
    def __init__(
        self,
        codex_bin: str = "codex",
        sandbox: str = "workspace-write",
        timeout_seconds: int = 1800,
        profile: str | None = None,
    ):
        self.codex_bin = codex_bin
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.profile = profile

    async def start(self, repo_path: Path, prompt: str, on_started: Callable[[int], None] | None = None) -> CodexRunResult:
        return await self._run(
            [
                *self._base_argv(),
                "exec",
                "--json",
                "-C",
                str(repo_path),
                "-s",
                self.sandbox,
                prompt,
            ],
            on_started=on_started,
        )

    async def resume(self, session_id: str, repo_path: Path, prompt: str, on_started: Callable[[int], None] | None = None) -> CodexRunResult:
        return await self._run(
            [*self._base_argv(), "exec", "resume", "--json", session_id, prompt],
            cwd=repo_path,
            on_started=on_started,
        )

    def _base_argv(self) -> list[str]:
        argv = [self.codex_bin]
        if self.profile:
            argv.extend(["--profile", self.profile])
        return argv

    async def _run(
        self,
        argv: list[str],
        cwd: Path | None = None,
        on_started: Callable[[int], None] | None = None,
    ) -> CodexRunResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if on_started:
            on_started(proc.pid)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return CodexRunResult(session_id=None, summary="Codex 执行超时，已终止。", status="timed_out")

        parsed = parse_codex_jsonl(stdout.decode(errors="replace"))
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            summary = err or parsed.last_message or f"Codex 退出码 {proc.returncode}"
            return CodexRunResult(session_id=parsed.session_id, summary=f"Codex 执行失败：{summary}", status="failed")

        return CodexRunResult(session_id=parsed.session_id, summary=parsed.last_message or "Codex 已完成，但没有返回文本。", status="succeeded")


def parse_codex_jsonl(raw: str) -> ParsedCodexOutput:
    session_id: str | None = None
    messages: list[str] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "session_meta":
            payload = event.get("payload") or {}
            session_id = payload.get("id") or session_id
        elif event.get("type") == "thread.started":
            session_id = event.get("thread_id") or session_id

        payload = event.get("payload") or {}
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            text = _extract_text(payload.get("content") or [])
            if text:
                messages.append(text)
        elif event.get("type") == "agent_message":
            text = payload.get("message") or event.get("message")
            if text:
                messages.append(str(text))
        elif event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))

    return ParsedCodexOutput(session_id=session_id, last_message=messages[-1] if messages else "")


def _extract_text(content: list) -> str:
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if text:
                parts.append(str(text))
        elif isinstance(item, str):
            parts.append(item)
    return "".join(parts).strip()
