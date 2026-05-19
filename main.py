import asyncio
import logging
from typing import Any

from config import FAR_CONFIG, FAR_STATE
from remote_control.config import load_config
from remote_control.events import ProcessedEventCache, consume_events_forever as consume_lark_events_forever
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

processed_events = ProcessedEventCache()


async def consume_events_forever() -> None:
    await consume_lark_events_forever(remote_config.lark_cli_bin, handle_event, logger)


async def handle_event(event: dict[str, Any]) -> None:
    incoming = _incoming_from_event(event)
    if incoming is None:
        return

    dedup_key = event.get("event_id") or incoming.message_id
    if processed_events.seen_or_add(str(dedup_key)):
        return

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
        thread_id=_event_text(event, "thread_id", "threadID", "thread"),
        root_message_id=_event_text(event, "root_message_id", "root_id", "parent_id", "parent_message_id"),
    )


def _event_text(event: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = event.get(key)
        if value:
            return str(value)
    message = event.get("message")
    if isinstance(message, dict):
        for key in keys:
            value = message.get(key)
            if value:
                return str(value)
    return None


if __name__ == "__main__":
    logger.info("Starting Feishu Agent Remote via lark-cli event consumer...")
    asyncio.run(consume_events_forever())
