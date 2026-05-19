from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class MessageRef:
    workspace_id: str
    chat_id: str
    message_id: str
    thread_key: str


@dataclass(frozen=True)
class ReplyHandle:
    mode: Literal["text", "card"]
    workspace_id: str
    message_id: str
    card_id: str | None = None


class ReplyGateway(Protocol):
    async def reply_text(self, ref: MessageRef, text: str) -> ReplyHandle: ...
    async def create_progress_card(self, ref: MessageRef, progress: object) -> ReplyHandle: ...
    async def update_progress_card(self, handle: ReplyHandle, progress: object) -> None: ...
    async def send_user_message(self, user_id: str, text: str) -> None: ...
