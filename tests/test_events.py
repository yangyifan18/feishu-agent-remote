import asyncio
import importlib
import logging
import os
import tempfile
import unittest
from pathlib import Path

from remote_control.events import EventConsumerBackoff, ProcessedEventCache, consume_events_forever


class FakeStream:
    def __init__(self, lines):
        self.lines = list(lines)

    def __aiter__(self):
        self._iter = iter(self.lines)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeProcess:
    def __init__(self, lines=(), returncode=0):
        self.pid = 123
        self.stdout = FakeStream(lines)
        self.stderr = FakeStream(())
        self.returncode = None
        self._final_returncode = returncode
        self.terminated = False
        self.killed = False

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class EventReliabilityTests(unittest.TestCase):
    def test_processed_event_cache_prunes_lru_without_clearing_recent_keys(self):
        cache = ProcessedEventCache(max_size=3, ttl_seconds=100)

        self.assertFalse(cache.seen_or_add("old", now=1))
        self.assertFalse(cache.seen_or_add("middle", now=2))
        self.assertFalse(cache.seen_or_add("recent", now=3))
        self.assertTrue(cache.seen_or_add("recent", now=4))
        self.assertFalse(cache.seen_or_add("new", now=5))

        self.assertFalse(cache.seen_or_add("old", now=6))
        self.assertTrue(cache.seen_or_add("recent", now=7))

    def test_processed_event_cache_allows_key_after_ttl(self):
        cache = ProcessedEventCache(max_size=3, ttl_seconds=10)

        self.assertFalse(cache.seen_or_add("event", now=1))
        self.assertTrue(cache.seen_or_add("event", now=5))
        self.assertFalse(cache.seen_or_add("event", now=20))

    def test_event_consumer_backoff_grows_and_resets_after_stable_run(self):
        backoff = EventConsumerBackoff(initial_delay=5, max_delay=60, stable_after=60)

        self.assertEqual([backoff.delay_after(0) for _ in range(5)], [5, 10, 20, 40, 60])
        self.assertEqual(backoff.delay_after(0), 60)
        self.assertEqual(backoff.delay_after(61), 5)
        self.assertEqual(backoff.delay_after(0), 10)

    def test_event_consumer_ignores_non_json_and_dispatches_json(self):
        async def run():
            handled = []

            async def process_factory(*args, **kwargs):
                return FakeProcess([b"not-json\n", b"{\"event_id\":\"e1\"}\n"])

            async def handle(event):
                handled.append(event)

            await consume_events_forever(
                "lark-cli",
                handle,
                logging.getLogger("test"),
                process_factory=process_factory,
                sleeper=lambda _: asyncio.sleep(0),
                max_cycles=1,
            )
            await asyncio.sleep(0)

            self.assertEqual(handled, [{"event_id": "e1"}])

        asyncio.run(run())

    def test_event_consumer_uses_exponential_restart_delays(self):
        async def run():
            sleeps = []
            processes = [FakeProcess(), FakeProcess(), FakeProcess()]

            async def process_factory(*args, **kwargs):
                return processes.pop(0)

            async def sleeper(delay):
                sleeps.append(delay)

            await consume_events_forever(
                "lark-cli",
                lambda event: asyncio.sleep(0),
                logging.getLogger("test"),
                process_factory=process_factory,
                sleeper=sleeper,
                clock=lambda: 0.0,
                max_cycles=3,
            )

            self.assertEqual(sleeps, [5.0, 10.0])

        asyncio.run(run())

    def test_handle_event_dedupes_same_event_id(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.yaml"
                state_path = Path(tmp) / "state.sqlite"
                config_path.write_text(
                    "\n".join(
                        [
                            "owner_open_id: ou_owner",
                            "default_repo: agent",
                            "repos:",
                            "  agent: /tmp/agent",
                            "bot_names:",
                            "  - yyf-codex",
                        ]
                    )
                )
                old_env = {key: os.environ.get(key) for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FAR_CONFIG", "FAR_STATE")}
                os.environ["FEISHU_APP_ID"] = "test-app"
                os.environ["FEISHU_APP_SECRET"] = "test-secret"
                os.environ["FAR_CONFIG"] = str(config_path)
                os.environ["FAR_STATE"] = str(state_path)
                try:
                    main = importlib.import_module("main")
                    main.processed_events = ProcessedEventCache(max_size=10, ttl_seconds=100)
                    calls = []

                    class FakeRouter:
                        async def handle(self, msg):
                            calls.append(msg.message_id)

                    main.router = FakeRouter()
                    event = {
                        "event_id": "evt_same",
                        "message_id": "om_msg",
                        "chat_id": "oc_chat",
                        "chat_type": "p2p",
                        "sender_id": "ou_owner",
                        "content": "/status",
                    }

                    await main.handle_event(event)
                    await main.handle_event(event)

                    self.assertEqual(calls, ["om_msg"])
                finally:
                    for key, value in old_env.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value

        asyncio.run(run())
