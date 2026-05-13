from pathlib import Path
from typing import Any

from .models import CodexRunResult, IncomingMessage, RemoteConfig
from .session_finder import CodexSessionFinder
from .state import StateStore


class RemoteRouter:
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
        self.codex_runner = codex_runner
        self.lark = lark_gateway
        self.session_finder = session_finder or CodexSessionFinder()
        self.active_runs: set[str] = set()

    async def handle(self, msg: IncomingMessage) -> None:
        text = _strip_bot_mention(msg.content, self.config.bot_names)
        if not text:
            return

        if msg.sender_id not in self.config.authorized_open_ids:
            await self.lark.reply(msg.message_id, "没有权限：只有授权用户可以控制 Feishu Agent Remote。")
            return

        if text.startswith("/new"):
            await self._new_session(msg, text.removeprefix("/new").strip())
        elif text.startswith("/status"):
            await self._status(msg)
        elif text.startswith("/sessions"):
            await self._sessions(msg)
        elif text.startswith("/remote-codex"):
            await self._remote_codex(msg, text.removeprefix("/remote-codex").strip())
        elif text.startswith("/remove"):
            await self._remove(msg, text.removeprefix("/remove").strip())
        elif text.startswith("/recent-codex"):
            await self._recent_codex(msg, text.removeprefix("/recent-codex").strip())
        elif text.startswith("/attach"):
            await self._attach(msg, text.removeprefix("/attach").strip())
        elif text.startswith("/summarize"):
            await self._summarize(msg, text.removeprefix("/summarize").strip())
        elif text.startswith("/close"):
            self.state.close_session(msg.chat_id, msg.thread_key)
            await self.lark.reply(msg.message_id, "已关闭当前线程绑定的 Codex session。")
        elif text.startswith("/send"):
            await self._send_preview(msg, text.removeprefix("/send").strip())
        elif text.startswith("/approve"):
            await self._approve(msg, text.removeprefix("/approve").strip())
        elif text.startswith("/reject"):
            await self._reject(msg, text.removeprefix("/reject").strip())
        elif text.startswith("/repo"):
            await self._repo(msg, text.removeprefix("/repo").strip())
        else:
            await self._continue_session(msg, text)

    async def _new_session(self, msg: IncomingMessage, args: str) -> None:
        repo_alias, title, prompt = self._parse_new_args(args)
        if not prompt and title:
            prompt = _standby_prompt(title)
        if not prompt:
            await self.lark.reply(msg.message_id, "请提供任务内容，例如：/new repo=agent title=agent-console 检查当前状态。")
            return

        repo_path = self._repo_path(repo_alias)
        if repo_path is None:
            await self.lark.reply(msg.message_id, f"未知 repo：{repo_alias}。可用：{', '.join(self.config.repos)}")
            return

        run_key = f"new:{msg.chat_id}:{_binding_key(msg)}"
        if run_key in self.active_runs:
            await self.lark.reply(msg.message_id, "当前上下文已有一个新建任务在执行，请稍后再试，或用 `/status` 查看。")
            return

        await self.lark.reply(msg.message_id, f"收到，开始在 `{repo_alias}` 执行。")
        self.active_runs.add(run_key)
        try:
            result = _normalize_result(await self.codex_runner.start(repo_path, prompt))
        finally:
            self.active_runs.discard(run_key)
        if result.session_id:
            agent = self.state.create_remote_agent(
                title or _title_from_prompt(prompt),
                repo_alias,
                repo_path,
                result.session_id,
                msg.chat_id,
                _binding_key(msg),
            )
            switch_text = self._bind_agent(msg, agent.id, repo_alias, repo_path, result.session_id)
            if switch_text:
                await self.lark.reply(msg.message_id, switch_text)
        await self.lark.reply(msg.message_id, result.summary)

    async def _continue_session(self, msg: IncomingMessage, prompt: str) -> None:
        binding = self.state.get_session(msg.chat_id, _binding_key(msg))
        if binding is None:
            await self.lark.reply(msg.message_id, "当前线程还没有绑定 Codex session，请先用 `/new repo=<alias> <任务>`。")
            return

        run_key = _run_key(binding)
        if run_key in self.active_runs:
            await self.lark.reply(msg.message_id, "当前线上专员还在处理上一条任务；你可以先用 `/status` 查看，或稍后再发。")
            return

        self.active_runs.add(run_key)
        try:
            result = _normalize_result(
                await self.codex_runner.resume(binding.codex_session_id, binding.repo_path, prompt)
            )
        finally:
            self.active_runs.discard(run_key)
        session_id = result.session_id or binding.codex_session_id
        self.state.upsert_session(
            msg.chat_id,
            _binding_key(msg),
            binding.repo_alias,
            binding.repo_path,
            session_id,
            agent_id=binding.agent_id,
        )
        if binding.agent_id:
            self.state.touch_remote_agent(binding.agent_id)
        await self.lark.reply(msg.message_id, result.summary)

    async def _status(self, msg: IncomingMessage) -> None:
        binding = self.state.get_session(msg.chat_id, _binding_key(msg))
        pending = self.state.list_confirmations()
        if binding is None:
            session_text = "当前上下文未绑定线上专员。可用 `/remote-codex` 查看并 `/attach <agent_id>` 切换。"
        else:
            title = binding.title or "(无标题)"
            agent_id = binding.agent_id or "(未登记)"
            session_text = (
                f"当前线上专员：{title}\n"
                f"Agent ID：{agent_id}\n"
                f"当前 repo：{binding.repo_alias}\n"
                f"Codex session：{binding.codex_session_id}\n"
                f"状态：{self._display_status(binding)}"
            )
        if pending:
            pending_text = "\n待确认：" + ", ".join(item.id for item in pending)
        else:
            pending_text = "\n待确认：无"
        await self.lark.reply(msg.message_id, session_text + pending_text)

    async def _sessions(self, msg: IncomingMessage) -> None:
        sessions = self.state.list_sessions()
        if not sessions:
            await self.lark.reply(msg.message_id, "暂无 Codex session。")
            return
        lines = [
            f"- {item.repo_alias} {item.codex_session_id} chat={item.chat_id} thread={item.thread_key}"
            for item in sessions
        ]
        await self.lark.reply(msg.message_id, "最近 session：\n" + "\n".join(lines))

    async def _remote_codex(self, msg: IncomingMessage, args: str) -> None:
        limit = _parse_limit(args, default=10)
        current = self.state.get_session(msg.chat_id, _binding_key(msg))
        agents = self.state.list_remote_agents(limit=limit)
        if not agents:
            await self.lark.reply(msg.message_id, "还没有线上专员。用 `/new repo=<alias> <任务>` 创建一个。")
            return
        lines = []
        for agent in agents:
            marker = "*" if current and current.agent_id == agent.id else "-"
            status = "running" if _agent_run_key(agent.id) in self.active_runs else agent.status
            lines.append(
                f"{marker} {agent.id} `{agent.title}` repo={agent.repo_alias} "
                f"session={agent.codex_session_id} status={status} updated={agent.updated_at}"
            )
        await self.lark.reply(msg.message_id, "线上专员：\n" + "\n".join(lines))


    async def _remove(self, msg: IncomingMessage, args: str) -> None:
        agent_ids = args.split()
        if not agent_ids:
            await self.lark.reply(msg.message_id, "用法：/remove <agent_id> [agent_id ...]。")
            return

        removed = self.state.delete_remote_agents(agent_ids)
        removed_ids = {agent.id for agent in removed}
        missing = [agent_id for agent_id in agent_ids if agent_id not in removed_ids]
        if not removed and missing:
            await self.lark.reply(msg.message_id, "没有找到要删除的线上专员：" + ", ".join(missing))
            return

        lines = [f"- {agent.id} `{agent.title}` session={agent.codex_session_id}" for agent in removed]
        reply = "已删除线上专员，并解除相关聊天绑定：\n" + "\n".join(lines)
        if missing:
            reply += "\n未找到：" + ", ".join(missing)
        await self.lark.reply(msg.message_id, reply)

    async def _recent_codex(self, msg: IncomingMessage, args: str) -> None:
        limit = _parse_limit(args, default=10)
        sessions = self.session_finder.recent(limit=limit)
        if not sessions:
            await self.lark.reply(msg.message_id, "没有找到本机 Codex session。")
            return
        lines = [
            f"- {item.session_id} cwd={item.cwd} time={item.timestamp or '-'} source={item.source or '-'}"
            for item in sessions
        ]
        await self.lark.reply(msg.message_id, "最近本机 Codex session：\n" + "\n".join(lines))

    async def _attach(self, msg: IncomingMessage, args: str) -> None:
        if args.strip() and not args.strip().startswith("repo="):
            agent = self.state.get_remote_agent(args.strip())
            if agent is not None:
                switch_text = self._bind_agent(
                    msg,
                    agent.id,
                    agent.repo_alias,
                    agent.repo_path,
                    agent.codex_session_id,
                )
                await self.lark.reply(msg.message_id, switch_text or f"已绑定 `{agent.title}`。")
                return

        repo_alias, session_id = self._parse_repo_arg(args)
        if not session_id:
            await self.lark.reply(msg.message_id, "用法：/attach <agent_id> 或 /attach repo=<alias> <codex_session_id>。")
            return
        repo_path = self._repo_path(repo_alias)
        if repo_path is None:
            await self.lark.reply(msg.message_id, f"未知 repo：{repo_alias}。可用：{', '.join(self.config.repos)}")
            return
        title = f"imported {session_id[:8]}"
        agent = self.state.create_remote_agent(title, repo_alias, repo_path, session_id, msg.chat_id, _binding_key(msg))
        switch_text = self._bind_agent(msg, agent.id, repo_alias, repo_path, session_id)
        await self.lark.reply(msg.message_id, switch_text or f"已绑定 `{repo_alias}` 到 Codex session：{session_id}")

    async def _summarize(self, msg: IncomingMessage, args: str) -> None:
        repo_alias, session_id = self._parse_repo_arg(args)
        if not session_id:
            binding = self.state.get_session(msg.chat_id, _binding_key(msg))
            if binding is None:
                await self.lark.reply(msg.message_id, "用法：/summarize repo=<alias> <codex_session_id>，或先 /attach 后再 /summarize。")
                return
            repo_alias = binding.repo_alias
            repo_path = binding.repo_path
            session_id = binding.codex_session_id
        else:
            repo_path = self._repo_path(repo_alias)
            if repo_path is None:
                await self.lark.reply(msg.message_id, f"未知 repo：{repo_alias}。可用：{', '.join(self.config.repos)}")
                return

        prompt = (
            "请总结这个 Codex session 的当前进展。请用中文，包含：目标、已完成工作、"
            "关键修改或产出、已运行验证、当前风险、下一步建议。不要修改文件。"
        )
        await self.lark.reply(msg.message_id, f"开始总结 `{repo_alias}` session：{session_id}")
        run_key = _agent_run_key(binding.agent_id) if "binding" in locals() and binding and binding.agent_id else _session_run_key(session_id)
        if run_key in self.active_runs:
            await self.lark.reply(msg.message_id, "这个线上专员还在处理上一条任务；请稍后再总结。")
            return
        self.active_runs.add(run_key)
        try:
            result = _normalize_result(await self.codex_runner.resume(session_id, repo_path, prompt))
        finally:
            self.active_runs.discard(run_key)
        if result.session_id:
            binding = self.state.get_session(msg.chat_id, _binding_key(msg))
            agent_id = binding.agent_id if binding else None
            self.state.upsert_session(msg.chat_id, _binding_key(msg), repo_alias, repo_path, result.session_id, agent_id=agent_id)
            if agent_id:
                self.state.touch_remote_agent(agent_id)
        await self.lark.reply(msg.message_id, result.summary)

    async def _repo(self, msg: IncomingMessage, args: str) -> None:
        if not args:
            lines = [f"- {alias}: {repo.path}" for alias, repo in self.config.repos.items()]
            await self.lark.reply(msg.message_id, "可用 repo：\n" + "\n".join(lines))
            return
        repo_path = self._repo_path(args)
        if repo_path is None:
            await self.lark.reply(msg.message_id, f"未知 repo：{args}")
            return
        binding = self.state.get_session(msg.chat_id, _binding_key(msg))
        if binding is None:
            await self.lark.reply(msg.message_id, "当前线程还没有 session；请用 `/new repo=<alias> <任务>` 创建。")
            return
        self.state.upsert_session(
            msg.chat_id,
            _binding_key(msg),
            args,
            repo_path,
            binding.codex_session_id,
            agent_id=binding.agent_id,
        )
        await self.lark.reply(msg.message_id, f"当前线程 repo 已切换为 `{args}`。")

    def _bind_agent(
        self,
        msg: IncomingMessage,
        agent_id: str,
        repo_alias: str,
        repo_path: Path,
        codex_session_id: str,
    ) -> str:
        key = _binding_key(msg)
        previous = self.state.get_session(msg.chat_id, key)
        self.state.upsert_session(
            msg.chat_id,
            key,
            repo_alias,
            repo_path,
            codex_session_id,
            agent_id=agent_id,
        )
        self.state.touch_remote_agent(agent_id)
        current = self.state.get_remote_agent(agent_id)
        current_title = current.title if current else codex_session_id
        if previous and previous.agent_id and previous.agent_id != agent_id:
            previous_title = previous.title or previous.codex_session_id
            return f"已从 `{previous_title}` 退出，切换到 `{current_title}`。\nAgent ID：{agent_id}\nSession：{codex_session_id}"
        return f"已绑定 `{current_title}`。\nAgent ID：{agent_id}\nSession：{codex_session_id}"

    async def _send_preview(self, msg: IncomingMessage, args: str) -> None:
        target, text = _split_first(args)
        if not target or not text:
            await self.lark.reply(msg.message_id, "用法：/send <open_id> <内容>。")
            return
        confirmation = self.state.create_confirmation(
            "send_user_message",
            msg.sender_id,
            msg.chat_id,
            msg.message_id,
            {"user_id": target, "text": text},
        )
        await self.lark.reply(
            msg.message_id,
            f"将以你的 user 身份发送给 `{target}`：\n{text}\n\n确认发送：/approve {confirmation.id}\n取消：/reject {confirmation.id}",
        )

    async def _approve(self, msg: IncomingMessage, confirmation_id: str) -> None:
        confirmation = self.state.get_confirmation(confirmation_id.strip())
        if confirmation is None or confirmation.status != "pending":
            await self.lark.reply(msg.message_id, "确认单不存在或已处理。")
            return
        if confirmation.requester_id != msg.sender_id:
            await self.lark.reply(msg.message_id, "只能由创建确认单的用户批准。")
            return
        if confirmation.action == "send_user_message":
            await self.lark.send_user_message(confirmation.payload["user_id"], confirmation.payload["text"])
            self.state.mark_confirmation(confirmation.id, "approved")
            await self.lark.reply(msg.message_id, f"已发送：{confirmation.id}")
            return
        await self.lark.reply(msg.message_id, f"未知确认动作：{confirmation.action}")

    async def _reject(self, msg: IncomingMessage, confirmation_id: str) -> None:
        confirmation = self.state.get_confirmation(confirmation_id.strip())
        if confirmation is None or confirmation.status != "pending":
            await self.lark.reply(msg.message_id, "确认单不存在或已处理。")
            return
        self.state.mark_confirmation(confirmation.id, "rejected")
        await self.lark.reply(msg.message_id, f"已取消：{confirmation.id}")

    def _parse_repo_arg(self, args: str) -> tuple[str, str]:
        repo_alias = self.config.default_repo
        parts = args.split()
        if parts and parts[0].startswith("repo="):
            repo_alias = parts[0].split("=", 1)[1]
            return repo_alias, " ".join(parts[1:]).strip()
        return repo_alias, args.strip()

    def _parse_new_args(self, args: str) -> tuple[str, str | None, str]:
        parts = args.split()
        repo_alias = self.config.default_repo
        title: str | None = None

        if parts and parts[0].startswith("repo="):
            repo_alias = parts.pop(0).split("=", 1)[1]
        elif parts and parts[0] in self.config.repos:
            repo_alias = parts.pop(0)

        if parts and parts[0].startswith("title="):
            title = parts.pop(0).split("=", 1)[1]
        elif parts and not parts[0].startswith("/"):
            # Positional shorthand: /new <repo> <title> <prompt>
            if args.split() and args.split()[0] in self.config.repos:
                title = parts.pop(0)

        return repo_alias, title, " ".join(parts).strip()

    def _repo_path(self, alias: str) -> Path | None:
        repo = self.config.repos.get(alias)
        return repo.path if repo else None

    def _display_status(self, binding) -> str:
        return "running" if _run_key(binding) in self.active_runs else binding.status


def _normalize_result(result: Any) -> CodexRunResult:
    if isinstance(result, CodexRunResult):
        return result
    return CodexRunResult(session_id=result.get("session_id"), summary=result.get("summary", ""))


def _strip_bot_mention(text: str, bot_names: tuple[str, ...]) -> str:
    stripped = text.strip()
    for name in bot_names:
        for prefix in (f"@{name}", name):
            if stripped.startswith(prefix):
                return stripped.removeprefix(prefix).strip()
    return stripped


def _split_first(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def _parse_limit(text: str, default: int) -> int:
    stripped = text.strip()
    if not stripped:
        return default
    try:
        return max(1, min(20, int(stripped)))
    except ValueError:
        return default


def _binding_key(msg: IncomingMessage) -> str:
    if msg.chat_type == "p2p":
        return f"chat:{msg.chat_id}"
    if msg.thread_id:
        return f"thread:{msg.thread_id}"
    if msg.root_message_id:
        return f"thread:{msg.root_message_id}"
    return f"chat:{msg.chat_id}"


def _run_key(binding) -> str:
    if binding.agent_id:
        return _agent_run_key(binding.agent_id)
    return _session_run_key(binding.codex_session_id)


def _agent_run_key(agent_id: str) -> str:
    return f"agent:{agent_id}"


def _session_run_key(session_id: str) -> str:
    return f"session:{session_id}"


def _title_from_prompt(prompt: str) -> str:
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else "remote codex"
    return first_line[:30]


def _standby_prompt(title: str) -> str:
    return (
        f"创建名为 `{title}` 的线上专员会话并待命。"
        "请只用一句中文确认已待命，不要修改任何文件。"
    )
