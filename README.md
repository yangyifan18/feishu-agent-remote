# 🚀 Feishu Agent Remote

[中文文档](README.zh-CN.md) · English

> Bring your local coding agents into Feishu/Lark: start work from your phone, switch sessions, and ask agents to report progress like online teammates.

Feishu Agent Remote is a lightweight remote-control layer for local coding agents. It listens to Feishu/Lark messages, maps them to local repositories and persistent local agent sessions, then replies back in chat like an always-on online teammate.

```text
💬 Feishu / Lark chat
      ↓
📡 lark-cli event stream
      ↓
🧭 Feishu Agent Remote
      ↓
🧑‍💻 local Codex / Claude sessions + repo allowlist
      ↓
📣 reply / report / optional user-identity sends
```

## ✨ Why

Sometimes your laptop is running, but you are not in front of it.

You may be on your phone, in a Feishu group, trying to:

- ask an agent to continue work in a repo;
- create a fresh isolated Codex or Claude session for a task;
- check what your remote coding helper is doing;
- summarize progress back to yourself or a team chat;
- send a message as yourself, but only after explicit approval.

Feishu Agent Remote turns that flow into a chat-native command console.

## 🧩 Highlights

- **Feishu-first remote control**: private chat or configured group mentions can drive local work.
- **Online helpers**: create named remote agents with `/new`, list them with `/agents`, switch with `/attach`.
- **Persistent runtime sessions**: follow-up messages resume the bound Codex or Claude session instead of starting over.
- **Progress updates**: opt in to lifecycle text replies, Feishu interactive cards, and runtime streaming summaries.
- **Multi-workspace ready**: one local service can supervise multiple Feishu/Lark bot profiles with isolated auth, repos, sessions, runs, and approvals.
- **Repo allowlist**: only configured repositories can be accessed.
- **Owner-only by default**: ignore commands from unauthorized users.
- **Approval-gated user sends**: `/send` creates a confirmation; `/approve` performs the actual user-identity send.
- **CLI-native Feishu integration**: built on the official `lark-cli` event consumer and IM commands.
- **Mac-friendly daemon mode**: manage launchd with `python -m remote_control.cli service ...`.

## 🧪 Status

This project is early but already usable for a personal remote-work setup.

Current backend:

- Feishu/Lark event input: `lark-cli event consume im.message.receive_v1 --as bot`
- Feishu/Lark replies: `lark-cli im +messages-reply --as bot`
- Local agent runtimes: Codex CLI and Claude Code CLI
- Optional UI mode: text progress replies or Feishu interactive progress cards
- State store: SQLite
- Config: local YAML-style file

## ⚡ Quick Start For Humans

This path is for people setting up their own Feishu/Lark remote agent from scratch.

### 1. Prepare accounts and tools

You need:

- official `lark-cli`: https://github.com/larksuite/cli
- Codex CLI installed and working locally; Claude Code CLI is optional for `runtime=claude`
- a Feishu/Lark bot app with message event permissions enabled
- your own Feishu/Lark `open_id`

### 2. Install Feishu Agent Remote

```bash
git clone https://github.com/yangyifan18/feishu-agent-remote.git
cd feishu-agent-remote

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Fill in your Feishu/Lark app credentials:

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FAR_CONFIG=~/.feishu-agent-remote/config.yaml
FAR_STATE=~/.feishu-agent-remote/state.sqlite
```

### 4. Configure owner and repositories

Create `~/.feishu-agent-remote/config.yaml`:

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

features:
  progress_replies: false
  card_replies: false
  runtime_streaming: false
  multi_workspace: false

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
      Review the task as a code reviewer. Task: ${task}
```

`bot_names` should match the display name or mention text of your own Feishu/Lark bot. Pick any name you like; it is not fixed by this project.

If your Codex CLI needs a profile, set `runtimes.codex.profile` to `fastrelay`. To start a Claude helper, use `/new runtime=claude <repo> <title> [task]`. To use a role template, use `/new template=reviewer <repo> <title> [task]`.

Optional progress features are disabled by default. Enable them gradually:

```yaml
features:
  progress_replies: true   # lifecycle text/card updates
  card_replies: false      # true = Feishu interactive card, fallback to text on failure
  runtime_streaming: false # true = show parsed assistant chunks before final result
```

For multiple Feishu/Lark workspaces or bot apps, use separate `lark-cli` profiles and workspace-scoped config:

```yaml
default_workspace: personal
shared_repos:
  agent: /Users/you/Code/feishu-agent-remote

workspaces:
  personal:
    owner_open_id: ou_personal
    authorized_open_ids:
      - ou_personal
    default_repo: agent
    lark_cli_args:
      - --profile
      - personal
    features:
      multi_workspace: true
  team:
    owner_open_id: ou_team
    authorized_open_ids:
      - ou_team
    default_repo: agent
    lark_cli_args:
      - --profile
      - team
    features:
      multi_workspace: true
```

### 5. Optional: migrate legacy config and install launchd

If you used an older `~/.yyf-codex` setup, migrate to the canonical path:

```bash
python -m remote_control.cli migrate-config --dry-run
python -m remote_control.cli migrate-config
```

Keep the bot alive on macOS with launchd:

```bash
python -m remote_control.cli service install
python -m remote_control.cli service status
python -m remote_control.cli service logs --lines 80
```

Tip: `alias far='python -m remote_control.cli'` if you want shorter commands.

### 6. Run and talk to the bot

```bash
.venv/bin/python main.py
```

Private chat:

```text
/status
/new agent agent-console
Continue checking the current repository state
```

Group chat:

```text
@your-bot /status
@your-bot /new app release-helper Check release risks
@your-bot /new runtime=claude app claude-reviewer Review the current diff
```

## 🤖 Quick Start For Agents

This path is for coding agents or automation scripts that need to bootstrap the project quickly.

### 1. Clone, install, verify

```bash
git clone https://github.com/yangyifan18/feishu-agent-remote.git
cd feishu-agent-remote
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py config.py remote_control/*.py
```

### 2. Create local config from templates

```bash
cp .env.example .env
mkdir -p ~/.feishu-agent-remote
cp config.example.yaml ~/.feishu-agent-remote/config.yaml
```

Then fill in, without committing secrets:

- `.env`: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`
- `~/.feishu-agent-remote/config.yaml`: `owner_open_id`, `authorized_open_ids`, `repos`, optional `runtimes`

### 3. Runtime contract

- The process starts with `python main.py`.
- Input comes from `lark-cli event consume im.message.receive_v1 --as bot`.
- Replies are sent through `lark-cli im +messages-reply --as bot`.
- User-identity sends are gated by `/send` + `/approve`.
- Do not write secrets into the repository.
- Before reporting setup complete, run the test and compile commands above.

## 🕹️ Commands

| Command | What it does |
| --- | --- |
| `/help` | Show core commands and context-aware next steps. |
| `/status` | Show the current online helper, repo, status, latest run, and pending confirmations. |
| `/new [runtime=<name>] [template=<name>] <repo> <title> [task]` | Create a new online helper; templates wrap common roles like reviewer/implementer/reporter. |
| plain text | Continue the currently bound helper's runtime session. |
| `/agents [n]` | List online helpers; the current binding is marked with `*`. |
| `/attach <agent_id>` | Switch the current chat context to an existing online helper. |
| `/detach` | Clear the current chat binding without deleting the helper. |
| `/remove <agent_id> [agent_id ...]` | Delete one or more online helpers and clear related bindings. |
| `/rename <agent_id> <title>` | Rename an online helper. |
| `/runs [agent_id] [n]` | Show recent task runs for the current helper, a specific helper, or all helpers. |
| `/cancel [agent_id]` | Cancel a running task; defaults to the current helper. |
| `/repos` | List configured repository aliases. |
| `/switch-repo <alias>` | Switch the current helper's repo alias. |
| `/runtime-sessions [runtime] [n]` | Scan recent local sessions for a runtime; `codex` and `claude` are supported. |
| `/codex-sessions [n]` | Compatibility alias that scans Codex sessions. |
| `/runtimes` | List configured local runtimes and whether their CLI binaries are available. |
| `/templates [name]` | List agent templates or inspect one template. |
| `/handoff [agent_id]` | Ask a helper to produce a structured progress handoff. |
| `/pending` | List pending approval-gated operations. |
| `/send <open_id> <text>` | Prepare a user-identity message and create a confirmation. |
| `/approve <id>` | Approve and execute a pending confirmation. |
| `/reject <id>` | Reject a pending confirmation. |
| `/doctor` | Check local `lark-cli`, runtimes, templates, config/state paths, service hints, repos, and state wiring. |

Compatibility aliases remain available: `/remote-codex` → `/agents`, `/recent-codex` and `/codex-sessions` → `/runtime-sessions codex`, `/close` → `/detach`, `/repo` → `/repos` or `/switch-repo`, and `/summarize` → `/handoff`.

## 💬 Example Flow

```text
You: /new agent agent-console
Bot: 已绑定 `agent-console`。Agent ID：rc_ab12cd34 ...

You: Summarize current repo progress. Do not modify files.
Bot: Current goal ... completed work ... next steps ...

You: /agents 5
Bot: * rc_ab12cd34 `agent-console` repo=agent runtime=codex session=019e...

You: /new runtime=claude app bug-hunter Check recently failing tests
Bot: 已从 `agent-console` 退出，切换到 `bug-hunter` ...

You: /attach rc_ab12cd34
Bot: 已从 `bug-hunter` 退出，切换到 `agent-console` ...
```

## 🔐 Safety Model

Feishu Agent Remote is intentionally conservative.

- Only `authorized_open_ids` can control the bot.
- Repositories must be explicitly listed in config.
- User-identity sends require `/approve`.
- Bot replies and user sends are separated.
- Codex runs use the configured sandbox mode; Claude runs use the configured Claude permission mode.
- Secrets should stay in `.env` and local config, never in shared docs or commits.

This is still a remote-control tool for a local machine. Treat the Feishu bot as a privileged interface and keep the app secret, owner list, and repo allowlist tight.

## 🍎 Run As A macOS LaunchAgent

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

## 🛠️ Development

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py config.py remote_control/*.py
```

Current test coverage includes:

- config loading;
- Codex JSONL and Claude stream-json parsing;
- auth checks;
- session creation and resume;
- remote helper listing, attach, remove;
- approval-gated user sends.

## 🗺️ Roadmap

- Add pure read-only session inspection without resuming a runtime session.
- Add first-class launchd installer/uninstaller.
- Support more local agent CLIs beyond Codex and Claude, such as OpenCode/Gemini.
- Add optional web dashboard for session history.

## 🪪 Name

English: **Feishu Agent Remote**

Chinese: **飞书线上专员**

The idea: your coding agents are no longer trapped inside a terminal window. They become remote, named, chat-addressable helpers that can wait, work, switch context, and report back.

## 📄 License

TBD.
