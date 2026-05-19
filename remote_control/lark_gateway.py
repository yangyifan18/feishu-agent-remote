import asyncio
import json
import logging
import re

from .replies import MessageRef, ReplyHandle

logger = logging.getLogger(__name__)


class LarkGateway:
    def __init__(self, lark_cli_bin: str = "lark-cli", lark_cli_args: tuple[str, ...] = ()):
        self.lark_cli_bin = lark_cli_bin
        self.lark_cli_args = tuple(lark_cli_args)

    @property
    def _prefix(self) -> list[str]:
        return [self.lark_cli_bin, *self.lark_cli_args]

    async def reply(self, message_id: str, text: str) -> None:
        await self._run(
            [
                *self._prefix,
                "im",
                "+messages-reply",
                "--as",
                "bot",
                "--message-id",
                message_id,
                "--text",
                text,
            ]
        )

    async def reply_text(self, ref: MessageRef, text: str) -> ReplyHandle:
        await self.reply(ref.message_id, text)
        return ReplyHandle(mode="text", workspace_id=ref.workspace_id, message_id=ref.message_id)

    async def create_progress_card(self, ref: MessageRef, progress: object) -> ReplyHandle:
        output = await self._run_json(
            [
                *self._prefix,
                "im",
                "+messages-reply",
                "--as",
                "bot",
                "--message-id",
                ref.message_id,
                "--msg-type",
                "interactive",
                "--content",
                json.dumps(_build_progress_card(progress), ensure_ascii=False),
            ]
        )
        data = output.get("data") if isinstance(output, dict) else {}
        message_id = str((data or {}).get("message_id") or output.get("message_id") or "")
        card_id = str((data or {}).get("card_id") or output.get("card_id") or message_id)
        if not card_id:
            raise RuntimeError("lark-cli card create returned no message_id/card_id")
        return ReplyHandle(mode="card", workspace_id=ref.workspace_id, message_id=message_id or ref.message_id, card_id=card_id)

    async def update_progress_card(self, handle: ReplyHandle, progress: object) -> None:
        if not handle.message_id:
            raise RuntimeError("progress card handle has no message_id")
        await self._run_json(
            [
                *self._prefix,
                "api",
                "PATCH",
                f"/open-apis/im/v1/messages/{handle.message_id}",
                "--as",
                "bot",
                "--data",
                json.dumps(
                    {
                        "msg_type": "interactive",
                        "content": json.dumps(_build_progress_card(progress), ensure_ascii=False),
                    },
                    ensure_ascii=False,
                ),
            ]
        )

    async def send_user_message(self, user_id: str, text: str) -> None:
        await self._run(
            [
                *self._prefix,
                "im",
                "+messages-send",
                "--as",
                "user",
                "--user-id",
                user_id,
                "--text",
                text,
            ]
        )

    async def _run(self, argv: list[str]) -> None:
        logger.info("Running lark-cli command: %s", " ".join(argv[:4]))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            details = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            logger.error("lark-cli command failed: %s", details)
            raise RuntimeError(details or f"{argv[0]} exited with {proc.returncode}")
        output = stdout.decode(errors="replace").strip()
        if output:
            logger.info("lark-cli command output: %s", output[:500])

    async def _run_json(self, argv: list[str]) -> dict:
        logger.info("Running lark-cli command: %s", " ".join(argv[:4]))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            details = stderr.decode(errors="replace").strip() or output
            logger.error("lark-cli command failed: %s", details)
            raise RuntimeError(details or f"{argv[0]} exited with {proc.returncode}")
        if output:
            logger.info("lark-cli command output: %s", output[:500])
        try:
            parsed = json.loads(output or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"lark-cli returned non-JSON output: {output[:200]}") from exc
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            raise RuntimeError(str(parsed.get("error") or parsed))
        return parsed if isinstance(parsed, dict) else {}


def _build_progress_card(progress: object) -> dict:
    status = str(getattr(progress, "status", "running"))
    title = str(getattr(progress, "title", "agent"))
    run_id = str(getattr(progress, "run_id", "run"))
    repo_alias = str(getattr(progress, "repo_alias", "repo"))
    runtime = str(getattr(progress, "runtime", "runtime"))
    text = _redact_progress_text(str(getattr(progress, "text", "")))[:800]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": f"{title} · {status}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**Run**: `{run_id}`\\n**Repo**: `{repo_alias}`\\n**Runtime**: `{runtime}`"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": text or status}},
        ],
    }


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*([^\s]+)"),
    re.compile(r"(?i)(bearer)\s+([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"),
)


def _redact_progress_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
