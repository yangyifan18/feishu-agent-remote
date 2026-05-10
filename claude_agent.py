from collections import defaultdict
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_HISTORY_ROUNDS

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """你是 yyf-agent，一个通过飞书消息与用户交流的开发助手。

你的能力：
- 解答技术问题、讨论代码实现、排查 bug
- 规划开发任务、分析架构方案
- 协助代码 review、编写技术文档

风格要求：
- 回复简洁专业，不啰嗦
- 适当使用 markdown 格式（代码块、列表、加粗等）
- 遇到不确定的问题坦诚说明，不编造
- 中文回复为主，代码和术语用英文"""

# 按 chat_id 存储对话历史
_histories: dict[str, list[dict]] = defaultdict(list)


def chat(chat_id: str, user_message: str) -> str:
    """调用 Claude API，带上下文对话。返回回复文本。"""
    history = _histories[chat_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY_ROUNDS * 2:
        history[:] = history[-(MAX_HISTORY_ROUNDS * 2):]

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply


async def chat_stream(chat_id: str, user_message: str):
    """流式调用 Claude API。yield 每个文本 chunk，最后自动存入历史。"""
    history = _histories[chat_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY_ROUNDS * 2:
        history[:] = history[-(MAX_HISTORY_ROUNDS * 2):]

    full_reply = []
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=history,
    ) as stream:
        for text in stream.text_stream:
            full_reply.append(text)
            yield text

    history.append({"role": "assistant", "content": "".join(full_reply)})


def clear_history(chat_id: str):
    """清除某个会话的对话历史。"""
    _histories.pop(chat_id, None)
