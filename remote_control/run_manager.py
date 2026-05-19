import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from .models import RuntimeRunResult, RunRecord
from .runtimes import RuntimeRegistry
from .state import BindingAlreadyReserved, StateStore


class RunManager:
    def __init__(self, state: StateStore, runtime_registry: Any):
        self.state = state
        self.runtime_registry = runtime_registry if isinstance(runtime_registry, RuntimeRegistry) else RuntimeRegistry({"codex": runtime_registry})
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._binding_locks: dict[str, asyncio.Lock] = {}

    @property
    def codex_runner(self) -> Any:
        return self.runtime_registry.get("codex")

    @codex_runner.setter
    def codex_runner(self, runner: Any) -> None:
        self.runtime_registry._runners["codex"] = runner

    def running_for_agent(self, agent_id: str) -> RunRecord | None:
        return self.state.get_running_run_for_agent(agent_id)

    async def start_new(
        self,
        *,
        chat_id: str,
        thread_key: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        prompt: str,
        runtime: str = "codex",
        on_reserved: Any | None = None,
    ) -> tuple[RunRecord, RuntimeRunResult]:
        async with self._lock_for_binding(chat_id, thread_key):
            existing = self.state.get_running_run_for_binding(chat_id, thread_key)
            if existing is not None:
                raise RunAlreadyActive(existing)
            try:
                run = self.state.create_reserved_run_for_binding(
                    chat_id=chat_id,
                    thread_key=thread_key,
                    message_id=message_id,
                    repo_alias=repo_alias,
                    repo_path=repo_path,
                    prompt=prompt,
                    runtime=runtime,
                )
            except BindingAlreadyReserved as exc:
                existing = self.state.get_run(exc.run_id) if exc.run_id else None
                if existing is None:
                    self.state.release_binding_reservation(chat_id, thread_key, exc.run_id)
                    run = self.state.create_reserved_run_for_binding(
                        chat_id=chat_id,
                        thread_key=thread_key,
                        message_id=message_id,
                        repo_alias=repo_alias,
                        repo_path=repo_path,
                        prompt=prompt,
                        runtime=runtime,
                    )
                else:
                    raise RunAlreadyActive(existing) from exc
        if on_reserved is not None:
            try:
                maybe_awaitable = on_reserved(run)
                if maybe_awaitable is not None:
                    await maybe_awaitable
            except Exception:
                self.release_binding(chat_id, thread_key, run.id)
                raise
        try:
            runner = self.runtime_registry.get(runtime)
            result = _normalize_result(await runner.start(
                repo_path,
                prompt,
                on_started=lambda pid: self.state.mark_run_running(run.id, pid),
            ))
        except Exception as exc:
            result = RuntimeRunResult(session_id=None, summary=f"{_runtime_label(runtime)} 执行异常：{exc}", status="failed")
        self._finish(run.id, result, runtime)
        return self.state.get_run(run.id) or run, result

    def release_binding(self, chat_id: str, thread_key: str, run_id: str | None = None) -> None:
        self.state.release_binding_reservation(chat_id, thread_key, run_id)

    async def resume_agent(
        self,
        *,
        agent_id: str,
        chat_id: str,
        message_id: str,
        repo_alias: str,
        repo_path: Path,
        runtime_session_id: str,
        prompt: str,
        runtime: str = "codex",
    ) -> tuple[RunRecord, RuntimeRunResult]:
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
                codex_session_id=runtime_session_id,
                prompt=prompt,
                runtime=runtime,
            )
            self.state.touch_remote_agent(agent_id, status="running", last_run_id=run.id, last_error=None)
        try:
            runner = self.runtime_registry.get(runtime)
            result = _normalize_result(await runner.resume(
                runtime_session_id,
                repo_path,
                prompt,
                on_started=lambda pid: self.state.mark_run_running(run.id, pid),
            ))
        except Exception as exc:
            result = RuntimeRunResult(session_id=runtime_session_id, summary=f"{_runtime_label(runtime)} 执行异常：{exc}", status="failed")
        cancelled = self._cancelled_result(run.id, runtime_session_id)
        if cancelled is not None:
            return cancelled
        self._finish(run.id, result, runtime)
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
        runtime_session_id: str,
        prompt: str,
        agent_id: str | None = None,
        runtime: str = "codex",
        thread_key: str | None = None,
    ) -> tuple[RunRecord, RuntimeRunResult]:
        if agent_id:
            return await self.resume_agent(
                agent_id=agent_id,
                chat_id=chat_id,
                message_id=message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                runtime_session_id=runtime_session_id,
                prompt=prompt,
                runtime=runtime,
            )
        if thread_key:
            async with self._lock_for_binding(chat_id, thread_key):
                existing = self.state.get_running_run_for_binding(chat_id, thread_key)
                if existing is not None:
                    raise RunAlreadyActive(existing)
                run = self.state.create_run(
                    agent_id=None,
                    chat_id=chat_id,
                    thread_key=thread_key,
                    message_id=message_id,
                    repo_alias=repo_alias,
                    repo_path=repo_path,
                    codex_session_id=runtime_session_id,
                    prompt=prompt,
                    runtime=runtime,
                )
        else:
            run = self.state.create_run(
                agent_id=None,
                chat_id=chat_id,
                message_id=message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                codex_session_id=runtime_session_id,
                prompt=prompt,
                runtime=runtime,
            )
        try:
            runner = self.runtime_registry.get(runtime)
            result = _normalize_result(await runner.resume(
                runtime_session_id,
                repo_path,
                prompt,
                on_started=lambda pid: self.state.mark_run_running(run.id, pid),
            ))
        except Exception as exc:
            result = RuntimeRunResult(session_id=runtime_session_id, summary=f"{_runtime_label(runtime)} 执行异常：{exc}", status="failed")
        cancelled = self._cancelled_result(run.id, runtime_session_id)
        if cancelled is not None:
            return cancelled
        self._finish(run.id, result, runtime)
        return self.state.get_run(run.id) or run, result

    def attach_run_to_agent(
        self,
        run_id: str,
        agent_id: str,
        runtime_session_id: str | None = None,
        runtime: str | None = None,
    ) -> None:
        self.state.attach_run_to_agent(run_id, agent_id, runtime_session_id, runtime)
        run = self.state.get_run(run_id)
        if run:
            self.state.touch_remote_agent(
                agent_id,
                status="idle" if run.status == "succeeded" else "failed",
                last_run_id=run_id,
                last_error=run.error,
                clear_last_error=run.error is None,
            )

    async def cancel_agent(self, agent_id: str) -> RunRecord | None:
        run = self.running_for_agent(agent_id)
        if run is None:
            return None
        if run.pid:
            await terminate_process_group(run.pid)
        self.state.finish_run(run.id, "cancelled", summary="用户已取消任务。")
        self.state.touch_remote_agent(agent_id, status="idle", last_run_id=run.id, last_error="用户已取消任务。")
        return self.state.get_run(run.id) or run

    def _finish(self, run_id: str, result: RuntimeRunResult, runtime: str) -> None:
        error = None if result.status == "succeeded" else result.summary
        self.state.finish_run(run_id, result.status, summary=result.summary, error=error, codex_session_id=result.session_id, runtime=runtime)

    def _cancelled_result(self, run_id: str, runtime_session_id: str | None) -> tuple[RunRecord, RuntimeRunResult] | None:
        run = self.state.get_run(run_id)
        if run is None or run.status != "cancelled":
            return None
        return run, RuntimeRunResult(
            session_id=runtime_session_id,
            summary=f"任务 {run_id} 已取消，忽略后续 runtime 结果。",
            status="cancelled",
        )

    def _lock_for_agent(self, agent_id: str) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_id] = lock
        return lock

    def _lock_for_binding(self, chat_id: str, thread_key: str) -> asyncio.Lock:
        key = f"{chat_id}:{thread_key}"
        lock = self._binding_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._binding_locks[key] = lock
        return lock


class RunAlreadyActive(Exception):
    def __init__(self, run: RunRecord):
        super().__init__(f"run {run.id} is already active")
        self.run = run


async def terminate_process_group(pid: int, grace_seconds: float = 0.2) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
        await asyncio.sleep(grace_seconds)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)


def _normalize_result(result: Any) -> RuntimeRunResult:
    if isinstance(result, RuntimeRunResult):
        return result
    return RuntimeRunResult(
        session_id=result.get("session_id"),
        summary=result.get("summary", ""),
        status=result.get("status", "succeeded"),
    )


def _runtime_label(runtime: str) -> str:
    return "Codex" if runtime == "codex" else runtime
