import asyncio
import logging
from typing import Any

from config import FAR_CONFIG, FAR_STATE
from remote_control.events import ProcessedEventCache, consume_events_forever as consume_lark_events_forever
from remote_control.models import IncomingMessage
from remote_control.workspaces import WorkspaceManager, WorkspaceRuntime, build_workspace_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

workspace_manager = build_workspace_manager(FAR_CONFIG, FAR_STATE)
_default_runtime = workspace_manager.runtime_for()

# Backwards-compatible globals used by older tests and single-workspace tooling.
remote_config = _default_runtime.config
state = _default_runtime.state
lark_gateway = _default_runtime.gateway
router = _default_runtime.router

processed_events = ProcessedEventCache()


async def consume_events_forever() -> None:
    await consume_all_workspaces(workspace_manager)


async def consume_workspace_events_forever(workspace: WorkspaceRuntime, manager: WorkspaceManager | None = None) -> None:
    manager = manager or workspace_manager
    await consume_lark_events_forever(
        workspace.config.lark_cli_bin,
        lambda event: handle_event(event, workspace.workspace_id, manager=manager),
        logger,
        lark_cli_args=workspace.config.lark_cli_args,
    )


async def consume_all_workspaces(manager: WorkspaceManager | None = None) -> None:
    manager = manager or workspace_manager
    workspaces = list(manager.runtimes.values())
    default = manager.runtime_for()
    multi_enabled = any(runtime.config.features and runtime.config.features.multi_workspace for runtime in workspaces)
    if len(workspaces) <= 1 or not multi_enabled:
        await consume_workspace_events_forever(default, manager)
        return
    await asyncio.gather(*(consume_workspace_events_forever(workspace, manager) for workspace in workspaces))


async def handle_event(event: dict[str, Any], workspace_id: str | None = None, *, manager: WorkspaceManager | None = None) -> None:
    using_global_manager = manager is None
    manager = manager or workspace_manager
    runtime = manager.runtime_for(workspace_id)
    is_default_single = using_global_manager and workspace_id is None and runtime.workspace_id == remote_config.workspace_id
    active_config = remote_config if is_default_single else runtime.config
    active_gateway = lark_gateway if is_default_single else runtime.gateway
    active_router = router if is_default_single else runtime.router

    incoming = _incoming_from_event(event, runtime.workspace_id)
    if incoming is None:
        return

    dedup_key = f"{incoming.workspace_id}:{event.get('event_id') or incoming.message_id}"
    if processed_events.seen_or_add(str(dedup_key)):
        return

    text = incoming.content.strip()
    if not text:
        return
    if incoming.chat_type == "group" and not text.startswith("/") and not _mentions_bot(text, active_config.bot_names):
        return

    logger.info(
        "[%s] message=%s sender=%s: %s",
        incoming.chat_type,
        incoming.message_id,
        incoming.sender_id,
        text[:100],
    )
    try:
        await active_router.handle(incoming)
    except Exception as exc:
        logger.exception("Feishu Agent Remote 处理失败")
        try:
            await active_gateway.reply(incoming.message_id, f"处理消息时出错: {exc}")
        except Exception:
            logger.exception("failed to send error reply")




def _mentions_bot(text: str, bot_names: tuple[str, ...] | None = None) -> bool:
    names = bot_names or remote_config.bot_names
    return any(name in text for name in names)


def _incoming_from_event(event: dict[str, Any], workspace_id: str = "default") -> IncomingMessage | None:
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
        workspace_id=workspace_id,
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
