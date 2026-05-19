import asyncio
import tempfile
import unittest
from pathlib import Path

from remote_control.config import load_config, load_workspace_configs
from remote_control.models import IncomingMessage
from remote_control.router import RemoteRouter
from remote_control.state import StateStore
from remote_control.workspaces import WorkspaceManager, WorkspaceRuntime


class FakeRunner:
    def __init__(self, session_prefix):
        self.session_prefix = session_prefix
        self.calls = []

    async def start(self, repo_path, prompt, on_started=None):
        self.calls.append(("start", str(repo_path), prompt))
        if on_started:
            on_started(123)
        return {"session_id": f"{self.session_prefix}-{len(self.calls)}", "summary": f"{self.session_prefix} done", "status": "succeeded"}

    async def resume(self, session_id, repo_path, prompt, on_started=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt))
        if on_started:
            on_started(123)
        return {"session_id": session_id, "summary": f"{self.session_prefix} resumed", "status": "succeeded"}


class FakeLarkGateway:
    def __init__(self):
        self.replies = []
        self.sent = []

    async def reply(self, message_id, text):
        self.replies.append((message_id, text))

    async def send_user_message(self, user_id, text):
        self.sent.append((user_id, text))


def event_message(text, sender, message_id="om", chat="oc_same"):
    return {
        "message_id": message_id,
        "chat_id": chat,
        "chat_type": "p2p",
        "sender_id": sender,
        "content": text,
    }


def incoming_from_event(event, workspace_id):
    return IncomingMessage(
        message_id=event["message_id"],
        chat_id=event["chat_id"],
        chat_type=event["chat_type"],
        sender_id=event["sender_id"],
        content=event["content"],
        workspace_id=workspace_id,
    )


class WorkspaceTests(unittest.TestCase):
    def test_old_config_maps_to_default_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "owner_open_id: ou_owner",
                        "default_repo: agent",
                        "repos:",
                        "  agent: /tmp/agent",
                    ]
                )
            )

            config = load_config(config_path)
            configs = load_workspace_configs(config_path)

            self.assertEqual(config.workspace_id, "default")
            self.assertEqual(config.default_workspace, "default")
            self.assertEqual(list(configs), ["default"])

    def test_workspace_config_parses_cli_args_features_and_shared_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "default_workspace: personal",
                        "shared_repos:",
                        "  agent: /tmp/agent",
                        "features:",
                        "  progress_replies: true",
                        "workspaces:",
                        "  personal:",
                        "    owner_open_id: ou_personal",
                        "    default_repo: agent",
                        "    lark_cli_args:",
                        "      - --profile",
                        "      - personal",
                        "    features:",
                        "      card_replies: true",
                        "      card_update_min_interval_seconds: 1",
                        "  team:",
                        "    owner_open_id: ou_team",
                        "    default_repo: agent",
                        "    lark_cli_args:",
                        "      - --profile",
                        "      - team",
                    ]
                )
            )

            configs = load_workspace_configs(config_path)

            self.assertEqual(set(configs), {"personal", "team"})
            self.assertEqual(configs["personal"].workspace_id, "personal")
            self.assertEqual(configs["personal"].lark_cli_args, ("--profile", "personal"))
            self.assertTrue(configs["personal"].features.progress_replies)
            self.assertTrue(configs["personal"].features.card_replies)
            self.assertEqual(configs["team"].repos["agent"].path, Path("/tmp/agent"))
            self.assertFalse(configs["team"].features.card_replies)

    def test_state_isolates_sessions_agents_runs_and_confirmations_by_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite")
            agent_a = state.create_remote_agent("personal", "agent", Path("/tmp/agent"), "sess-a", "oc", "chat:oc", workspace_id="personal")
            agent_b = state.create_remote_agent("team", "agent", Path("/tmp/agent"), "sess-b", "oc", "chat:oc", workspace_id="team")
            state.upsert_session("oc", "chat:oc", "agent", Path("/tmp/agent"), "sess-a", agent_id=agent_a.id, workspace_id="personal")
            state.upsert_session("oc", "chat:oc", "agent", Path("/tmp/agent"), "sess-b", agent_id=agent_b.id, workspace_id="team")
            run_a = state.create_run(agent_id=agent_a.id, chat_id="oc", message_id="om_a", repo_alias="agent", repo_path=Path("/tmp/agent"), codex_session_id="sess-a", prompt="a", workspace_id="personal")
            run_b = state.create_run(agent_id=agent_b.id, chat_id="oc", message_id="om_b", repo_alias="agent", repo_path=Path("/tmp/agent"), codex_session_id="sess-b", prompt="b", workspace_id="team")
            confirm_a = state.create_confirmation("send", "ou_a", "oc", "om_a", {"text": "a"}, workspace_id="personal")
            confirm_b = state.create_confirmation("send", "ou_b", "oc", "om_b", {"text": "b"}, workspace_id="team")

            self.assertEqual(state.get_session("oc", "chat:oc", "personal").agent_id, agent_a.id)
            self.assertEqual(state.get_session("oc", "chat:oc", "team").agent_id, agent_b.id)
            self.assertEqual([agent.id for agent in state.list_remote_agents(workspace_id="personal")], [agent_a.id])
            self.assertEqual([run.id for run in state.list_runs(workspace_id="team")], [run_b.id])
            self.assertEqual([item.id for item in state.list_confirmations(workspace_id="personal")], [confirm_a.id])
            self.assertIsNone(state.get_confirmation(confirm_a.id, workspace_id="team"))

    def test_workspace_manager_routes_same_chat_to_isolated_routers(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                configs = write_workspace_config(tmp)
                state = StateStore(Path(tmp) / "state.sqlite")
                personal_lark = FakeLarkGateway()
                team_lark = FakeLarkGateway()
                personal_runner = FakeRunner("personal")
                team_runner = FakeRunner("team")
                manager = WorkspaceManager(
                    {
                        "personal": WorkspaceRuntime(
                            "personal",
                            configs["personal"],
                            state,
                            personal_lark,
                            RemoteRouter(configs["personal"], state, personal_runner, personal_lark),
                        ),
                        "team": WorkspaceRuntime(
                            "team",
                            configs["team"],
                            state,
                            team_lark,
                            RemoteRouter(configs["team"], state, team_runner, team_lark),
                        ),
                    },
                    default_workspace="personal",
                )

                await manager.handle_event("personal", event_message("/new agent personal-agent work", "ou_personal", "om_p"), incoming_from_event)
                await manager.handle_event("team", event_message("/new agent team-agent work", "ou_team", "om_t"), incoming_from_event)
                await manager.handle_event("personal", event_message("/status", "ou_personal", "om_ps"), incoming_from_event)
                await manager.handle_event("team", event_message("/status", "ou_team", "om_ts"), incoming_from_event)

                self.assertIn("personal-agent", personal_lark.replies[-1][1])
                self.assertIn("team-agent", team_lark.replies[-1][1])
                self.assertNotIn("team-agent", personal_lark.replies[-1][1])
                self.assertNotIn("personal-agent", team_lark.replies[-1][1])

        asyncio.run(run())

    def test_workspace_auth_and_confirmation_are_scoped(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                configs = write_workspace_config(tmp)
                state = StateStore(Path(tmp) / "state.sqlite")
                personal_lark = FakeLarkGateway()
                team_lark = FakeLarkGateway()
                manager = WorkspaceManager(
                    {
                        "personal": WorkspaceRuntime("personal", configs["personal"], state, personal_lark, RemoteRouter(configs["personal"], state, FakeRunner("personal"), personal_lark)),
                        "team": WorkspaceRuntime("team", configs["team"], state, team_lark, RemoteRouter(configs["team"], state, FakeRunner("team"), team_lark)),
                    },
                    default_workspace="personal",
                )

                await manager.handle_event("personal", event_message("/send ou_target hello", "ou_personal", "om_send"), incoming_from_event)
                confirmation_id = state.list_confirmations(workspace_id="personal")[0].id
                await manager.handle_event("team", event_message(f"/approve {confirmation_id}", "ou_team", "om_approve"), incoming_from_event)
                await manager.handle_event("team", event_message("/status", "ou_personal", "om_bad"), incoming_from_event)

                self.assertEqual(team_lark.sent, [])
                self.assertIn("不存在", team_lark.replies[0][1])
                self.assertIn("没有权限", team_lark.replies[-1][1])

        asyncio.run(run())


def write_workspace_config(tmp):
    config_path = Path(tmp) / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "default_workspace: personal",
                "shared_repos:",
                "  agent: /tmp/agent",
                "workspaces:",
                "  personal:",
                "    owner_open_id: ou_personal",
                "    authorized_open_ids:",
                "      - ou_personal",
                "    default_repo: agent",
                "  team:",
                "    owner_open_id: ou_team",
                "    authorized_open_ids:",
                "      - ou_team",
                "    default_repo: agent",
            ]
        )
    )
    return load_workspace_configs(config_path)


if __name__ == "__main__":
    unittest.main()
