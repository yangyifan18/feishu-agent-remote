import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from .models import RuntimeRunResult, RunRecord
from .progress import ProgressReporter, RuntimeEvent, final_progress, maybe_emit, progress_from_run, schedule_emit
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

    def running_for_agent(self, agent_id: str, workspace_id: str = "default") -> RunRecord | None:
        return self.state.get_running_run_for_agent(agent_id, workspace_id)

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
        workspace_id: str = "default",
        progress_reporter: ProgressReporter | None = None,
        runtime_streaming: bool = False,
        title: str | None = None,
    ) -> tuple[RunRecord, RuntimeRunResult]:
        async with self._lock_for_binding(chat_id, thread_key, workspace_id):
            existing = self.state.get_running_run_for_binding_in_workspace(chat_id, thread_key, workspace_id)
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
                    workspace_id=workspace_id,
                )
            except BindingAlreadyReserved as exc:
                existing = self.state.get_run(exc.run_id) if exc.run_id else None
                if existing is None:
                    self.state.release_binding_reservation(chat_id, thread_key, exc.run_id, workspace_id)
                    run = self.state.create_reserved_run_for_binding(
                        chat_id=chat_id,
                        thread_key=thread_key,
                        message_id=message_id,
                        repo_alias=repo_alias,
                        repo_path=repo_path,
                        prompt=prompt,
                        runtime=runtime,
                        workspace_id=workspace_id,
                    )
                else:
                    raise RunAlreadyActive(existing) from exc
        if on_reserved is not None:
            try:
                maybe_awaitable = on_reserved(run)
                if maybe_awaitable is not None:
                    await maybe_awaitable
            except Exception:
                self.state.finish_run(run.id, "failed", summary="创建线上专员前置回复失败。", error="on_reserved callback failed")
                self.release_binding(chat_id, thread_key, run.id, workspace_id)
                raise
        await maybe_emit(progress_reporter, progress_from_run(run, "queued", "任务已排队。", title=title))
        progress_tasks: list[asyncio.Task] = []
        try:
            runner = self.runtime_registry.get(runtime)
            result = _normalize_result(await _runner_start(
                runner,
                repo_path,
                prompt,
                on_started=lambda pid: self._mark_started(run, pid, progress_reporter, progress_tasks, title),
                on_event=self._runtime_event_callback(run, progress_reporter, progress_tasks, title) if runtime_streaming else None,
            ))
        except Exception as exc:
            result = RuntimeRunResult(session_id=None, summary=f"{_runtime_label(runtime)} 执行异常：{exc}", status="failed")
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        self._finish(run.id, result, runtime)
        finished = self.state.get_run(run.id) or run
        await maybe_emit(progress_reporter, final_progress(finished, result, title=title))
        return self.state.get_run(run.id) or run, result

    def release_binding(self, chat_id: str, thread_key: str, run_id: str | None = None, workspace_id: str = "default") -> None:
        self.state.release_binding_reservation(chat_id, thread_key, run_id, workspace_id)

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
        thread_key: str | None = None,
        workspace_id: str = "default",
        progress_reporter: ProgressReporter | None = None,
        runtime_streaming: bool = False,
        title: str | None = None,
    ) -> tuple[RunRecord, RuntimeRunResult]:
        run: RunRecord | None = None
        if thread_key:
            async with self._lock_for_binding(chat_id, thread_key, workspace_id):
                existing = self.state.get_running_run_for_binding_in_workspace(chat_id, thread_key, workspace_id)
                if existing is not None:
                    raise RunAlreadyActive(existing)
                async with self._lock_for_agent(agent_id, workspace_id):
                    existing = self.running_for_agent(agent_id, workspace_id)
                    if existing is not None:
                        raise RunAlreadyActive(existing)
                    try:
                        run = self.state.create_reserved_run_for_binding(
                            agent_id=agent_id,
                            chat_id=chat_id,
                            thread_key=thread_key,
                            message_id=message_id,
                            repo_alias=repo_alias,
                            repo_path=repo_path,
                            codex_session_id=runtime_session_id,
                            prompt=prompt,
                            runtime=runtime,
                            workspace_id=workspace_id,
                        )
                    except BindingAlreadyReserved as exc:
                        existing = self.state.get_run(exc.run_id) if exc.run_id else None
                        raise RunAlreadyActive(existing) from exc
                    self.state.touch_remote_agent(agent_id, status="running", last_run_id=run.id, last_error=None, workspace_id=workspace_id)
        else:
            async with self._lock_for_agent(agent_id, workspace_id):
                existing = self.running_for_agent(agent_id, workspace_id)
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
                    workspace_id=workspace_id,
                )
                self.state.touch_remote_agent(agent_id, status="running", last_run_id=run.id, last_error=None, workspace_id=workspace_id)
        await maybe_emit(progress_reporter, progress_from_run(run, "queued", "任务已排队。", title=title))
        progress_tasks: list[asyncio.Task] = []
        try:
            try:
                runner = self.runtime_registry.get(runtime)
                result = _normalize_result(await _runner_resume(
                    runner,
                    runtime_session_id,
                    repo_path,
                    prompt,
                    on_started=lambda pid: self._mark_started(run, pid, progress_reporter, progress_tasks, title),
                    on_event=self._runtime_event_callback(run, progress_reporter, progress_tasks, title) if runtime_streaming else None,
                ))
            except Exception as exc:
                result = RuntimeRunResult(session_id=runtime_session_id, summary=f"{_runtime_label(runtime)} 执行异常：{exc}", status="failed")
            if progress_tasks:
                await asyncio.gather(*progress_tasks, return_exceptions=True)
            cancelled = self._cancelled_result(run.id, runtime_session_id)
            if cancelled is not None:
                return cancelled
            self._finish(run.id, result, runtime)
            finished = self.state.get_run(run.id) or run
            await maybe_emit(progress_reporter, final_progress(finished, result, title=title))
            self.state.touch_remote_agent(
                agent_id,
                status="idle" if result.status == "succeeded" else "failed",
                last_run_id=run.id,
                last_error=None if result.status == "succeeded" else result.summary,
                clear_last_error=result.status == "succeeded",
                workspace_id=workspace_id,
            )
            return self.state.get_run(run.id) or run, result
        finally:
            if thread_key and run is not None:
                self.release_binding(chat_id, thread_key, run.id, workspace_id)

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
        workspace_id: str = "default",
        progress_reporter: ProgressReporter | None = None,
        runtime_streaming: bool = False,
        title: str | None = None,
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
                thread_key=thread_key,
                workspace_id=workspace_id,
                progress_reporter=progress_reporter,
                runtime_streaming=runtime_streaming,
                title=title,
            )
        if thread_key:
            async with self._lock_for_binding(chat_id, thread_key, workspace_id):
                existing = self.state.get_running_run_for_binding_in_workspace(chat_id, thread_key, workspace_id)
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
                    workspace_id=workspace_id,
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
                workspace_id=workspace_id,
            )
        await maybe_emit(progress_reporter, progress_from_run(run, "queued", "任务已排队。", title=title))
        progress_tasks: list[asyncio.Task] = []
        try:
            runner = self.runtime_registry.get(runtime)
            result = _normalize_result(await _runner_resume(
                runner,
                runtime_session_id,
                repo_path,
                prompt,
                on_started=lambda pid: self._mark_started(run, pid, progress_reporter, progress_tasks, title),
                on_event=self._runtime_event_callback(run, progress_reporter, progress_tasks, title) if runtime_streaming else None,
            ))
        except Exception as exc:
            result = RuntimeRunResult(session_id=runtime_session_id, summary=f"{_runtime_label(runtime)} 执行异常：{exc}", status="failed")
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        cancelled = self._cancelled_result(run.id, runtime_session_id)
        if cancelled is not None:
            return cancelled
        self._finish(run.id, result, runtime)
        finished = self.state.get_run(run.id) or run
        await maybe_emit(progress_reporter, final_progress(finished, result, title=title))
        return self.state.get_run(run.id) or run, result

    def attach_run_to_agent(
        self,
        run_id: str,
        agent_id: str,
        runtime_session_id: str | None = None,
        runtime: str | None = None,
        workspace_id: str = "default",
    ) -> None:
        self.state.attach_run_to_agent(run_id, agent_id, runtime_session_id, runtime, workspace_id)
        run = self.state.get_run(run_id)
        if run:
            self.state.touch_remote_agent(
                agent_id,
                status="idle" if run.status == "succeeded" else "failed",
                last_run_id=run_id,
                last_error=run.error,
                clear_last_error=run.error is None,
                workspace_id=workspace_id,
            )

    async def cancel_agent(self, agent_id: str, workspace_id: str = "default") -> RunRecord | None:
        run = self.running_for_agent(agent_id, workspace_id)
        if run is None:
            return None
        if run.pid:
            await terminate_process_group(run.pid)
        self.state.finish_run(run.id, "cancelled", summary="用户已取消任务。")
        self.state.touch_remote_agent(agent_id, status="idle", last_run_id=run.id, last_error="用户已取消任务。", workspace_id=workspace_id)
        return self.state.get_run(run.id) or run

    def _mark_started(
        self,
        run: RunRecord,
        pid: int | None,
        progress_reporter: ProgressReporter | None,
        progress_tasks: list[asyncio.Task],
        title: str | None,
    ) -> None:
        self.state.mark_run_running(run.id, pid)
        running = self.state.get_run(run.id) or run
        schedule_emit(progress_tasks, progress_reporter, progress_from_run(running, "running", "任务运行中。", title=title))

    def _runtime_event_callback(
        self,
        run: RunRecord,
        progress_reporter: ProgressReporter | None,
        progress_tasks: list[asyncio.Task],
        title: str | None,
    ):
        async def on_event(event: RuntimeEvent) -> None:
            if event.type == "assistant":
                if progress_tasks:
                    pending = list(progress_tasks)
                    progress_tasks.clear()
                    await asyncio.gather(*pending, return_exceptions=True)
                current = self.state.get_run(run.id) or run
                if current.status in {"succeeded", "failed", "timed_out", "cancelled"}:
                    return
                await maybe_emit(progress_reporter, progress_from_run(current, "assistant", event.text, title=title))

        return on_event

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

    def _lock_for_agent(self, agent_id: str, workspace_id: str = "default") -> asyncio.Lock:
        key = f"{workspace_id}:{agent_id}"
        lock = self._agent_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[key] = lock
        return lock

    def _lock_for_binding(self, chat_id: str, thread_key: str, workspace_id: str = "default") -> asyncio.Lock:
        key = f"{workspace_id}:{chat_id}:{thread_key}"
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


async def _runner_start(runner: Any, repo_path: Path, prompt: str, on_started: Any, on_event: Any | None = None) -> Any:
    if on_event is None:
        return await runner.start(repo_path, prompt, on_started=on_started)
    return await runner.start(repo_path, prompt, on_started=on_started, on_event=on_event)


async def _runner_resume(
    runner: Any,
    session_id: str,
    repo_path: Path,
    prompt: str,
    on_started: Any,
    on_event: Any | None = None,
) -> Any:
    if on_event is None:
        return await runner.resume(session_id, repo_path, prompt, on_started=on_started)
    return await runner.resume(session_id, repo_path, prompt, on_started=on_started, on_event=on_event)
