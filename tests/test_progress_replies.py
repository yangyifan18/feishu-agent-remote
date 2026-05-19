import asyncio
import tempfile
import unittest
from pathlib import Path

from remote_control.progress import CardProgressReporter, RunProgress, TextProgressReporter
from remote_control.replies import MessageRef, ReplyHandle
from remote_control.run_manager import RunManager
from remote_control.state import StateStore
from remote_control.lark_gateway import LarkGateway, _build_progress_card


class FakeReplyGateway:
    def __init__(self, fail_create=False, fail_update=False):
        self.fail_create = fail_create
        self.fail_update = fail_update
        self.texts = []
        self.card_creates = []
        self.card_updates = []

    async def reply_text(self, ref, text):
        self.texts.append((ref, text))
        return ReplyHandle(mode="text", workspace_id=ref.workspace_id, message_id=ref.message_id)

    async def create_progress_card(self, ref, progress):
        if self.fail_create:
            raise RuntimeError("card create failed")
        self.card_creates.append((ref, progress))
        return ReplyHandle(mode="card", workspace_id=ref.workspace_id, message_id="om_card", card_id="om_card")

    async def update_progress_card(self, handle, progress):
        if self.fail_update:
            raise RuntimeError("card update failed")
        self.card_updates.append((handle, progress))


class FakeProgressReporter:
    def __init__(self):
        self.events = []

    async def update(self, progress):
        self.events.append(progress)


class FailingFinalProgressReporter(FakeProgressReporter):
    async def update(self, progress):
        self.events.append(progress)
        if progress.status == "succeeded":
            raise RuntimeError("reply down")


class EventingRunner:
    def __init__(self):
        self.calls = []

    async def start(self, repo_path, prompt, on_started=None, on_event=None):
        self.calls.append(("start", str(repo_path), prompt, on_event is not None))
        if on_started:
            on_started(123)
        if on_event:
            result = on_event(type("Event", (), {"type": "assistant", "text": "stream chunk"})())
            if result is not None:
                await result
        return {"session_id": "sess-new", "summary": "final answer", "status": "succeeded"}

    async def resume(self, session_id, repo_path, prompt, on_started=None, on_event=None):
        self.calls.append(("resume", session_id, str(repo_path), prompt, on_event is not None))
        if on_started:
            on_started(123)
        if on_event:
            result = on_event(type("Event", (), {"type": "assistant", "text": "resume stream"})())
            if result is not None:
                await result
        return {"session_id": session_id, "summary": "resume final", "status": "succeeded"}


class FakeLarkCardGateway(LarkGateway):
    def __init__(self, output):
        super().__init__()
        self.output = output
        self.argv = []

    async def _run_json(self, argv):
        self.argv.append(argv)
        return self.output


class ProgressReplyTests(unittest.TestCase):
    def test_text_reporter_sends_started_and_finished(self):
        async def run():
            gateway = FakeReplyGateway()
            ref = MessageRef(workspace_id="default", chat_id="oc", message_id="om", thread_key="chat:oc")
            reporter = TextProgressReporter(gateway, ref)

            await reporter.update(progress(status="running", text="started"))
            await reporter.update(progress(status="succeeded", text="done"))

            self.assertEqual(len(gateway.texts), 2)
            self.assertIn("started", gateway.texts[0][1])
            self.assertIn("done", gateway.texts[1][1])

        asyncio.run(run())

    def test_card_create_failure_falls_back_to_text_and_final_text(self):
        async def run():
            gateway = FakeReplyGateway(fail_create=True)
            ref = MessageRef(workspace_id="default", chat_id="oc", message_id="om", thread_key="chat:oc")
            reporter = CardProgressReporter(gateway, ref, min_interval_seconds=0)

            await reporter.update(progress(status="running", text="starting"))
            await reporter.update(progress(status="succeeded", text="done"))

            self.assertEqual(gateway.card_creates, [])
            self.assertEqual(gateway.card_updates, [])
            self.assertEqual(len(gateway.texts), 2)
            self.assertIn("done", gateway.texts[-1][1])

        asyncio.run(run())

    def test_card_debounce_suppresses_intermediate_but_sends_final(self):
        async def run():
            gateway = FakeReplyGateway()
            ref = MessageRef(workspace_id="default", chat_id="oc", message_id="om", thread_key="chat:oc")
            reporter = CardProgressReporter(gateway, ref, min_interval_seconds=999)

            await reporter.update(progress(status="running", text="one"))
            await reporter.update(progress(status="running", text="two"))
            await reporter.update(progress(status="succeeded", text="done"))

            self.assertEqual(len(gateway.card_creates), 1)
            self.assertEqual(len(gateway.card_updates), 1)
            self.assertEqual(gateway.card_updates[0][1].status, "succeeded")

        asyncio.run(run())

    def test_card_handle_is_persisted(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                state = StateStore(Path(tmp) / "state.sqlite")
                run_record = state.create_run(
                    agent_id=None,
                    chat_id="oc",
                    message_id="om",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    codex_session_id=None,
                    prompt="work",
                )
                gateway = FakeReplyGateway()
                ref = MessageRef(workspace_id="default", chat_id="oc", message_id="om", thread_key="chat:oc")
                reporter = CardProgressReporter(gateway, ref, state=state, min_interval_seconds=0)

                await reporter.update(progress(run_id=run_record.id, status="running", text="start"))

                handle = state.get_run_reply(run_record.id)
                self.assertIsNotNone(handle)
                self.assertEqual(handle.mode, "card")
                self.assertEqual(handle.card_id, "om_card")

        asyncio.run(run())

    def test_lark_gateway_card_create_requires_returned_card_or_message_id(self):
        async def run():
            ref = MessageRef(workspace_id="default", chat_id="oc", message_id="om_original", thread_key="chat:oc")
            gateway = FakeLarkCardGateway({"data": {"message_id": "om_card"}})

            handle = await gateway.create_progress_card(ref, progress(status="running", text="start"))

            self.assertEqual(handle.mode, "card")
            self.assertEqual(handle.message_id, "om_card")
            self.assertEqual(handle.card_id, "om_card")

            with self.assertRaises(RuntimeError):
                await FakeLarkCardGateway({}).create_progress_card(ref, progress(status="running", text="start"))

        asyncio.run(run())

    def test_lark_gateway_card_update_targets_message_id_not_card_id(self):
        async def run():
            gateway = FakeLarkCardGateway({})
            handle = ReplyHandle(mode="card", workspace_id="default", message_id="om_message", card_id="card_distinct")

            await gateway.update_progress_card(handle, progress(status="running", text="update"))

            self.assertIn("/open-apis/im/v1/messages/om_message", gateway.argv[0])
            self.assertNotIn("/open-apis/im/v1/messages/card_distinct", gateway.argv[0])

        asyncio.run(run())

    def test_progress_card_redacts_secret_like_text(self):
        card = _build_progress_card(progress(text="token=abc123456789 bearer abcdefghijklmnop sk-abcdefghijklmnop"))

        rendered = str(card)
        self.assertTrue(card["config"]["update_multi"])
        self.assertNotIn("abc123456789", rendered)
        self.assertNotIn("abcdefghijklmnop", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_run_manager_emits_lifecycle_progress(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                state = StateStore(Path(tmp) / "state.sqlite")
                runner = EventingRunner()
                manager = RunManager(state, runner)
                reporter = FakeProgressReporter()

                run_record, result = await manager.start_new(
                    chat_id="oc",
                    thread_key="chat:oc",
                    message_id="om",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    prompt="do work",
                    progress_reporter=reporter,
                    runtime_streaming=True,
                    title="agent-title",
                )

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(run_record.workspace_id, "default")
                self.assertEqual([event.status for event in reporter.events], ["queued", "running", "assistant", "succeeded"])
                self.assertIn("stream chunk", reporter.events[2].text)

        asyncio.run(run())

    def test_run_manager_does_not_pass_on_event_when_streaming_disabled(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                state = StateStore(Path(tmp) / "state.sqlite")
                runner = EventingRunner()
                manager = RunManager(state, runner)

                await manager.start_new(
                    chat_id="oc",
                    thread_key="chat:oc",
                    message_id="om",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    prompt="do work",
                    runtime_streaming=False,
                )

                self.assertEqual(runner.calls[0], ("start", "/tmp/agent", "do work", False))

        asyncio.run(run())

    def test_progress_reporter_failure_does_not_wedge_start_new(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                state = StateStore(Path(tmp) / "state.sqlite")
                manager = RunManager(state, EventingRunner())
                reporter = FailingFinalProgressReporter()

                run_record, result = await manager.start_new(
                    chat_id="oc",
                    thread_key="chat:oc",
                    message_id="om",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    prompt="do work",
                    progress_reporter=reporter,
                )
                manager.release_binding("oc", "chat:oc", run_record.id)
                retry, _ = await manager.start_new(
                    chat_id="oc",
                    thread_key="chat:oc",
                    message_id="om_retry",
                    repo_alias="agent",
                    repo_path=Path("/tmp/agent"),
                    prompt="retry",
                )

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(state.get_run(run_record.id).status, "succeeded")
                self.assertEqual(state.get_run(retry.id).status, "succeeded")

        asyncio.run(run())


def progress(run_id="run_1", status="running", text="work"):
    return RunProgress(
        run_id=run_id,
        agent_id="rc_1",
        title="agent",
        repo_alias="agent",
        runtime="codex",
        status=status,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
