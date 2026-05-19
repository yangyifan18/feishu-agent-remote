import json
import unittest

from remote_control.claude_runner import parse_claude_event_line, parse_claude_stream_json
from remote_control.codex_runner import parse_codex_event_line, parse_codex_jsonl


class RuntimeStreamingTests(unittest.TestCase):
    def test_parse_codex_session_meta_event(self):
        event = parse_codex_event_line(json.dumps({"type": "session_meta", "payload": {"id": "019e-session"}}))

        self.assertEqual(event.type, "session")
        self.assertEqual(event.session_id, "019e-session")

    def test_parse_codex_thread_started_event(self):
        event = parse_codex_event_line(json.dumps({"type": "thread.started", "thread_id": "019e-thread"}))

        self.assertEqual(event.type, "session")
        self.assertEqual(event.session_id, "019e-thread")

    def test_parse_codex_assistant_payload_event(self):
        event = parse_codex_event_line(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                }
            )
        )

        self.assertEqual(event.type, "assistant")
        self.assertEqual(event.text, "hello")

    def test_parse_codex_item_completed_agent_message(self):
        event = parse_codex_event_line(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
        )

        self.assertEqual(event.type, "assistant")
        self.assertEqual(event.text, "done")

    def test_parse_codex_invalid_or_unknown_event_returns_none(self):
        self.assertIsNone(parse_codex_event_line("not-json"))
        self.assertIsNone(parse_codex_event_line(json.dumps({"type": "unknown"})))

    def test_codex_final_parser_still_uses_last_message(self):
        raw = "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "019e-session"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}),
            ]
        )

        parsed = parse_codex_jsonl(raw)

        self.assertEqual(parsed.session_id, "019e-session")
        self.assertEqual(parsed.last_message, "final")

    def test_parse_claude_assistant_event(self):
        event = parse_claude_event_line(
            json.dumps(
                {
                    "type": "assistant",
                    "session_id": "claude-session",
                    "message": {"content": [{"type": "text", "text": "working"}]},
                }
            )
        )

        self.assertEqual(event.type, "assistant")
        self.assertEqual(event.session_id, "claude-session")
        self.assertEqual(event.text, "working")

    def test_parse_claude_result_event_as_assistant_progress(self):
        event = parse_claude_event_line(json.dumps({"type": "result", "session_id": "claude-session", "result": "done"}))

        self.assertEqual(event.type, "assistant")
        self.assertEqual(event.session_id, "claude-session")
        self.assertEqual(event.text, "done")

    def test_parse_claude_session_only_event(self):
        event = parse_claude_event_line(json.dumps({"type": "system", "session_id": "claude-session"}))

        self.assertEqual(event.type, "session")
        self.assertEqual(event.session_id, "claude-session")

    def test_parse_claude_invalid_or_unknown_event_returns_none(self):
        self.assertIsNone(parse_claude_event_line("not-json"))
        self.assertIsNone(parse_claude_event_line(json.dumps({"type": "system"})))

    def test_claude_final_parser_still_uses_result(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "session_id": "claude-session",
                        "message": {"content": [{"type": "text", "text": "working"}]},
                    }
                ),
                json.dumps({"type": "result", "session_id": "claude-session", "result": "final"}),
            ]
        )

        parsed = parse_claude_stream_json(raw)

        self.assertEqual(parsed.session_id, "claude-session")
        self.assertEqual(parsed.last_message, "final")


if __name__ == "__main__":
    unittest.main()
