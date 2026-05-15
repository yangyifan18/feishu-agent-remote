import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from remote_control.codex_runner import parse_codex_jsonl
from remote_control.codex_runner import CodexRunner
from remote_control.config import load_config
from remote_control.models import CodexSessionMeta
from remote_control.models import IncomingMessage
from remote_control.router import RemoteRouter
from remote_control.state import StateStore


class FakeCodexRunner:
    def __init__(self):
        self.calls = []

    async def start(self, repo_path, prompt, on_started=None):
        self.calls.append(("start", str(repo_path), prompt))
        if on_started:
            on_started(12345)
        return {"session_id": "codex-new", "summary": "started work", "status": "succeeded"}

    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        if on_started:
            on_started(12345)
        return {"session_id": session_id, "summary": "continued work", "status": "succeeded"}


class FailingCodexRunner(FakeCodexRunner):
    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        if on_started:
            on_started(12345)
        raise RuntimeError("boom")


class TimedOutCodexRunner(FakeCodexRunner):
    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        if on_started:
            on_started(12345)
        return {"session_id": session_id, "summary": "timeout", "status": "timed_out"}


class SlowCodexRunner(FakeCodexRunner):
    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        await asyncio.sleep(0.01)
        if on_started:
            on_started(12345)
        await asyncio.sleep(0.05)
        return {"session_id": session_id, "summary": f"done {prompt}", "status": "succeeded"}


class CancellableCodexRunner(FakeCodexRunner):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        if on_started:
            on_started(999999)
        self.started.set()
        await asyncio.sleep(0.05)
        return {"session_id": session_id, "summary": "finished after cancel", "status": "succeeded"}


class DelayedStartCodexRunner(FakeCodexRunner):
    def __init__(self):
        super().__init__()
        self.before_started = asyncio.Event()

    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        self.before_started.set()
        await asyncio.sleep(0.02)
        if on_started:
            on_started(999999)
        await asyncio.sleep(0.02)
        return {"session_id": session_id, "summary": "late success", "status": "succeeded"}


class FakeLarkGateway:
    def __init__(self):
        self.replies = []
        self.sent = []

    async def reply(self, message_id, text):
        self.replies.append((message_id, text))

    async def send_user_message(self, user_id, text):
        self.sent.append((user_id, text))


class FakeSessionFinder:
    def __init__(self):
        self.sessions = [
            CodexSessionMeta(
                session_id="019e-existing",
                cwd=Path("/tmp/agent"),
                timestamp="2026-05-12T03:00:00Z",
                source="exec",
                path=Path("/tmp/session.jsonl"),
            )
        ]

    def recent(self, limit=10):
        return self.sessions[:limit]


def message(text, sender="ou_owner", chat="oc_chat", message_id="om_msg"):
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat,
        chat_type="p2p",
        sender_id=sender,
        content=text,
    )


class RemoteControlTests(unittest.TestCase):
    def test_load_config_reads_yaml_repo_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "owner_open_id: ou_owner",
                        "default_repo: agent",
                        "repos:",
                        "  agent: /tmp/agent",
                        "authorized_open_ids:",
                        "  - ou_owner",
                    ]
                )
            )

            config = load_config(config_path)

            self.assertEqual(config.owner_open_id, "ou_owner")
            self.assertEqual(config.default_repo, "agent")
            self.assertEqual(config.repos["agent"].path, Path("/tmp/agent"))
            self.assertIn("ou_owner", config.authorized_open_ids)

    def test_load_config_reads_optional_codex_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "owner_open_id: ou_owner",
                        "default_repo: agent",
                        "repos:",
                        "  agent: /tmp/agent",
                        "codex_profile: fastrelay",
                    ]
                )
            )

            config = load_config(config_path)

            self.assertEqual(config.codex_profile, "fastrelay")

    def test_codex_runner_puts_profile_before_exec_command(self):
        runner = CodexRunner(profile="fastrelay")

        self.assertEqual(runner._base_argv(), ["codex", "--profile", "fastrelay"])

    def test_load_config_expands_tilde_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                config_dir = Path(tmp) / ".feishu-agent-remote"
                config_dir.mkdir()
                (config_dir / "config.yaml").write_text(
                    "\n".join(
                        [
                            "owner_open_id: ou_owner",
                            "default_repo: agent",
                            "repos:",
                            "  agent: /tmp/agent",
                        ]
                    )
                )

                config = load_config("~/.feishu-agent-remote/config.yaml")

                self.assertEqual(config.owner_open_id, "ou_owner")
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

    def test_parse_codex_jsonl_extracts_session_and_last_message(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "019e-session"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    }
                ),
            ]
        )

        parsed = parse_codex_jsonl(raw)

        self.assertEqual(parsed.session_id, "019e-session")
        self.assertEqual(parsed.last_message, "done")

    def test_parse_codex_jsonl_supports_current_thread_and_agent_message_events(self):
        raw = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "019e-thread"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "remote smoke ok",
                        },
                    }
                ),
            ]
        )

        parsed = parse_codex_jsonl(raw)

        self.assertEqual(parsed.session_id, "019e-thread")
        self.assertEqual(parsed.last_message, "remote smoke ok")

    def test_unauthorized_sender_does_not_start_codex(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent fix it", sender="ou_other"))

                self.assertEqual(runner.calls, [])
                self.assertIn("没有权限", lark.replies[0][1])

        asyncio.run(run())

    def test_new_session_starts_codex_and_binds_thread(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent title=agent-console fix it"))

                self.assertEqual(runner.calls[0][0], "start")
                self.assertEqual(runner.calls[0][2], "fix it")
                self.assertIn("started work", lark.replies[-1][1])
                self.assertEqual(
                    router.state.get_session("oc_chat", "chat:oc_chat").codex_session_id,
                    "codex-new",
                )
                self.assertEqual(router.state.list_remote_agents()[0].title, "agent-console")

        asyncio.run(run())

    def test_new_session_accepts_positional_repo_and_title(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new agent agent-console fix it"))

                self.assertEqual(runner.calls[0], ("start", "/tmp/agent", "fix it"))
                self.assertEqual(router.state.list_remote_agents()[0].title, "agent-console")

        asyncio.run(run())

    def test_new_session_allows_title_without_instruction(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent title=agent-console"))

                self.assertEqual(runner.calls[0][0], "start")
                self.assertIn("agent-console", runner.calls[0][2])
                self.assertIn("待命", runner.calls[0][2])
                self.assertEqual(router.state.list_remote_agents()[0].title, "agent-console")

        asyncio.run(run())

    def test_new_session_positional_title_without_instruction(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new agent agent-console"))

                self.assertEqual(runner.calls[0][0], "start")
                self.assertIn("agent-console", runner.calls[0][2])
                self.assertEqual(router.state.list_remote_agents()[0].title, "agent-console")

        asyncio.run(run())

    def test_new_session_rejects_unknown_positional_repo(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new typo helper do work"))

                self.assertEqual(runner.calls, [])
                self.assertIn("未知 repo：typo", lark.replies[-1][1])

        asyncio.run(run())

    def test_p2p_status_uses_chat_binding_after_new_message_id_changes(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent title=relay-summary summarize relay", message_id="om_new"))
                await router.handle(message("/status", message_id="om_status"))

                self.assertIn("relay-summary", lark.replies[-1][1])
                self.assertIn("codex-new", lark.replies[-1][1])

        asyncio.run(run())

    def test_plain_message_resumes_existing_session(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent fix it", message_id="om_root"))
                await router.handle(message("continue", message_id="om_followup"))

                self.assertEqual(runner.calls[-1], ("resume", "codex-new", "/tmp/agent", "continue"))
                self.assertIn("continued work", lark.replies[-1][1])
                self.assertNotIn("继续处理当前 Codex session", [reply for _, reply in lark.replies])

        asyncio.run(run())

    def test_send_requires_approval_before_user_identity_send(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/send ou_target hello there"))

                self.assertEqual(lark.sent, [])
                self.assertIn("/approve", lark.replies[-1][1])

                confirmation_id = router.state.list_confirmations()[0].id
                await router.handle(message(f"/approve {confirmation_id}"))

                self.assertEqual(lark.sent, [("ou_target", "hello there")])
                self.assertIn("已发送", lark.replies[-1][1])

        asyncio.run(run())

    def test_recent_codex_lists_local_codex_sessions(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/recent-codex"))

                self.assertEqual(runner.calls, [])
                self.assertIn("019e-existing", lark.replies[-1][1])
                self.assertIn("/tmp/agent", lark.replies[-1][1])

        asyncio.run(run())

    def test_codex_sessions_alias_lists_local_codex_sessions(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/codex-sessions"))

                self.assertEqual(runner.calls, [])
                self.assertIn("019e-existing", lark.replies[-1][1])

        asyncio.run(run())

    def test_attach_binds_existing_codex_session_to_current_thread(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/attach repo=agent 019e-existing"))

                binding = router.state.get_session("oc_chat", "chat:oc_chat")
                self.assertEqual(binding.codex_session_id, "019e-existing")
                self.assertEqual(binding.repo_alias, "agent")
                self.assertEqual(runner.calls, [])
                self.assertIn("已绑定", lark.replies[-1][1])

        asyncio.run(run())

    def test_remote_codex_lists_created_remote_agents(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent review current state"))
                await router.handle(message("/remote-codex"))

                self.assertIn("review current state", lark.replies[-1][1])
                self.assertIn("codex-new", lark.replies[-1][1])
                self.assertIn("*", lark.replies[-1][1])

        asyncio.run(run())

    def test_agents_lists_created_remote_agents(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent review current state"))
                await router.handle(message("/agents"))

                self.assertIn("review current state", lark.replies[-1][1])
                self.assertIn("codex-new", lark.replies[-1][1])
                self.assertIn("*", lark.replies[-1][1])

        asyncio.run(run())

    def test_attach_by_remote_agent_id_switches_binding_and_reports_previous_title(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent first helper"))
                first_agent = router.state.list_remote_agents()[0]
                await router.handle(message("/new repo=agent second helper"))
                await router.handle(message(f"/attach {first_agent.id}"))

                binding = router.state.get_session("oc_chat", "chat:oc_chat")
                self.assertEqual(binding.agent_id, first_agent.id)
                self.assertIn("second helper", lark.replies[-1][1])
                self.assertIn("first helper", lark.replies[-1][1])

        asyncio.run(run())

    def test_remove_deletes_one_or_more_remote_agents_and_bindings(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent first helper"))
                await router.handle(message("/new repo=agent second helper"))
                agents = router.state.list_remote_agents()

                await router.handle(message(f"/remove {agents[0].id} {agents[1].id} rc_missing"))

                self.assertEqual(router.state.list_remote_agents(), [])
                self.assertIsNone(router.state.get_session("oc_chat", "chat:oc_chat"))
                self.assertIn(agents[0].id, lark.replies[-1][1])
                self.assertIn(agents[1].id, lark.replies[-1][1])
                self.assertIn("未找到：rc_missing", lark.replies[-1][1])

        asyncio.run(run())

    def test_remove_without_ids_shows_usage(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/remove"))

                self.assertIn("用法：/remove", lark.replies[-1][1])

        asyncio.run(run())

    def test_rename_updates_remote_agent_title(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent old-title"))
                agent = router.state.list_remote_agents()[0]

                await router.handle(message(f"/rename {agent.id} new-title"))

                self.assertEqual(router.state.get_remote_agent(agent.id).title, "new-title")
                self.assertIn("new-title", lark.replies[-1][1])

        asyncio.run(run())

    def test_detach_alias_close_clears_current_binding(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                await router.handle(message("/detach"))

                self.assertIsNone(router.state.get_session("oc_chat", "chat:oc_chat"))
                self.assertIn("解除", lark.replies[-1][1])

        asyncio.run(run())

    def test_close_alias_still_detaches_current_binding(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                await router.handle(message("/close"))

                self.assertIsNone(router.state.get_session("oc_chat", "chat:oc_chat"))
                self.assertIn("/detach", lark.replies[-1][1])

        asyncio.run(run())

    def test_repos_and_switch_repo_commands(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/repos"))
                self.assertIn("agent", lark.replies[-1][1])

                await router.handle(message("/new repo=agent helper"))
                await router.handle(message("/switch-repo agent"))
                self.assertEqual(router.state.get_session("oc_chat", "chat:oc_chat").repo_alias, "agent")
                self.assertIn("已切换", lark.replies[-1][1])

        asyncio.run(run())

    def test_repo_aliases_to_repos_and_switch_repo(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/repo"))
                self.assertIn("可用 repo", lark.replies[-1][1])

                await router.handle(message("/new repo=agent helper"))
                await router.handle(message("/repo agent"))
                self.assertIn("/switch-repo", lark.replies[-1][1])

        asyncio.run(run())

    def test_runs_lists_persisted_run_history(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                await router.handle(message("continue work"))
                await router.handle(message("/runs"))

                self.assertIn("run_", lark.replies[-1][1])
                self.assertIn("continue work", lark.replies[-1][1])

        asyncio.run(run())

    def test_run_history_persists_after_state_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.sqlite"
            state = StateStore(state_path)
            run = state.create_run(
                agent_id="rc_test",
                chat_id="oc_chat",
                message_id="om_run",
                repo_alias="agent",
                repo_path=Path("/tmp/agent"),
                codex_session_id="codex-new",
                prompt="persist me",
            )
            state.mark_run_running(run.id, 12345)
            state.finish_run(run.id, "succeeded", summary="done")

            reopened = StateStore(state_path)

            self.assertEqual(reopened.get_run(run.id).summary, "done")
            self.assertEqual(reopened.list_runs(agent_id="rc_test")[0].id, run.id)

    def test_cancel_reports_no_running_run_for_idle_agent(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                await router.handle(message("/cancel"))

                self.assertIn("没有运行中的任务", lark.replies[-1][1])

        asyncio.run(run())

    def test_cancel_running_run_marks_it_cancelled(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                agent = router.state.list_remote_agents()[0]
                run = router.state.create_run(
                    agent_id=agent.id,
                    chat_id="oc_chat",
                    message_id="om_run",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    codex_session_id="codex-new",
                    prompt="long task",
                )
                router.state.mark_run_running(run.id, 999999)
                router.state.touch_remote_agent(agent.id, status="running", last_run_id=run.id)

                await router.handle(message("/cancel"))

                self.assertEqual(router.state.get_run(run.id).status, "cancelled")
                self.assertEqual(router.state.get_remote_agent(agent.id).status, "idle")
                self.assertIn(run.id, lark.replies[-1][1])

        asyncio.run(run())

    def test_cancelled_inflight_run_is_not_overwritten_by_late_result(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                agent = router.state.list_remote_agents()[0]
                cancellable = CancellableCodexRunner()
                router.handlers.run_manager.codex_runner = cancellable

                task = asyncio.create_task(router.handle(message("long task", message_id="om_long")))
                await cancellable.started.wait()
                await router.handle(message("/cancel", message_id="om_cancel"))
                cancelled_run_id = router.state.get_remote_agent(agent.id).last_run_id
                self.assertEqual(router.state.get_run(cancelled_run_id).status, "cancelled")

                await task

                self.assertEqual(router.state.get_run(cancelled_run_id).status, "cancelled")
                self.assertEqual(router.state.get_remote_agent(agent.id).status, "idle")
                self.assertFalse(any("finished after cancel" in reply for _, reply in lark.replies))

        asyncio.run(run())

    def test_cancelled_queued_run_is_not_reopened_by_late_on_started(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                agent = router.state.list_remote_agents()[0]
                delayed = DelayedStartCodexRunner()
                router.handlers.run_manager.codex_runner = delayed

                task = asyncio.create_task(router.handle(message("long task", message_id="om_long")))
                await delayed.before_started.wait()
                await router.handle(message("/cancel", message_id="om_cancel"))
                cancelled_run_id = router.state.get_remote_agent(agent.id).last_run_id
                self.assertEqual(router.state.get_run(cancelled_run_id).status, "cancelled")

                await task

                self.assertEqual(router.state.get_run(cancelled_run_id).status, "cancelled")
                self.assertFalse(any("late success" in reply for _, reply in lark.replies))

        asyncio.run(run())

    def test_busy_agent_rejects_followup_but_status_still_responds(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                agent = router.state.list_remote_agents()[0]
                run = router.state.create_run(
                    agent_id=agent.id,
                    chat_id="oc_chat",
                    message_id="om_run",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    codex_session_id="codex-new",
                    prompt="long task",
                )
                router.state.mark_run_running(run.id, 999999)
                router.state.touch_remote_agent(agent.id, status="running", last_run_id=run.id)

                await router.handle(message("do another thing"))
                self.assertIn("还在处理", lark.replies[-1][1])

                await router.handle(message("/status"))
                self.assertIn("running", lark.replies[-1][1])
                self.assertIn(run.id, lark.replies[-1][1])

        asyncio.run(run())

    def test_failed_resume_records_run_and_preserves_error_across_attach(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                agent = router.state.list_remote_agents()[0]
                router.handlers.run_manager.codex_runner = FailingCodexRunner()

                await router.handle(message("trigger failure"))

                updated = router.state.get_remote_agent(agent.id)
                self.assertEqual(updated.status, "failed")
                self.assertIn("boom", updated.last_error)
                self.assertEqual(router.state.get_run(updated.last_run_id).status, "failed")

                await router.handle(message(f"/attach {agent.id}"))

                self.assertIn("boom", router.state.get_remote_agent(agent.id).last_error)
                self.assertEqual(router.state.get_remote_agent(agent.id).status, "failed")
                self.assertIn("Codex 执行异常", lark.replies[-2][1])

        asyncio.run(run())

    def test_concurrent_followups_only_start_one_run_for_same_agent(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                slow_runner = SlowCodexRunner()
                router.handlers.run_manager.codex_runner = slow_runner

                await asyncio.gather(
                    router.handle(message("first", message_id="om_first")),
                    router.handle(message("second", message_id="om_second")),
                )

                self.assertEqual(len(slow_runner.calls), 1)
                self.assertTrue(any("还在处理" in reply for _, reply in lark.replies))

        asyncio.run(run())

    def test_timed_out_resume_is_persisted_on_run_and_agent(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/new repo=agent helper"))
                agent = router.state.list_remote_agents()[0]
                router.handlers.run_manager.codex_runner = TimedOutCodexRunner()

                await router.handle(message("long task"))

                updated = router.state.get_remote_agent(agent.id)
                self.assertEqual(updated.status, "failed")
                self.assertEqual(router.state.get_run(updated.last_run_id).status, "timed_out")
                self.assertIn("timeout", lark.replies[-1][1])

        asyncio.run(run())

    def test_run_prompt_redacts_common_secret_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite")
            run = state.create_run(
                agent_id="rc_test",
                chat_id="oc_chat",
                message_id="om_run",
                repo_alias="agent",
                repo_path=Path("/tmp/agent"),
                codex_session_id="codex-new",
                prompt="use token=abc123456789 secret: my-secret-value sk-abcdefghijklmnop",
            )

            stored = state.get_run(run.id).prompt
            self.assertNotIn("abc123456789", stored)
            self.assertNotIn("my-secret-value", stored)
            self.assertNotIn("sk-abcdefghijklmnop", stored)
            self.assertIn("[REDACTED]", stored)

    def test_pending_and_natural_confirm_single_confirmation(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/send ou_target hello there"))
                await router.handle(message("/pending"))
                self.assertIn("待确认", lark.replies[-1][1])

                await router.handle(message("确认"))

                self.assertEqual(lark.sent, [("ou_target", "hello there")])
                self.assertIn("已发送", lark.replies[-1][1])

        asyncio.run(run())

    def test_help_and_doctor_reply(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/help"))
                self.assertIn("Feishu Agent Remote", lark.replies[-1][1])

                await router.handle(message("/doctor"))
                self.assertIn("Doctor", lark.replies[-1][1])

        asyncio.run(run())

    def test_summarize_resumes_existing_session_with_summary_prompt(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                router, runner, lark = make_router(tmp)
                await router.handle(message("/summarize repo=agent 019e-existing"))

                self.assertEqual(runner.calls[-1][0], "resume")
                self.assertEqual(runner.calls[-1][1], "019e-existing")
                self.assertIn("总结", runner.calls[-1][3])
                self.assertIn("continued work", lark.replies[-1][1])

        asyncio.run(run())


def make_router(tmp):
    config_path = Path(tmp) / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "owner_open_id: ou_owner",
                "default_repo: agent",
                "repos:",
                "  agent: /tmp/agent",
                "authorized_open_ids:",
                "  - ou_owner",
            ]
        )
    )
    config = load_config(config_path)
    state = StateStore(Path(tmp) / "state.sqlite")
    runner = FakeCodexRunner()
    lark = FakeLarkGateway()
    return RemoteRouter(config, state, runner, lark, FakeSessionFinder()), runner, lark


if __name__ == "__main__":
    unittest.main()
