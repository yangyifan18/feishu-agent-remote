import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]
Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class ProcessedEventCache:
    def __init__(self, max_size: int = 10000, ttl_seconds: float = 86400):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._events: OrderedDict[str, float] = OrderedDict()

    def seen_or_add(self, key: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        self._prune_expired(current)
        seen_at = self._events.get(key)
        if seen_at is not None and current - seen_at <= self.ttl_seconds:
            self._events.move_to_end(key)
            self._events[key] = current
            return True
        self._events[key] = current
        self._events.move_to_end(key)
        while len(self._events) > self.max_size:
            self._events.popitem(last=False)
        return False

    def _prune_expired(self, now: float) -> None:
        while self._events:
            _, seen_at = next(iter(self._events.items()))
            if now - seen_at <= self.ttl_seconds:
                break
            self._events.popitem(last=False)


class EventConsumerBackoff:
    def __init__(self, initial_delay: float = 5.0, max_delay: float = 60.0, stable_after: float = 60.0):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.stable_after = stable_after
        self._fast_failures = 0

    def delay_after(self, runtime_seconds: float) -> float:
        if runtime_seconds >= self.stable_after:
            self._fast_failures = 0
        delay = min(self.initial_delay * (2 ** self._fast_failures), self.max_delay)
        self._fast_failures += 1
        return delay


async def consume_events_forever(
    lark_cli_bin: str,
    handle_event: EventHandler,
    logger: logging.Logger,
    *,
    lark_cli_args: tuple[str, ...] = (),
    process_factory: ProcessFactory | None = None,
    sleeper: Sleeper = asyncio.sleep,
    clock: Clock = time.monotonic,
    backoff: EventConsumerBackoff | None = None,
    max_cycles: int | None = None,
) -> None:
    factory = process_factory or asyncio.create_subprocess_exec
    restart_backoff = backoff or EventConsumerBackoff()
    cycles = 0
    while True:
        started_at = clock()
        try:
            proc = await factory(
                lark_cli_bin,
                *lark_cli_args,
                "event",
                "consume",
                "im.message.receive_v1",
                "--as",
                "bot",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            cycles += 1
            runtime_seconds = max(0.0, clock() - started_at)
            delay = restart_backoff.delay_after(runtime_seconds)
            logger.exception("failed to start lark-cli event consumer; restarting in %.1fs", delay)
            if max_cycles is not None and cycles >= max_cycles:
                return
            await sleeper(delay)
            continue
        logger.info("Started lark-cli event consumer pid=%s", proc.pid)
        stderr_task = asyncio.create_task(_log_stderr(proc, logger))
        try:
            await _read_events(proc, handle_event, logger)
        finally:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        runtime_seconds = max(0.0, clock() - started_at)
        delay = restart_backoff.delay_after(runtime_seconds)
        logger.warning(
            "lark-cli event consumer exited returncode=%s after %.1fs; restarting in %.1fs",
            proc.returncode,
            runtime_seconds,
            delay,
        )
        await sleeper(delay)


async def _read_events(proc: asyncio.subprocess.Process, handle_event: EventHandler, logger: logging.Logger) -> None:
    if proc.stdout is None:
        raise RuntimeError("lark-cli stdout is not available")
    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Ignoring non-JSON lark event line: %s", line[:300])
            continue
        task = asyncio.create_task(handle_event(event))
        task.add_done_callback(_log_task_exception)
    await proc.wait()


async def _log_stderr(proc: asyncio.subprocess.Process, logger: logging.Logger) -> None:
    if proc.stderr is None:
        return
    async for raw_line in proc.stderr:
        line = raw_line.decode(errors="replace").strip()
        if line:
            logger.info("[lark-cli event] %s", line)


def _log_task_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logging.getLogger(__name__).exception("event handler task crashed")
