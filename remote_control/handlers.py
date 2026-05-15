from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .commands import Command
from .models import RuntimeRunResult, IncomingMessage, RemoteAgent, RemoteConfig, RunRecord, SessionBinding
from .run_manager import RunAlreadyActive, RunManager
from .runtimes import RuntimeRegistry
from .session_finder import RuntimeSessionFinder
from .state import StateStore


class CommandHandlers:
    """Concrete command behavior for Feishu Agent Remote."""

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
        self.runtime_registry = codex_runner if isinstance(codex_runner, RuntimeRegistry) else RuntimeRegistry({"codex": codex_runner}, default_runtime=config.default_runtime)
        self.codex_runner = self.runtime_registry.get("codex") if self.runtime_registry.has("codex") else None
        self.run_manager = RunManager(state, self.runtime_registry)
        self.lark = lark_gateway
        self.session_finder = session_finder or RuntimeSessionFinder()

    async def _help(self, msg: IncomingMessage, command: Command) -> None:
        binding = self._current_binding(msg)
        current = f"当前：`{binding.title or binding.agent_id}` repo={binding.repo_alias}" if binding else "当前未绑定线上专员"
        text = (
            f"Feishu Agent Remote 帮助\n{current}\n\n"
            "常用命令：\n"
            "- `/new [runtime=<name>] <repo> <title> [任务]` 创建线上专员\n"
            "- `/agents` 查看线上专员\n"
            "- `/attach <agent_id>` 切换专员\n"
            "- `/status` 查看当前状态\n"
            "- `/runs` 查看最近任务\n"
            "- `/cancel` 取消当前任务\n"
            "- `/rename <agent_id> <title>` 重命名\n"
            "- `/runtimes` 查看本机 runtime\n"
            "- `/doctor` 检查本机配置\n\n"
            "示例：`/new agent agent-console 总结当前 repo`"
        )
        await self.lark.reply(msg.message_id, text)

    async def _new_session(self, msg: IncomingMessage, command: Command) -> None:
        runtime, rest = self._parse_runtime_arg(command.args)
        if not self.runtime_registry.has(runtime):
            await self.lark.reply(msg.message_id, f"未知 runtime：{runtime}。可用：{', '.join(self.runtime_registry.names())}")
            return
        repo_alias, title, prompt = self._parse_new_args(rest)
        if not prompt and title:
            prompt = _standby_prompt(title)
        if not prompt:
            await self.lark.reply(msg.message_id, "用法：/new [runtime=<name>] <repo> <title> [任务]，例如 `/new runtime=claude agent agent-console 检查当前状态`。")
            return

        repo_path = self._repo_path(repo_alias)
        if repo_path is None:
            await self.lark.reply(msg.message_id, f"未知 repo：{repo_alias}。可用：{', '.join(self.config.repos)}")
            return

        await self.lark.reply(msg.message_id, f"收到，开始创建线上专员 `{title or _title_from_prompt(prompt)}`（runtime={runtime}）。")
        run, result = await self.run_manager.start_new(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            repo_alias=repo_alias,
            repo_path=repo_path,
            prompt=prompt,
            runtime=runtime,
        )
        if result.session_id:
            agent = self.state.create_remote_agent(
                title or _title_from_prompt(prompt),
                repo_alias,
                repo_path,
                result.session_id,
                msg.chat_id,
                _binding_key(msg),
                status="idle" if result.status == "succeeded" else "failed",
                runtime=runtime,
            )
            self.run_manager.attach_run_to_agent(run.id, agent.id, result.session_id, runtime)
            switch_text = self._bind_agent(msg, agent.id, repo_alias, repo_path, result.session_id, runtime)
            await self.lark.reply(msg.message_id, switch_text)
        await self.lark.reply(msg.message_id, result.summary)

    async def _continue_session(self, msg: IncomingMessage, prompt: str) -> None:
        binding = self._current_binding(msg)
        if binding is None:
            await self.lark.reply(msg.message_id, "当前聊天还没有绑定线上专员，请先用 `/new <repo> <title> [任务]`。")
            return
        if binding.agent_id is None:
            await self.lark.reply(msg.message_id, "当前绑定缺少 Agent ID，请重新 `/attach <agent_id>`。")
            return

        try:
            run, result = await self.run_manager.resume_agent(
                agent_id=binding.agent_id,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                repo_alias=binding.repo_alias,
                repo_path=binding.repo_path,
                runtime_session_id=binding.runtime_session_id or binding.codex_session_id,
                prompt=prompt,
                runtime=binding.runtime,
            )
        except RunAlreadyActive as exc:
            await self.lark.reply(msg.message_id, f"当前线上专员还在处理上一条任务：{exc.run.id}。可用 `/runs` 查看，或 `/cancel` 取消。")
            return
        self._update_binding_after_result(msg, binding, result)
        if result.status != "cancelled":
            await self.lark.reply(msg.message_id, result.summary)

    async def _status(self, msg: IncomingMessage, command: Command) -> None:
        binding = self._current_binding(msg)
        pending = self.state.list_confirmations(requester_id=msg.sender_id, chat_id=msg.chat_id)
        if binding is None:
            session_text = "当前聊天未绑定线上专员。可用 `/agents` 查看，或 `/new <repo> <title>` 创建。"
        else:
            agent_id = binding.agent_id or "(未登记)"
            run = self.state.get_running_run_for_agent(binding.agent_id) if binding.agent_id else None
            last_run = run or (self.state.get_run(binding.last_run_id) if binding.last_run_id else None)
            session_text = (
                f"当前线上专员：{binding.title or '(无标题)'}\n"
                f"Agent ID：{agent_id}\n"
                f"Repo：{binding.repo_alias}\n"
                f"Runtime：{binding.runtime}\n"
                f"Session：{binding.runtime_session_id or binding.codex_session_id}\n"
                f"状态：{run.status if run else binding.status}\n"
                f"最近任务：{_format_run_line(last_run) if last_run else '无'}"
            )
            if binding.last_error:
                session_text += f"\n最近错误：{_truncate(binding.last_error, 80)}"
        pending_text = "\n待确认：" + (", ".join(item.id for item in pending) if pending else "无")
        await self.lark.reply(msg.message_id, session_text + pending_text + "\n\n可用：/runs /cancel /detach /help")

    async def _bindings(self, msg: IncomingMessage, command: Command) -> None:
        sessions = self.state.list_sessions()
        if not sessions:
            await self.lark.reply(msg.message_id, "暂无聊天绑定。")
            return
        lines = [f"- agent={item.agent_id or '-'} repo={item.repo_alias} chat={item.chat_id} thread={item.thread_key}" for item in sessions]
        await self.lark.reply(msg.message_id, "聊天绑定：\n" + "\n".join(lines))

    async def _agents(self, msg: IncomingMessage, command: Command) -> None:
        limit = _parse_limit(command.args, default=10)
        current = self._current_binding(msg)
        agents = self.state.list_remote_agents(limit=limit)
        if not agents:
            await self.lark.reply(msg.message_id, "还没有线上专员。用 `/new <repo> <title> [任务]` 创建一个。")
            return
        lines = []
        for agent in agents:
            marker = "*" if current and current.agent_id == agent.id else "-"
            status = self._agent_status(agent)
            lines.append(
                f"{marker} {agent.id} `{agent.title}` repo={agent.repo_alias} "
                f"runtime={agent.runtime} session={agent.runtime_session_id or agent.codex_session_id} "
                f"status={status} last_run={agent.last_run_id or '-'} updated={agent.updated_at}"
            )
        suffix = "\n提示：旧命令 `/remote-codex` 仍可用，但建议改用 `/agents`。" if command.raw_name == "remote-codex" else ""
        await self.lark.reply(msg.message_id, "线上专员：\n" + "\n".join(lines) + suffix)

    async def _remove(self, msg: IncomingMessage, command: Command) -> None:
        agent_ids = command.args.split()
        if not agent_ids:
            await self.lark.reply(msg.message_id, "用法：/remove <agent_id> [agent_id ...]。")
            return
        busy = [agent_id for agent_id in agent_ids if self.state.get_running_run_for_agent(agent_id)]
        if busy:
            await self.lark.reply(msg.message_id, "这些线上专员仍有任务运行，请先 `/cancel`：" + ", ".join(busy))
            return
        removed = self.state.delete_remote_agents(agent_ids)
        removed_ids = {agent.id for agent in removed}
        missing = [agent_id for agent_id in agent_ids if agent_id not in removed_ids]
        if not removed and missing:
            await self.lark.reply(msg.message_id, "没有找到要删除的线上专员：" + ", ".join(missing))
            return
        lines = [f"- {agent.id} `{agent.title}`" for agent in removed]
        reply = "已删除线上专员，并解除相关聊天绑定：\n" + "\n".join(lines)
        if missing:
            reply += "\n未找到：" + ", ".join(missing)
        await self.lark.reply(msg.message_id, reply)

    async def _runtime_sessions(self, msg: IncomingMessage, command: Command) -> None:
        runtime, limit = self._parse_runtime_sessions_args(command)
        if not self.runtime_registry.has(runtime):
            await self.lark.reply(msg.message_id, f"未知 runtime：{runtime}。可用：{', '.join(self.runtime_registry.names())}")
            return
        try:
            sessions = self.session_finder.recent(limit=limit, runtime=runtime)
        except TypeError:
            sessions = self.session_finder.recent(limit=limit)
        if not sessions:
            await self.lark.reply(msg.message_id, f"没有找到本机 {runtime} session。")
            return
        lines = [f"- {item.session_id} runtime={item.runtime} cwd={item.cwd} time={item.timestamp or '-'} source={item.source or '-'}" for item in sessions]
        suffix = "\n提示：旧命令仍可用，但建议改用 `/runtime-sessions [runtime]`。" if command.raw_name in {"recent-codex", "codex-sessions"} else ""
        await self.lark.reply(msg.message_id, f"最近本机 {runtime} session：\n" + "\n".join(lines) + suffix)

    async def _attach(self, msg: IncomingMessage, command: Command) -> None:
        args = command.args.strip()
        if args and not args.startswith("repo="):
            agent = self.state.get_remote_agent(args)
            if agent is not None:
                switch_text = self._bind_agent(msg, agent.id, agent.repo_alias, agent.repo_path, agent.runtime_session_id or agent.codex_session_id, agent.runtime)
                await self.lark.reply(msg.message_id, switch_text)
                return
        runtime, rest = self._parse_runtime_arg(args)
        repo_alias, session_id = self._parse_repo_arg(rest)
        if not session_id:
            await self.lark.reply(msg.message_id, "用法：/attach <agent_id> 或 /attach [runtime=<name>] repo=<alias> <session_id>。")
            return
        if not self.runtime_registry.has(runtime):
            await self.lark.reply(msg.message_id, f"未知 runtime：{runtime}。可用：{', '.join(self.runtime_registry.names())}")
            return
        repo_path = self._repo_path(repo_alias)
        if repo_path is None:
            await self.lark.reply(msg.message_id, f"未知 repo：{repo_alias}。可用：{', '.join(self.config.repos)}")
            return
        title = f"imported {session_id[:8]}"
        agent = self.state.create_remote_agent(title, repo_alias, repo_path, session_id, msg.chat_id, _binding_key(msg), runtime=runtime)
        switch_text = self._bind_agent(msg, agent.id, repo_alias, repo_path, session_id, runtime)
        await self.lark.reply(msg.message_id, switch_text)

    async def _handoff(self, msg: IncomingMessage, command: Command) -> None:
        agent, repo_alias, repo_path, session_id, runtime = self._resolve_handoff_target(msg, command.args)
        if session_id is None or repo_path is None or repo_alias is None:
            await self.lark.reply(msg.message_id, "用法：/handoff [agent_id]，或先 /attach 后再 /handoff。")
            return
        prompt = (
            "请生成这个线上专员/runtime session 的标准交接总结。请用中文，包含：目标、已完成工作、"
            "关键修改或产出、已运行验证、当前风险、下一步建议。不要修改文件。"
        )
        await self.lark.reply(msg.message_id, f"开始生成交接总结：{agent.title if agent else session_id}")
        try:
            run, result = await self.run_manager.resume_session(
                agent_id=agent.id if agent else None,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                repo_alias=repo_alias,
                repo_path=repo_path,
                runtime_session_id=session_id,
                prompt=prompt,
                runtime=runtime,
            )
        except RunAlreadyActive as exc:
            await self.lark.reply(msg.message_id, f"这个线上专员还在处理上一条任务：{exc.run.id}。请稍后再交接，或 `/cancel`。")
            return
        if agent:
            self._update_binding_after_result(msg, self._binding_from_agent(msg, agent), result)
        if result.status != "cancelled":
            await self.lark.reply(msg.message_id, result.summary)

    async def _detach(self, msg: IncomingMessage, command: Command) -> None:
        self.state.close_session(msg.chat_id, _binding_key(msg))
        suffix = "（旧命令 `/close` 仍可用，建议改用 `/detach`）" if command.raw_name == "close" else ""
        await self.lark.reply(msg.message_id, "已解除当前聊天绑定，不会删除线上专员。" + suffix)

    async def _send_preview(self, msg: IncomingMessage, command: Command) -> None:
        target, text = _split_first(command.args)
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
        await self.lark.reply(msg.message_id, f"将以你的 user 身份发送给 `{target}`：\n{text}\n\n确认发送：/approve {confirmation.id}\n取消：/reject {confirmation.id}")

    async def _pending(self, msg: IncomingMessage, command: Command) -> None:
        pending = self.state.list_confirmations(requester_id=msg.sender_id, chat_id=msg.chat_id)
        if not pending:
            await self.lark.reply(msg.message_id, "当前没有待确认操作。")
            return
        lines = [f"- {item.id} action={item.action} message={_truncate(str(item.payload), 80)}" for item in pending]
        await self.lark.reply(msg.message_id, "待确认操作：\n" + "\n".join(lines) + "\n\n确认：/approve <id>；取消：/reject <id>")

    async def _approve_single_pending(self, msg: IncomingMessage) -> None:
        pending = self.state.list_confirmations(requester_id=msg.sender_id, chat_id=msg.chat_id)
        if len(pending) == 1:
            await self._approve(msg, Command("approve", pending[0].id, "确认", "确认", False))
        elif len(pending) > 1:
            await self.lark.reply(msg.message_id, "当前有多个待确认操作，请用 `/pending` 查看后 `/approve <id>`。")
        else:
            await self._continue_session(msg, "确认")

    async def _approve(self, msg: IncomingMessage, command: Command) -> None:
        confirmation = self.state.get_confirmation(command.args.strip())
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

    async def _reject(self, msg: IncomingMessage, command: Command) -> None:
        confirmation = self.state.get_confirmation(command.args.strip())
        if confirmation is None or confirmation.status != "pending":
            await self.lark.reply(msg.message_id, "确认单不存在或已处理。")
            return
        self.state.mark_confirmation(confirmation.id, "rejected")
        await self.lark.reply(msg.message_id, f"已取消：{confirmation.id}")

    async def _repos(self, msg: IncomingMessage, command: Command) -> None:
        lines = [f"- {alias}: {repo.path}" for alias, repo in self.config.repos.items()]
        await self.lark.reply(msg.message_id, "可用 repo：\n" + "\n".join(lines))

    async def _switch_repo(self, msg: IncomingMessage, command: Command) -> None:
        alias = command.args.strip()
        repo_path = self._repo_path(alias)
        if repo_path is None:
            await self.lark.reply(msg.message_id, f"未知 repo：{alias}")
            return
        binding = self._current_binding(msg)
        if binding is None or not binding.agent_id:
            await self.lark.reply(msg.message_id, "当前聊天还没有绑定线上专员；请先 `/new` 或 `/attach`。")
            return
        self.state.update_remote_agent_repo(binding.agent_id, alias, repo_path)
        self.state.upsert_session(
            msg.chat_id,
            _binding_key(msg),
            alias,
            repo_path,
            binding.runtime_session_id or binding.codex_session_id,
            agent_id=binding.agent_id,
            runtime=binding.runtime,
        )
        suffix = "（旧命令 `/repo <alias>` 仍可用，建议改用 `/switch-repo <alias>`）" if command.raw_name == "repo" else ""
        await self.lark.reply(msg.message_id, f"当前线上专员 repo 已切换为 `{alias}`。" + suffix)

    async def _rename(self, msg: IncomingMessage, command: Command) -> None:
        agent_id, title = _split_first(command.args)
        if not agent_id or not title:
            await self.lark.reply(msg.message_id, "用法：/rename <agent_id> <new-title>。")
            return
        title = title[:60]
        agent = self.state.rename_remote_agent(agent_id, title)
        if agent is None:
            await self.lark.reply(msg.message_id, f"没有找到线上专员：{agent_id}")
            return
        current = self._current_binding(msg)
        prefix = "当前线上专员已重命名" if current and current.agent_id == agent_id else "线上专员已重命名"
        await self.lark.reply(msg.message_id, f"{prefix}：`{agent.title}` ({agent.id})")

    async def _runs(self, msg: IncomingMessage, command: Command) -> None:
        agent_id, limit = self._parse_runs_args(msg, command.args)
        runs = self.state.list_runs(agent_id=agent_id, limit=limit)
        if not runs:
            await self.lark.reply(msg.message_id, "暂无任务记录。")
            return
        lines = [f"- {_format_run_line(run)}" for run in runs]
        await self.lark.reply(msg.message_id, "最近任务：\n" + "\n".join(lines))

    async def _cancel(self, msg: IncomingMessage, command: Command) -> None:
        agent_id = command.args.strip()
        if not agent_id:
            binding = self._current_binding(msg)
            agent_id = binding.agent_id if binding else ""
        if not agent_id:
            await self.lark.reply(msg.message_id, "当前聊天未绑定线上专员。用法：/cancel <agent_id>。")
            return
        run = self.run_manager.cancel_agent(agent_id)
        if run is None:
            await self.lark.reply(msg.message_id, "当前没有运行中的任务。")
            return
        await self.lark.reply(msg.message_id, f"已取消任务：{run.id}")

    async def _runtimes(self, msg: IncomingMessage, command: Command) -> None:
        await self.lark.reply(msg.message_id, "Runtimes：\n" + "\n".join(self.runtime_registry.status_lines()))

    async def _doctor(self, msg: IncomingMessage, command: Command) -> None:
        lark_path = shutil.which(self.config.lark_cli_bin)
        config_path = self.config.config_path
        checks = [
            _check_line("lark-cli executable", bool(lark_path), self.config.lark_cli_bin),
            _check_line("lark-cli event list", await _command_ok([self.config.lark_cli_bin, "event", "list"]) if lark_path else False, "event list"),
            _check_line("config parsed", config_path is None or config_path.exists(), str(config_path or "in-memory")),
            _check_line("owner_open_id", bool(self.config.owner_open_id), self.config.owner_open_id or "missing"),
            _check_line("authorized users", bool(self.config.authorized_open_ids), str(len(self.config.authorized_open_ids))),
            _check_line("state sqlite", self.state.path.exists(), str(self.state.path)),
            _check_line("state writable", self.state.check_writable(), str(self.state.path.parent)),
        ]
        for name in self.runtime_registry.names():
            runtime_config = self.config.runtimes.get(name) if self.config.runtimes else None
            bin_name = runtime_config.bin if runtime_config else name
            path = shutil.which(bin_name)
            checks.append(_check_line(f"runtime {name}", bool(path), bin_name))
        for alias, repo in self.config.repos.items():
            checks.append(_check_line(f"repo {alias}", repo.path.exists(), str(repo.path)))
        await self.lark.reply(msg.message_id, "Doctor：\n" + "\n".join(checks))

    def _current_binding(self, msg: IncomingMessage) -> SessionBinding | None:
        return self.state.get_session(msg.chat_id, _binding_key(msg))

    def _agent_status(self, agent: RemoteAgent) -> str:
        run = self.state.get_running_run_for_agent(agent.id)
        return run.status if run else agent.status

    def _binding_from_agent(self, msg: IncomingMessage, agent: RemoteAgent) -> SessionBinding:
        return SessionBinding(
            chat_id=msg.chat_id,
            thread_key=_binding_key(msg),
            repo_alias=agent.repo_alias,
            repo_path=agent.repo_path,
            codex_session_id=agent.codex_session_id,
            status=agent.status,
            agent_id=agent.id,
            title=agent.title,
            last_run_id=agent.last_run_id,
            last_error=agent.last_error,
            runtime=agent.runtime,
            runtime_session_id=agent.runtime_session_id or agent.codex_session_id,
        )

    def _update_binding_after_result(self, msg: IncomingMessage, binding: SessionBinding, result: RuntimeRunResult) -> None:
        session_id = result.session_id or binding.runtime_session_id or binding.codex_session_id
        self.state.upsert_session(msg.chat_id, _binding_key(msg), binding.repo_alias, binding.repo_path, session_id, agent_id=binding.agent_id, runtime=binding.runtime)
        if binding.agent_id and result.session_id:
            self.state.update_remote_agent_session(binding.agent_id, result.session_id, binding.runtime)

    def _bind_agent(self, msg: IncomingMessage, agent_id: str, repo_alias: str, repo_path: Path, runtime_session_id: str, runtime: str = "codex") -> str:
        key = _binding_key(msg)
        previous = self.state.get_session(msg.chat_id, key)
        self.state.upsert_session(msg.chat_id, key, repo_alias, repo_path, runtime_session_id, agent_id=agent_id, runtime=runtime)
        self.state.touch_remote_agent(agent_id)
        current = self.state.get_remote_agent(agent_id)
        current_title = current.title if current else runtime_session_id
        if previous and previous.agent_id and previous.agent_id != agent_id:
            previous_title = previous.title or previous.runtime_session_id or previous.codex_session_id
            return f"已从 `{previous_title}` 退出，切换到 `{current_title}`。\nAgent ID：{agent_id}\nRuntime：{runtime}\nSession：{runtime_session_id}"
        return f"已绑定 `{current_title}`。\nAgent ID：{agent_id}\nruntime={runtime}\nSession：{runtime_session_id}"

    def _parse_runs_args(self, msg: IncomingMessage, args: str) -> tuple[str | None, int]:
        parts = args.split()
        agent_id: str | None = None
        limit = 5
        if parts:
            if parts[0].startswith("rc_"):
                agent_id = parts.pop(0)
            elif parts[0].isdigit():
                limit = _parse_limit(parts.pop(0), default=5)
        if parts and parts[0].isdigit():
            limit = _parse_limit(parts[0], default=limit)
        if agent_id is None:
            binding = self._current_binding(msg)
            agent_id = binding.agent_id if binding else None
            if agent_id is None:
                limit = max(limit, 10)
        return agent_id, limit

    def _resolve_handoff_target(self, msg: IncomingMessage, args: str) -> tuple[RemoteAgent | None, str | None, Path | None, str | None, str]:
        stripped = args.strip()
        if stripped.startswith("repo="):
            repo_alias, session_id = self._parse_repo_arg(stripped)
            repo_path = self._repo_path(repo_alias)
            return None, repo_alias, repo_path, session_id, "codex"
        if stripped.startswith("runtime="):
            runtime, rest = self._parse_runtime_arg(stripped)
            repo_alias, session_id = self._parse_repo_arg(rest)
            repo_path = self._repo_path(repo_alias)
            return None, repo_alias, repo_path, session_id, runtime
        if stripped:
            agent = self.state.get_remote_agent(stripped)
            if agent:
                return agent, agent.repo_alias, agent.repo_path, agent.runtime_session_id or agent.codex_session_id, agent.runtime
        binding = self._current_binding(msg)
        if binding:
            agent = self.state.get_remote_agent(binding.agent_id) if binding.agent_id else None
            return agent, binding.repo_alias, binding.repo_path, binding.runtime_session_id or binding.codex_session_id, binding.runtime
        return None, None, None, None, "codex"

    def _parse_repo_arg(self, args: str) -> tuple[str, str]:
        repo_alias = self.config.default_repo
        parts = args.split()
        if parts and parts[0].startswith("repo="):
            repo_alias = parts[0].split("=", 1)[1]
            return repo_alias, " ".join(parts[1:]).strip()
        return repo_alias, args.strip()

    def _parse_runtime_arg(self, args: str) -> tuple[str, str]:
        runtime = self.config.default_runtime
        parts = args.split()
        if parts and parts[0].startswith("runtime="):
            runtime = parts.pop(0).split("=", 1)[1] or runtime
        return runtime, " ".join(parts)

    def _parse_runtime_sessions_args(self, command: Command) -> tuple[str, int]:
        parts = command.args.split()
        runtime = "codex" if command.raw_name in {"codex-sessions", "recent-codex"} else self.config.default_runtime
        if parts and not parts[0].isdigit():
            runtime = parts.pop(0)
        limit = _parse_limit(parts[0], default=10) if parts else 10
        return runtime, limit

    def _parse_new_args(self, args: str) -> tuple[str, str | None, str]:
        parts = args.split()
        repo_alias = self.config.default_repo
        title: str | None = None
        if parts and parts[0].startswith("repo="):
            repo_alias = parts.pop(0).split("=", 1)[1]
        elif parts and not parts[0].startswith("title="):
            repo_alias = parts.pop(0)
        if parts and parts[0].startswith("title="):
            title = parts.pop(0).split("=", 1)[1]
        elif parts and not parts[0].startswith("/") and args.split() and args.split()[0] in self.config.repos:
            title = parts.pop(0)
        return repo_alias, title, " ".join(parts).strip()

    def _repo_path(self, alias: str) -> Path | None:
        repo = self.config.repos.get(alias)
        return repo.path if repo else None


def _binding_key(msg: IncomingMessage) -> str:
    if msg.chat_type == "p2p":
        return f"chat:{msg.chat_id}"
    if msg.thread_id:
        return f"thread:{msg.thread_id}"
    if msg.root_message_id:
        return f"thread:{msg.root_message_id}"
    return f"chat:{msg.chat_id}"


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


def _title_from_prompt(prompt: str) -> str:
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else "remote agent"
    return first_line[:30]


def _standby_prompt(title: str) -> str:
    return f"创建名为 `{title}` 的线上专员会话并待命。请只用一句中文确认已待命，不要修改任何文件。"


def _format_run_line(run: RunRecord | None) -> str:
    if run is None:
        return "无"
    duration = _duration(run.started_at or run.created_at, run.finished_at)
    prompt = _truncate(run.prompt, 60)
    return f"{run.id} {run.status} {duration} `{prompt}`"


def _duration(start: str | None, end: str | None) -> str:
    if not start:
        return "-"
    try:
        started = datetime.fromisoformat(start.replace(" ", "T"))
        ended = datetime.fromisoformat(end.replace(" ", "T")) if end else datetime.now()
    except ValueError:
        return "-"
    seconds = max(0, int((ended - started).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _check_line(name: str, ok: bool, detail: str) -> str:
    return f"{'✅' if ok else '❌'} {name}: {detail}"


async def _command_ok(argv: list[str]) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        return proc.returncode == 0
    except Exception:
        return False
