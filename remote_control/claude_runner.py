import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import RuntimeRunResult
from .progress import RuntimeEvent, RuntimeEventCallback


@dataclass(frozen=True)
class ParsedClaudeOutput:
    session_id: str | None
    last_message: str


class ClaudeRunner:
    def __init__(
        self,
        claude_bin: str = "claude",
        timeout_seconds: int = 1800,
        permission_mode: str | None = "acceptEdits",
        model: str | None = None,
        extra_args: tuple[str, ...] = (),
    ):
        self.claude_bin = claude_bin
        self.bin = claude_bin
        self.timeout_seconds = timeout_seconds
        self.permission_mode = permission_mode
        self.model = model
        self.extra_args = tuple(extra_args)

    async def start(
        self,
        repo_path: Path,
        prompt: str,
        on_started: Callable[[int], None] | None = None,
        on_event: RuntimeEventCallback | None = None,
    ) -> RuntimeRunResult:
        return await self._run(self._argv(prompt), cwd=repo_path, on_started=on_started, on_event=on_event)

    async def resume(
        self,
        session_id: str,
        repo_path: Path,
        prompt: str,
        on_started: Callable[[int], None] | None = None,
        on_event: RuntimeEventCallback | None = None,
    ) -> RuntimeRunResult:
        return await self._run([*self._argv(prompt), "--resume", session_id], cwd=repo_path, on_started=on_started, on_event=on_event)

    def _argv(self, prompt: str) -> list[str]:
        argv = [self.claude_bin, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if self.permission_mode:
            argv.extend(["--permission-mode", self.permission_mode])
        if self.model:
            argv.extend(["--model", self.model])
        argv.extend(self.extra_args)
        return argv

    async def _run(
        self,
        argv: list[str],
        cwd: Path,
        on_started: Callable[[int], None] | None = None,
        on_event: RuntimeEventCallback | None = None,
    ) -> RuntimeRunResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
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
                stdout, stderr = await asyncio.wait_for(_stream_process_output(proc, parse_claude_event_line, on_event), timeout=self.timeout_seconds)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return RuntimeRunResult(session_id=None, summary="Claude 执行超时，已终止。", status="timed_out")

        parsed = parse_claude_stream_json(stdout.decode(errors="replace"))
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            summary = err or parsed.last_message or f"Claude 退出码 {proc.returncode}"
            return RuntimeRunResult(session_id=parsed.session_id, summary=f"Claude 执行失败：{summary}", status="failed")
        return RuntimeRunResult(session_id=parsed.session_id, summary=parsed.last_message or "Claude 已完成，但没有返回文本。", status="succeeded")


def parse_claude_stream_json(raw: str) -> ParsedClaudeOutput:
    session_id: str | None = None
    messages: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = event.get("session_id") or event.get("sessionId") or session_id
        if event.get("type") == "assistant":
            text = _message_text(event.get("message") or event)
            if text:
                messages.append(text)
        elif event.get("type") == "result":
            text = event.get("result")
            if text:
                messages.append(str(text))
    return ParsedClaudeOutput(session_id=session_id, last_message=messages[-1] if messages else "")


def parse_claude_event_line(line: str) -> RuntimeEvent | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    session_id = event.get("session_id") or event.get("sessionId")
    if event.get("type") == "assistant":
        text = _message_text(event.get("message") or event)
        return RuntimeEvent(type="assistant", text=text, session_id=session_id) if text else None
    if event.get("type") == "result":
        text = event.get("result")
        return RuntimeEvent(type="assistant", text=str(text), session_id=session_id) if text else None
    if session_id:
        return RuntimeEvent(type="session", text="", session_id=str(session_id))
    return None


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content") or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
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
