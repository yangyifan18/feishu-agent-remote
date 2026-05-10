import asyncio
import logging
from lark_oapi.channel import FeishuChannel
from config import FEISHU_APP_ID, FEISHU_APP_SECRET
from claude_agent import chat_stream, clear_history

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

channel = FeishuChannel(
    app_id=FEISHU_APP_ID,
    app_secret=FEISHU_APP_SECRET,
)

# 去重：记录已处理的 message_id
_processed: set[str] = set()


@channel.on("message")
async def on_message(msg):
    message_id = msg.message_id

    # 去重
    if message_id in _processed:
        return
    _processed.add(message_id)
    if len(_processed) > 10000:
        _processed.clear()

    chat_id = msg.chat_id
    text = msg.content_text or ""

    # 特殊命令：清除历史
    if text.strip() in ("/clear", "清除历史"):
        clear_history(chat_id)
        await channel.send(chat_id, {"text": "对话历史已清除。"})
        return

    if not text.strip():
        return

    logger.info(f"[{msg.chat_type}] {msg.sender_name or msg.sender_id}: {text[:100]}")

    try:
        async def produce(stream):
            async for chunk in chat_stream(chat_id, text):
                await stream.append(chunk)

        await channel.stream(
            chat_id,
            {"markdown": produce},
            {"reply_to": message_id},
        )
    except Exception as e:
        logger.exception("Claude API 调用失败")
        await channel.send(
            chat_id,
            {"text": f"处理消息时出错: {e}"},
            {"reply_to": message_id},
        )


@channel.on("error")
async def on_error(err):
    logger.error(f"Channel error: {err}")


@channel.on("reconnecting")
async def on_reconnecting():
    logger.warning("WebSocket reconnecting...")


@channel.on("reconnected")
async def on_reconnected():
    logger.info("WebSocket reconnected.")


if __name__ == "__main__":
    logger.info("Starting Feishu Claude Agent Bot...")
    asyncio.run(channel.connect())
