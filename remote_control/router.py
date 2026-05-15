from typing import Any

from .commands import parse_command
from .handlers import CommandHandlers
from .models import IncomingMessage, RemoteConfig
from .session_finder import RuntimeSessionFinder
from .state import StateStore


class RemoteRouter:
    """Auth and dispatch layer for incoming Feishu messages."""

    def __init__(
        self,
        config: RemoteConfig,
        state: StateStore,
        codex_runner: Any,
        lark_gateway: Any,
        session_finder: Any | None = None,
    ):
        self.config = config
        self.state = state
        self.lark = lark_gateway
        self.handlers = CommandHandlers(
            config,
            state,
            codex_runner,
            lark_gateway,
            session_finder or RuntimeSessionFinder(),
        )

    async def handle(self, msg: IncomingMessage) -> None:
        command = parse_command(msg.content, self.config.bot_names)
        if not command.raw_text:
            return

        if msg.sender_id not in self.config.authorized_open_ids:
            await self.lark.reply(msg.message_id, "没有权限：只有授权用户可以控制 Feishu Agent Remote。")
            return

        if not command.is_command and command.raw_text.strip() == "确认":
            await self.handlers._approve_single_pending(msg)
            return

        if not command.is_command:
            await self.handlers._continue_session(msg, command.raw_text)
            return

        handlers = {
            "help": self.handlers._help,
            "new": self.handlers._new_session,
            "status": self.handlers._status,
            "bindings": self.handlers._bindings,
            "agents": self.handlers._agents,
            "remove": self.handlers._remove,
            "runtime-sessions": self.handlers._runtime_sessions,
            "attach": self.handlers._attach,
            "handoff": self.handlers._handoff,
            "detach": self.handlers._detach,
            "send": self.handlers._send_preview,
            "approve": self.handlers._approve,
            "reject": self.handlers._reject,
            "pending": self.handlers._pending,
            "repos": self.handlers._repos,
            "switch-repo": self.handlers._switch_repo,
            "rename": self.handlers._rename,
            "runs": self.handlers._runs,
            "cancel": self.handlers._cancel,
            "runtimes": self.handlers._runtimes,
            "doctor": self.handlers._doctor,
        }
        handler = handlers.get(command.name)
        if handler is None:
            await self.lark.reply(msg.message_id, "未知命令。发送 `/help` 查看可用命令。")
            return
        await handler(msg, command)
