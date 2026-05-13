import asyncio
import logging

logger = logging.getLogger(__name__)


class LarkGateway:
    def __init__(self, lark_cli_bin: str = "lark-cli"):
        self.lark_cli_bin = lark_cli_bin

    async def reply(self, message_id: str, text: str) -> None:
        await self._run(
            [
                self.lark_cli_bin,
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

    async def send_user_message(self, user_id: str, text: str) -> None:
        await self._run(
            [
                self.lark_cli_bin,
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
