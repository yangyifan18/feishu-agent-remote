import asyncio
import json
import logging
from typing import Any

from config import FAR_CONFIG, FAR_STATE
from remote_control.config import load_config
from remote_control.lark_gateway import LarkGateway
from remote_control.models import IncomingMessage
from remote_control.router import RemoteRouter
from remote_control.runtimes import build_runtime_registry
from remote_control.state import StateStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

remote_config = load_config(FAR_CONFIG)
state = StateStore(FAR_STATE)
lark_gateway = LarkGateway(remote_config.lark_cli_bin)
router = RemoteRouter(
    remote_config,
    state,
    build_runtime_registry(remote_config),
    lark_gateway,
)

_processed: set[str] = set()


async def consume_events_forever() -> None:
    while True:
        proc = await asyncio.create_subprocess_exec(
            remote_config.lark_cli_bin,
            "event",
            "consume",
            "im.message.receive_v1",
            "--as",
            "bot",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("Started lark-cli event consumer pid=%s", proc.pid)
        stderr_task = asyncio.create_task(_log_stderr(proc))
        try:
            await _read_events(proc)
        finally:
            stderr_task.cancel()
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        logger.warning("lark-cli event consumer exited; restarting in 5 seconds")
        await asyncio.sleep(5)


async def _read_events(proc: asyncio.subprocess.Process) -> None:
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


async def _log_stderr(proc: asyncio.subprocess.Process) -> None:
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
        logger.exception("event handler task crashed")


async def handle_event(event: dict[str, Any]) -> None:
    incoming = _incoming_from_event(event)
    if incoming is None:
        return

    dedup_key = event.get("event_id") or incoming.message_id
    if dedup_key in _processed:
        return
    _processed.add(dedup_key)
    if len(_processed) > 10000:
        _processed.clear()

    text = incoming.content.strip()
    if not text:
        return
    if incoming.chat_type == "group" and not text.startswith("/") and not _mentions_bot(text):
        return

    logger.info(
        "[%s] message=%s sender=%s: %s",
        incoming.chat_type,
        incoming.message_id,
        incoming.sender_id,
        text[:100],
    )
    try:
        await router.handle(incoming)
    except Exception as exc:
        logger.exception("Feishu Agent Remote 处理失败")
        try:
            await lark_gateway.reply(incoming.message_id, f"处理消息时出错: {exc}")
        except Exception:
            logger.exception("failed to send error reply")




def _mentions_bot(text: str) -> bool:
    return any(name in text for name in remote_config.bot_names)


def _incoming_from_event(event: dict[str, Any]) -> IncomingMessage | None:
    message_id = str(event.get("message_id") or event.get("id") or "")
    chat_id = str(event.get("chat_id") or "")
    sender_id = str(event.get("sender_id") or "")
    if not message_id or not chat_id or not sender_id:
        logger.warning("Ignoring incomplete lark event: %s", event)
        return None
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=str(event.get("chat_type") or ""),
        sender_id=sender_id,
        content=str(event.get("content") or ""),
    )


if __name__ == "__main__":
    logger.info("Starting Feishu Agent Remote via lark-cli event consumer...")
    asyncio.run(consume_events_forever())
