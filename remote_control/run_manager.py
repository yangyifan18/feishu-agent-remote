import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

from .models import CodexRunResult, RunRecord
from .state import StateStore


class RunManager:
    def __init__(self, state: StateStore, codex_runner: Any):
        self.state = state
        self.codex_runner = codex_runner
        self._agent_locks: dict[str, asyncio.Lock] = {}

    def running_for_agent(self, agent_id: str) -> RunRecord | None:
        return self.state.get_running_run_for_agent(agent_id)

    async def start_new(
        self,
        *,
        chat_id: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        prompt: str,
    ) -> tuple[RunRecord, CodexRunResult]:
        run = self.state.create_run(
            agent_id=None,
            chat_id=chat_id,
            message_id=message_id,
            repo_alias=repo_alias,
            repo_path=repo_path,
            codex_session_id=None,
            prompt=prompt,
        )
        try:
            result = _normalize_result(await self.codex_runner.start(
                repo_path,
                prompt,
                on_started=lambda pid: self.state.mark_run_running(run.id, pid),
            ))
        except Exception as exc:
            result = CodexRunResult(session_id=None, summary=f"Codex 执行异常：{exc}", status="failed")
        self._finish(run.id, result)
        return self.state.get_run(run.id) or run, result

    async def resume_agent(
        self,
        *,
        agent_id: str,
        chat_id: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str,
        prompt: str,
    ) -> tuple[RunRecord, CodexRunResult]:
        async with self._lock_for_agent(agent_id):
            existing = self.running_for_agent(agent_id)
            if existing is not None:
                raise RunAlreadyActive(existing)
            run = self.state.create_run(
                agent_id=agent_id,
                chat_id=chat_id,
                message_id=message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                codex_session_id=codex_session_id,
                prompt=prompt,
            )
            self.state.touch_remote_agent(agent_id, status="running", last_run_id=run.id, last_error=None)
        try:
            result = _normalize_result(await self.codex_runner.resume(
                codex_session_id,
                repo_path,
                prompt,
                on_started=lambda pid: self.state.mark_run_running(run.id, pid),
            ))
        except Exception as exc:
            result = CodexRunResult(session_id=codex_session_id, summary=f"Codex 执行异常：{exc}", status="failed")
        cancelled = self._cancelled_result(run.id, codex_session_id)
        if cancelled is not None:
            return cancelled
        self._finish(run.id, result)
        self.state.touch_remote_agent(
            agent_id,
            status="idle" if result.status == "succeeded" else "failed",
            last_run_id=run.id,
            last_error=None if result.status == "succeeded" else result.summary,
            clear_last_error=result.status == "succeeded",
        )
        return self.state.get_run(run.id) or run, result

    async def resume_session(
        self,
        *,
        chat_id: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str,
        prompt: str,
        agent_id: str | None = None,
    ) -> tuple[RunRecord, CodexRunResult]:
        if agent_id:
            return await self.resume_agent(
                agent_id=agent_id,
                chat_id=chat_id,
                message_id=message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                codex_session_id=codex_session_id,
                prompt=prompt,
            )
        run = self.state.create_run(
            agent_id=None,
            chat_id=chat_id,
            message_id=message_id,
            repo_alias=repo_alias,
            repo_path=repo_path,
            codex_session_id=codex_session_id,
            prompt=prompt,
        )
        try:
            result = _normalize_result(await self.codex_runner.resume(
                codex_session_id,
                repo_path,
                prompt,
                on_started=lambda pid: self.state.mark_run_running(run.id, pid),
            ))
        except Exception as exc:
            result = CodexRunResult(session_id=codex_session_id, summary=f"Codex 执行异常：{exc}", status="failed")
        cancelled = self._cancelled_result(run.id, codex_session_id)
        if cancelled is not None:
            return cancelled
        self._finish(run.id, result)
        return self.state.get_run(run.id) or run, result

    def attach_run_to_agent(self, run_id: str, agent_id: str, codex_session_id: str | None = None) -> None:
        self.state.attach_run_to_agent(run_id, agent_id, codex_session_id)
        run = self.state.get_run(run_id)
        if run:
            self.state.touch_remote_agent(
                agent_id,
                status="idle" if run.status == "succeeded" else "failed",
                last_run_id=run_id,
                last_error=run.error,
                clear_last_error=run.error is None,
            )

    def cancel_agent(self, agent_id: str) -> RunRecord | None:
        run = self.running_for_agent(agent_id)
        if run is None:
            return None
        if run.pid:
            terminate_process_group(run.pid)
        self.state.finish_run(run.id, "cancelled", summary="用户已取消任务。")
        self.state.touch_remote_agent(agent_id, status="idle", last_run_id=run.id, last_error="用户已取消任务。")
        return self.state.get_run(run.id) or run

    def _finish(self, run_id: str, result: CodexRunResult) -> None:
        error = None if result.status == "succeeded" else result.summary
        self.state.finish_run(run_id, result.status, summary=result.summary, error=error, codex_session_id=result.session_id)

    def _cancelled_result(self, run_id: str, codex_session_id: str | None) -> tuple[RunRecord, CodexRunResult] | None:
        run = self.state.get_run(run_id)
        if run is None or run.status != "cancelled":
            return None
        return run, CodexRunResult(
            session_id=codex_session_id,
            summary=f"任务 {run_id} 已取消，忽略后续 Codex 结果。",
            status="cancelled",
        )

    def _lock_for_agent(self, agent_id: str) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_id] = lock
        return lock


class RunAlreadyActive(Exception):
    def __init__(self, run: RunRecord):
        super().__init__(f"run {run.id} is already active")
        self.run = run


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.2)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)


def _normalize_result(result: Any) -> CodexRunResult:
    if isinstance(result, CodexRunResult):
        return result
    return CodexRunResult(
        session_id=result.get("session_id"),
        summary=result.get("summary", ""),
        status=result.get("status", "succeeded"),
    )
