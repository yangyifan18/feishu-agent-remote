# feishu-claude-agent Handoff

## 项目概述

飞书 Claude Agent Bot，通过飞书长连接（WebSocket）接收消息，调用 Claude API 流式回复。
Agent 名称：**yyf-agent**

## 当前状态

**代码已完成，尚未配置凭证和测试。** 需要下一步操作：

1. 在飞书开放平台创建应用，获取 App ID / App Secret
2. 配置 `.env` 文件
3. 安装依赖，启动测试

## 技术架构

```
飞书服务器 ←WebSocket→ main.py (FeishuChannel) → claude_agent.py (Anthropic API) → 流式回复
```

### 文件结构

```
feishu-claude-agent/
├── main.py              # 入口，WebSocket 长连接 + 消息事件处理 + 流式回复
├── claude_agent.py      # Claude API 封装，多轮对话上下文管理，流式/非流式两种调用
├── config.py            # 环境变量读取
├── requirements.txt     # 依赖：lark-oapi>=1.2.0, anthropic, python-dotenv
├── .env.example         # 环境变量模板
├── .gitignore
└── HANDOFF.md           # 本文件
```

### 核心依赖

- **lark-oapi >= 1.2.0** — 飞书官方 Python SDK，使用 `FeishuChannel`（channel 模块）实现长连接
- **anthropic** — Claude API SDK，使用 `messages.stream()` 流式调用
- **python-dotenv** — 环境变量管理

### 关键实现细节

#### main.py
- 使用 `lark_oapi.channel.FeishuChannel` 建立 WebSocket 长连接（非旧版 LarkWsClient）
- `@channel.on("message")` 注册消息处理器
- 用 `message_id` 做去重（集合，超过 10000 条清空）
- 流式回复：`channel.stream(chat_id, {"markdown": produce}, {"reply_to": message_id})`
- 支持 `/clear` 和 `清除历史` 命令重置对话
- 错误处理：捕获异常后发送错误提示到飞书

#### claude_agent.py
- `chat_stream(chat_id, user_message)` — 流式调用，async generator，yield 每个 chunk
- `chat(chat_id, user_message)` — 非流式调用，返回完整文本（当前未使用，备用）
- 对话历史按 `chat_id` 隔离，最多保留 `MAX_HISTORY_ROUNDS * 2` 条消息
- System prompt 定义了 yyf-agent 的能力范围和回复风格

#### config.py
- 从 `.env` 读取：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`ANTHROPIC_API_KEY`
- 可选：`CLAUDE_MODEL`（默认 claude-sonnet-4-20250514）、`MAX_HISTORY_ROUNDS`（默认 20）

## 飞书开放平台配置步骤

1. https://open.feishu.cn/ → 开发者后台 → 创建企业自建应用
2. 记录 App ID 和 App Secret
3. 添加应用能力 → 机器人
4. 事件与回调 → 事件订阅方式选 **长连接**
5. 添加事件：`im.message.receive_v1`
6. 权限管理 → 开通 `im:message` + `im:message:send_as_bot`
7. 发布应用，管理员审核

## 环境变量配置

```bash
cp .env.example .env
# 编辑 .env 填入：
# FEISHU_APP_ID=cli_xxx
# FEISHU_APP_SECRET=xxx
# ANTHROPIC_API_KEY=sk-ant-xxx
```

## 启动

```bash
pip install -r requirements.txt
python main.py
```

## 测试验证

1. 启动后日志应显示 WebSocket 连接成功
2. 飞书私聊 bot 发消息 → 应收到流式回复
3. 发 `/clear` → 应提示"对话历史已清除"
4. 连续发多条消息 → bot 应记住上下文

## 待办 / 可扩展方向

- [ ] 配置凭证并完成首次测试
- [ ] 支持群聊 @机器人 触发（当前默认群聊需 @）
- [ ] 支持图片/文件消息处理（当前只处理文本）
- [ ] 部署到云服务器（systemd / docker 保持常驻）
- [ ] 添加更多 Agent 能力（执行代码、调用工具等）
- [ ] 日志持久化
- [ ] 对话历史持久化（当前内存存储，重启丢失）
