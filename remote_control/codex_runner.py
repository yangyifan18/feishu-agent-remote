import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import CodexRunResult
from .progress import RuntimeEvent, RuntimeEventCallback


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

    async def start(
        self,
        repo_path: Path,
        prompt: str,
        on_started: Callable[[int], None] | None = None,
        on_event: RuntimeEventCallback | None = None,
    ) -> CodexRunResult:
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
            on_event=on_event,
        )

    async def resume(
        self,
        session_id: str,
        repo_path: Path,
        prompt: str,
        on_started: Callable[[int], None] | None = None,
        on_event: RuntimeEventCallback | None = None,
    ) -> CodexRunResult:
        return await self._run(
            [*self._base_argv(), "exec", "resume", "--json", session_id, prompt],
            cwd=repo_path,
            on_started=on_started,
            on_event=on_event,
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
        on_event: RuntimeEventCallback | None = None,
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
            if on_event is None:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            else:
                stdout, stderr = await asyncio.wait_for(_stream_process_output(proc, parse_codex_event_line, on_event), timeout=self.timeout_seconds)
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


def parse_codex_event_line(line: str) -> RuntimeEvent | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if event.get("type") == "session_meta":
        payload = event.get("payload") or {}
        session_id = payload.get("id")
        if session_id:
            return RuntimeEvent(type="session", text="", session_id=str(session_id))
    if event.get("type") == "thread.started" and event.get("thread_id"):
        return RuntimeEvent(type="session", text="", session_id=str(event["thread_id"]))
    payload = event.get("payload") or {}
    if payload.get("type") == "message" and payload.get("role") == "assistant":
        text = _extract_text(payload.get("content") or [])
        return RuntimeEvent(type="assistant", text=text) if text else None
    if event.get("type") == "agent_message":
        text = payload.get("message") or event.get("message")
        return RuntimeEvent(type="assistant", text=str(text)) if text else None
    if event.get("type") == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            return RuntimeEvent(type="assistant", text=str(item["text"]))
    return None


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


async def _stream_process_output(proc: asyncio.subprocess.Process, parser: Callable[[str], RuntimeEvent | None], on_event: RuntimeEventCallback) -> tuple[bytes, bytes]:
    stdout_parts: list[bytes] = []
    stderr_task = asyncio.create_task(proc.stderr.read() if proc.stderr else _empty_bytes())
    if proc.stdout is not None:
        async for raw_line in proc.stdout:
            stdout_parts.append(raw_line)
            event = parser(raw_line.decode(errors="replace"))
            if event is not None:
                try:
                    result = on_event(event)
                    if result is not None:
                        await result
                except Exception:
                    pass
    await proc.wait()
    stderr = await stderr_task
    return b"".join(stdout_parts), stderr


async def _empty_bytes() -> bytes:
    return b""
