import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from .models import RunRecord, RuntimeRunResult
from .replies import MessageRef, ReplyGateway, ReplyHandle

logger = logging.getLogger(__name__)

FINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled"}


@dataclass(frozen=True)
class RunProgress:
    run_id: str
    agent_id: str | None
    title: str
    repo_alias: str
    runtime: str
    status: str
    text: str
    updated_at: str | None = None


@dataclass(frozen=True)
class RuntimeEvent:
    type: Literal["session", "assistant", "tool", "status", "raw"]
    text: str
    session_id: str | None = None


RuntimeEventCallback = Callable[[RuntimeEvent], Awaitable[None] | None]


class ProgressReporter(Protocol):
    async def update(self, progress: RunProgress) -> None: ...


class NullProgressReporter:
    async def update(self, progress: RunProgress) -> None:
        return None


class TextProgressReporter:
    def __init__(self, gateway: ReplyGateway, ref: MessageRef):
        self.gateway = gateway
        self.ref = ref

    async def update(self, progress: RunProgress) -> None:
        await self.gateway.reply_text(self.ref, format_text_progress(progress))


class CardProgressReporter:
    def __init__(
        self,
        gateway: ReplyGateway,
        ref: MessageRef,
        *,
        min_interval_seconds: float = 5.0,
        state: object | None = None,
    ):
        self.gateway = gateway
        self.ref = ref
        self.min_interval_seconds = min_interval_seconds
        self.state = state
        self.handle: ReplyHandle | None = None
        self._last_status: str | None = None
        self._last_text: str | None = None
        self._last_update = 0.0

    async def update(self, progress: RunProgress) -> None:
        if self.handle is None:
            await self._create(progress)
            return
        if self.handle.mode == "text":
            if progress.status in FINAL_STATUSES:
                self.handle = await self.gateway.reply_text(self.ref, format_text_progress(progress))
                self._save(progress)
            return
        if not self._should_update(progress):
            return
        try:
            await self.gateway.update_progress_card(self.handle, progress)
            self._mark_updated(progress)
            self._save(progress)
        except Exception as exc:
            logger.warning("progress card update failed; falling back to text: %s", exc)
            self.handle = await self.gateway.reply_text(self.ref, format_text_progress(progress))
            self._mark_updated(progress)
            self._save(progress)

    async def _create(self, progress: RunProgress) -> None:
        try:
            self.handle = await self.gateway.create_progress_card(self.ref, progress)
        except Exception as exc:
            logger.warning("progress card create failed; falling back to text: %s", exc)
            self.handle = await self.gateway.reply_text(self.ref, format_text_progress(progress))
        self._mark_updated(progress)
        self._save(progress)

    def _should_update(self, progress: RunProgress) -> bool:
        if progress.status in FINAL_STATUSES:
            return True
        if progress.status != self._last_status:
            return True
        if progress.text != self._last_text and time.monotonic() - self._last_update >= self.min_interval_seconds:
            return True
        return False

    def _mark_updated(self, progress: RunProgress) -> None:
        self._last_status = progress.status
        self._last_text = progress.text
        self._last_update = time.monotonic()

    def _save(self, progress: RunProgress) -> None:
        if self.state is None or self.handle is None:
            return
        try:
            self.state.save_run_reply(progress.run_id, self.handle)
        except Exception:
            logger.exception("failed to save run reply handle")


def progress_from_run(run: RunRecord, status: str, text: str, title: str | None = None) -> RunProgress:
    return RunProgress(
        run_id=run.id,
        agent_id=run.agent_id,
        title=title or run.agent_id or "agentless",
        repo_alias=run.repo_alias,
        runtime=run.runtime,
        status=status,
        text=text,
    )


def final_progress(run: RunRecord, result: RuntimeRunResult, title: str | None = None) -> RunProgress:
    return progress_from_run(run, result.status, result.summary, title=title)


def format_text_progress(progress: RunProgress) -> str:
    text = _shorten(progress.text, 1200)
    return (
        f"Run {progress.run_id} {progress.status}\n"
        f"Agent：{progress.title}\n"
        f"Repo：{progress.repo_alias}\n"
        f"Runtime：{progress.runtime}\n"
        f"{text}"
    )


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def maybe_emit(reporter: ProgressReporter | None, progress: RunProgress) -> None:
    if reporter is not None:
        await reporter.update(progress)


def schedule_emit(tasks: list[asyncio.Task], reporter: ProgressReporter | None, progress: RunProgress) -> None:
    if reporter is not None:
        tasks.append(asyncio.create_task(reporter.update(progress)))
