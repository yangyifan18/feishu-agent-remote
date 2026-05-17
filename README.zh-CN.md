# 🚀 Feishu Agent Remote

中文 · [English](README.md)

> 飞书线上专员：把你的本机 AI 编程 Agent 放进飞书，随时从手机上开工、切换上下文、汇报进度。

Feishu Agent Remote 是一个轻量的本机 Agent 远程控制层。它监听飞书/Lark 消息，把聊天指令映射到本机 repo 和持久化本机 Agent session，然后像一个随时在线的协作伙伴一样在飞书里回复你。

```text
💬 飞书 / Lark 聊天
      ↓
📡 lark-cli 事件流
      ↓
🧭 Feishu Agent Remote
      ↓
🧑‍💻 本机 Codex / Claude sessions + repo 白名单
      ↓
📣 回复 / 汇报 / 可选的 user 身份发送
```

## ✨ 为什么需要它

有时候电脑在跑，但你不在电脑前。

你可能正在手机上、在飞书群里，想要：

- 让 Agent 继续某个 repo 的工作；
- 给一个任务创建全新的隔离 Codex 或 Claude session；
- 查看远程编程助手当前在做什么；
- 让它总结进度并同步给自己或团队；
- 以自己的身份发一条消息，但必须先经过确认。

Feishu Agent Remote 把这些动作变成一个飞书里的远程命令台。

## 🧩 亮点

- **飞书优先的远程控制**：私聊或群聊 @ bot 都能驱动本机 Agent 工作。
- **线上专员**：用 `/new` 创建具名远程 Agent，用 `/agents` 查看，用 `/attach` 切换。
- **持久 runtime session**：普通后续消息会恢复当前绑定的 Codex 或 Claude session，而不是每次重新开始。
- **Repo 白名单**：只允许访问配置过的仓库。
- **默认 owner-only**：未授权用户的命令会被忽略。
- **高影响操作确认**：`/send` 只创建确认单，`/approve` 后才会以 user 身份发送。
- **基于官方 CLI**：事件接收和 IM 回复基于 `lark-cli`。
- **适合 Mac 常驻**：用 `python -m remote_control.cli service ...` 管理 launchd。

## 🧪 当前状态

项目还处于早期，但已经能支撑个人远程工作流。

当前后端：

- 飞书/Lark 事件输入：`lark-cli event consume im.message.receive_v1 --as bot`
- 飞书/Lark 回复：`lark-cli im +messages-reply --as bot`
- 本机 Agent runtime：Codex CLI 和 Claude Code CLI
- 状态存储：SQLite
- 配置文件：本地 YAML 风格配置

## ⚡ Quick Start For Humans

这部分面向想自己搭一个飞书远程 Agent 的用户。

### 1. 准备账号和工具

你需要：

- 官方 `lark-cli`：https://github.com/larksuite/cli
- 本机可正常运行的 Codex CLI；如需 `runtime=claude`，还需要 Claude Code CLI
- 一个已开启消息事件权限的飞书/Lark bot 应用
- 你自己的飞书/Lark `open_id`

### 2. 安装 Feishu Agent Remote

```bash
git clone https://github.com/yangyifan18/feishu-agent-remote.git
cd feishu-agent-remote

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置环境变量

复制 `.env.example` 到 `.env`：

```bash
cp .env.example .env
```

填入你的飞书/Lark 应用凭证：

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FAR_CONFIG=~/.feishu-agent-remote/config.yaml
FAR_STATE=~/.feishu-agent-remote/state.sqlite
```

### 4. 配置 owner 和仓库白名单

创建 `~/.feishu-agent-remote/config.yaml`：

```yaml
owner_open_id: ou_xxxxxxxxxxxxxxxx
default_repo: agent
default_runtime: codex

repos:
  agent: /Users/you/Code/feishu-agent-remote
  app: /Users/you/Code/my-app
  infra: /Users/you/Code/my-infra

authorized_open_ids:
  - ou_xxxxxxxxxxxxxxxx

lark_cli_bin: lark-cli
bot_names:
  - your-bot-name

runtimes:
  codex:
    type: codex
    bin: codex
    profile: null
    sandbox: workspace-write
  claude:
    type: claude
    bin: claude
    permission_mode: acceptEdits

agent_templates:
  reviewer:
    runtime: codex
    description: Review diffs and identify risks.
    prompt: |
      请作为代码审查线上专员工作。任务：${task}
```

`bot_names` 应该填写你自己的飞书/Lark bot 展示名或群聊中的 @ 名称。这个名字由用户自己选择，不是项目固定值。

如果你的 Codex CLI 需要 profile，把 `runtimes.codex.profile` 设置为 `fastrelay`。如果要启动 Claude 专员，用 `/new runtime=claude <repo> <title> [任务]`。如果要使用角色模板，用 `/new template=reviewer <repo> <title> [任务]`。

### 5. 可选：迁移旧配置并安装 launchd

如果你之前使用过 `~/.yyf-codex`，先迁移到 canonical 路径：

```bash
python -m remote_control.cli migrate-config --dry-run
python -m remote_control.cli migrate-config
```

在 macOS 上用 launchd 常驻运行：

```bash
python -m remote_control.cli service install
python -m remote_control.cli service status
python -m remote_control.cli service logs --lines 80
```

如果想少打字，可以设置：`alias far='python -m remote_control.cli'`。

### 6. 启动并和 bot 对话

```bash
.venv/bin/python main.py
```

私聊：

```text
/status
/new agent agent-console
继续检查当前仓库状态
```

群聊：

```text
@your-bot /status
@your-bot /new app release-helper 检查发版风险
@your-bot /new runtime=claude app claude-reviewer review 当前 diff
```

## 🤖 Quick Start For Agents

这部分面向需要快速接手、部署或验证项目的 coding agent / 自动化脚本。

### 1. Clone、安装、验证

```bash
git clone https://github.com/yangyifan18/feishu-agent-remote.git
cd feishu-agent-remote
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py config.py remote_control/*.py
```

### 2. 从模板创建本地配置

```bash
cp .env.example .env
mkdir -p ~/.feishu-agent-remote
cp config.example.yaml ~/.feishu-agent-remote/config.yaml
```

然后补齐配置，但不要提交 secrets：

- `.env`：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- `~/.feishu-agent-remote/config.yaml`：`owner_open_id`、`authorized_open_ids`、`repos`、可选的 `runtimes`

### 3. Runtime contract

- 进程入口是 `python main.py`。
- 输入来自 `lark-cli event consume im.message.receive_v1 --as bot`。
- 回复通过 `lark-cli im +messages-reply --as bot` 发送。
- user 身份发送必须经过 `/send` + `/approve`。
- 不要把 secret 写进 repo。
- 汇报 setup 完成前，先运行上面的 test 和 compile 命令。

## 🕹️ 命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看核心命令和当前上下文提示。 |
| `/status` | 查看当前线上专员、repo、状态、最近任务和待确认事项。 |
| `/new [runtime=<name>] [template=<name>] <repo> <title> [task]` | 创建新的线上专员；模板封装 reviewer/implementer/reporter 等常用角色。 |
| 普通文本 | 继续当前绑定专员的 runtime session。 |
| `/agents [n]` | 列出线上专员；当前绑定会用 `*` 标记。 |
| `/attach <agent_id>` | 将当前聊天上下文切换到已有线上专员。 |
| `/detach` | 解除当前聊天绑定，但不删除线上专员。 |
| `/remove <agent_id> [agent_id ...]` | 删除一个或多个线上专员，并清除相关绑定。 |
| `/rename <agent_id> <title>` | 重命名线上专员。 |
| `/runs [agent_id] [n]` | 查看当前专员、指定专员或全局最近任务记录。 |
| `/cancel [agent_id]` | 取消运行中的任务；默认使用当前专员。 |
| `/repos` | 列出已配置的 repo alias。 |
| `/switch-repo <alias>` | 切换当前线上专员的 repo alias。 |
| `/runtime-sessions [runtime] [n]` | 扫描指定 runtime 的最近本机 session；支持 `codex` 和 `claude`。 |
| `/codex-sessions [n]` | 兼容 alias，扫描 Codex session。 |
| `/runtimes` | 查看已配置 runtime 以及本机 CLI 是否可用。 |
| `/templates [name]` | 查看线上专员模板列表，或查看某个模板详情。 |
| `/handoff [agent_id]` | 让线上专员生成结构化进度交接。 |
| `/pending` | 查看待确认操作。 |
| `/send <open_id> <text>` | 准备一条 user 身份消息，并创建确认单。 |
| `/approve <id>` | 批准并执行待确认操作。 |
| `/reject <id>` | 拒绝待确认操作。 |
| `/doctor` | 检查本机 `lark-cli`、runtime、模板、config/state 路径、service 提示、repo 和 state 连接。 |

兼容 alias 仍可用：`/remote-codex` → `/agents`，`/recent-codex` 和 `/codex-sessions` → `/runtime-sessions codex`，`/close` → `/detach`，`/repo` → `/repos` 或 `/switch-repo`，`/summarize` → `/handoff`。

## 💬 示例流程

```text
你：/new agent agent-console
Bot：已绑定 `agent-console`。Agent ID：rc_ab12cd34 ...

你：总结一下当前 repo 的进展，不要修改文件
Bot：当前目标是 ... 已完成 ... 下一步建议 ...

你：/agents 5
Bot：* rc_ab12cd34 `agent-console` repo=agent runtime=codex session=019e...

你：/new runtime=claude app bug-hunter 检查最近失败的测试
Bot：已从 `agent-console` 退出，切换到 `bug-hunter` ...

你：/attach rc_ab12cd34
Bot：已从 `bug-hunter` 退出，切换到 `agent-console` ...
```

## 🔐 安全模型

Feishu Agent Remote 默认偏保守。

- 只有 `authorized_open_ids` 可以控制 bot。
- 仓库必须显式写在配置文件里。
- user 身份发送必须 `/approve`。
- bot 回复和 user 身份发送分开处理。
- Codex 使用配置中的 sandbox mode；Claude 使用配置中的 permission mode。
- Secrets 应该留在 `.env` 和本地配置中，不要写进共享文档或提交。

这仍然是一个可以远程控制本机的工具。请把飞书 bot 当作高权限入口来对待，谨慎管理 app secret、授权用户列表和 repo 白名单。

## 🍎 作为 macOS LaunchAgent 常驻

如果你希望 Mac 登录后自动保持可访问，可以创建一个 LaunchAgent 来运行：

```bash
/Users/you/Code/feishu-agent-remote/.venv/bin/python /Users/you/Code/feishu-agent-remote/main.py
```

推荐日志路径：

```text
~/Library/Logs/feishu-agent-remote/bot.out.log
~/Library/Logs/feishu-agent-remote/bot.err.log
```

设置 `KeepAlive=true`，让 event consumer 退出时自动重启。

## 🛠️ 开发

运行测试：

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py config.py remote_control/*.py
```

当前测试覆盖：

- 配置读取；
- Codex JSONL 和 Claude stream-json 解析；
- 权限检查；
- session 创建和恢复；
- 线上专员列表、attach、remove；
- 带审批的 user 身份发送。

## 🗺️ Roadmap

- 增加不 resume runtime session 的只读 session inspection。
- 增加 launchd installer/uninstaller。
- 继续扩展 OpenCode/Gemini 等本机 Agent CLI。
- 增加可选的 session history web dashboard。

## 🪪 名字

英文：**Feishu Agent Remote**

中文：**飞书线上专员**

核心想法：你的 coding agents 不再被困在终端窗口里。它们会变成可远程访问、可命名、可切换上下文、可随时汇报的线上专员。

## 📄 License

TBD.
