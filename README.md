# Feishu Agent Remote

> 飞书线上专员：把你的本机 AI 编程 Agent 放进飞书，随时从手机上开工、切换上下文、汇报进度。

Feishu Agent Remote is a lightweight remote-control layer for local coding agents. It listens to Feishu/Lark messages, maps them to local repositories and persistent Codex sessions, then replies back in chat like an always-on online teammate.

```text
Feishu / Lark chat
      ↓
lark-cli event stream
      ↓
Feishu Agent Remote
      ↓
local Codex sessions + repo allowlist
      ↓
reply / report / optional user-identity sends
```

## Why

Sometimes your laptop is running, but you are not in front of it.

You may be on your phone, in a Feishu group, trying to:

- ask an agent to continue work in a repo;
- create a fresh isolated Codex session for a task;
- check what your remote coding helper is doing;
- summarize progress back to yourself or a team chat;
- send a message as yourself, but only after explicit approval.

Feishu Agent Remote turns that flow into a chat-native command console.

## Highlights

- **Feishu-first remote control**: private chat or configured group mentions can drive local work.
- **Online helpers**: create named remote agents with `/new`, list them with `/remote-codex`, switch with `/attach`.
- **Persistent Codex sessions**: follow-up messages resume the bound local Codex session instead of starting over.
- **Repo allowlist**: only configured repositories can be accessed.
- **Owner-only by default**: ignore commands from unauthorized users.
- **Approval-gated user sends**: `/send` creates a confirmation; `/approve` performs the actual user-identity send.
- **CLI-native Feishu integration**: built on the official `lark-cli` event consumer and IM commands.
- **Mac-friendly daemon mode**: can be kept alive with `launchd`.

## Status

This project is early but already usable for a personal remote-work setup.

Current backend:

- Feishu/Lark event input: `lark-cli event consume im.message.receive_v1 --as bot`
- Feishu/Lark replies: `lark-cli im +messages-reply --as bot`
- Local agent runtime: `codex exec` / `codex resume`
- State store: SQLite
- Config: local YAML-style file

## Quick Start

### 1. Install dependencies

```bash
git clone https://github.com/<you>/feishu-agent-remote.git
cd feishu-agent-remote

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

You also need:

- official `lark-cli`: https://github.com/larksuite/cli
- Codex CLI installed and working locally
- a Feishu/Lark bot app with message event permissions enabled

### 2. Configure environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Fill in your Feishu/Lark app credentials:

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
YYF_CODEX_CONFIG=~/.yyf-codex/config.yaml
YYF_CODEX_STATE=~/.yyf-codex/state.sqlite
```

### 3. Configure repositories and owner

Create `~/.yyf-codex/config.yaml`:

```yaml
owner_open_id: ou_xxxxxxxxxxxxxxxx
default_repo: agent

repos:
  agent: /Users/you/Code/feishu-agent-remote
  app: /Users/you/Code/my-app
  infra: /Users/you/Code/my-infra

authorized_open_ids:
  - ou_xxxxxxxxxxxxxxxx

codex_bin: codex
codex_profile: null
lark_cli_bin: lark-cli
default_sandbox: workspace-write
bot_names:
  - feishu-agent-remote
  - your-bot-name
```

If your Codex CLI needs a profile, set it here:

```yaml
codex_profile: fastrelay
```

### 4. Run locally

```bash
.venv/bin/python main.py
```

You should see the `lark-cli event` consumer become ready.

### 5. Talk to the bot

Private chat:

```text
/status
/new agent agent-console
继续检查当前仓库状态
```

Group chat:

```text
@your-bot /status
@your-bot /new app release-helper 检查发版风险
```

## Commands

| Command | What it does |
| --- | --- |
| `/new repo=<alias> title=<title> [task]` | Create a new online helper and bind it to the current chat context. |
| `/new <alias> <title> [task]` | Shorthand form of `/new`. |
| plain text | Continue the currently bound Codex session. |
| `/status` | Show the current helper, agent id, repo, Codex session id, status, and pending confirmations. |
| `/remote-codex [n]` | List online helpers created or imported by this bot. |
| `/attach <agent_id>` | Switch the current chat context to an existing online helper. |
| `/attach repo=<alias> <codex_session_id>` | Import an existing local Codex session as an online helper. |
| `/remove <agent_id> [agent_id ...]` | Delete one or more online helpers and clear their chat bindings. |
| `/recent-codex [n]` | Scan local `~/.codex/sessions` for recent Codex sessions. |
| `/summarize repo=<alias> <codex_session_id>` | Ask a Codex session to summarize progress. |
| `/repo` | List configured repository aliases. |
| `/repo <alias>` | Switch the repo alias for the current bound session. |
| `/sessions` | List stored local chat-session bindings. |
| `/close` | Close the current chat binding without deleting the helper. |
| `/send <open_id> <text>` | Prepare a user-identity message and create a confirmation. |
| `/approve <id>` | Approve and execute a pending confirmation. |
| `/reject <id>` | Reject a pending confirmation. |

## Example Flow

```text
You: /new agent agent-console
Bot: 已绑定 `agent-console`。Agent ID：rc_ab12cd34 ...

You: 总结一下当前 repo 的进展，不要修改文件
Bot: 当前目标是 ... 已完成 ... 下一步建议 ...

You: /remote-codex 5
Bot: * rc_ab12cd34 `agent-console` repo=agent session=019e...

You: /new app bug-hunter 检查最近失败的测试
Bot: 已从 `agent-console` 退出，切换到 `bug-hunter` ...

You: /attach rc_ab12cd34
Bot: 已从 `bug-hunter` 退出，切换到 `agent-console` ...
```

## Safety Model

Feishu Agent Remote is intentionally conservative.

- Only `authorized_open_ids` can control the bot.
- Repositories must be explicitly listed in config.
- User-identity sends require `/approve`.
- Bot replies and user sends are separated.
- Codex runs use the configured sandbox mode.
- Secrets should stay in `.env` and local config, never in shared docs or commits.

This is still a remote-control tool for a local machine. Treat the Feishu bot as a privileged interface and keep the app secret, owner list, and repo allowlist tight.

## Run As A macOS LaunchAgent

For a personal Mac that should stay reachable after login, create a LaunchAgent that runs:

```bash
/Users/you/Code/feishu-agent-remote/.venv/bin/python /Users/you/Code/feishu-agent-remote/main.py
```

Recommended log paths:

```text
~/Library/Logs/feishu-agent-remote/bot.out.log
~/Library/Logs/feishu-agent-remote/bot.err.log
```

Use `KeepAlive=true` so the bot restarts if the event consumer exits.

## Development

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py config.py remote_control/*.py
```

Current test coverage includes:

- config loading;
- Codex JSONL parsing;
- auth checks;
- session creation and resume;
- remote helper listing, attach, remove;
- approval-gated user sends.

## Roadmap

- Rename hardcoded bot trigger text into config.
- Add `/rename` for online helpers.
- Add pure read-only session inspection without resuming Codex.
- Add `/cancel` for long-running Codex subprocesses.
- Add first-class launchd installer/uninstaller.
- Support more local agent CLIs beyond Codex.
- Add optional web dashboard for session history.

## Name

English: **Feishu Agent Remote**

Chinese: **飞书线上专员**

The idea: your coding agents are no longer trapped inside a terminal window. They become remote, named, chat-addressable helpers that can wait, work, switch context, and report back.

## License

TBD.
