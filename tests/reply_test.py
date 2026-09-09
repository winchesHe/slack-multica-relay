"""验证最终回复发送的身份、线程目标和持久化去重。"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("relay_reply", Path(__file__).parents[1] / "multica-skills/multica-final-reply/scripts/slack_sender.py")
reply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reply)


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "slack.py"
        self.cli.touch()
        self.text = self.root / "text.md"
        self.text.write_text("完整正文", encoding="utf-8")
        self.args = argparse.Namespace(issue="44444444-4444-4444-4444-444444444444", channel="C1", thread_ts="100.000001",
            text_file=str(self.text), blocks_file=None, format="markdown", dry_run=False)
        self.env = {"MULTICA_TASK_ID": "55555555-5555-5555-5555-555555555555",
            "MULTICA_WORKSPACE_ID": "11111111-1111-1111-1111-111111111111",
            "RELAY_SLACK_CLI": str(self.cli), "SLACK_REPLY_ACTOR": "user", "SLACK_TEAM_ID": "T1",
            "RELAY_RECEIPT_DIR": str(self.root / "receipts")}
        self.preview = {"status": "preview", "preview_digest": "sha256:preview", "actor": {"selected": "user", "team_id": "T1"}}
        self.sent = {"status": "sent", "operation_status": "succeeded", "actor": {"selected": "user"},
            "message": {"channel_id": "C1", "thread_ts": "100.000001", "ts": "101.000001"}}

    def test_send_and_repeat(self):
        with patch.object(reply, "invoke", side_effect=[self.preview, self.sent]) as invoke:
            self.assertEqual(reply.execute(self.args, self.env)["action"], "sent")
            self.assertEqual(reply.execute(self.args, self.env)["action"], "duplicate")
            self.assertEqual(invoke.call_count, 2)
            self.assertIn("--confirm-preview", invoke.call_args.args[1])
        receipt = next((self.root / "receipts").glob("*.json")).read_text()
        self.assertNotIn("完整正文", receipt)

    def test_ambiguous_send_is_not_replayed(self):
        with patch.object(reply, "invoke", side_effect=[self.preview, reply.ReplyError("发送超时")]) as invoke:
            with self.assertRaises(reply.ReplyError):
                reply.execute(self.args, self.env)
            with self.assertRaisesRegex(reply.ReplyError, "上次发送结果不明"):
                reply.execute(self.args, self.env)
            self.assertEqual(invoke.call_count, 2)

    def test_changed_final_reply_does_not_send_again(self):
        with patch.object(reply, "invoke", side_effect=[self.preview, self.sent]) as invoke:
            reply.execute(self.args, self.env)
            self.text.write_text("另一份正文")
            with self.assertRaisesRegex(reply.ReplyError, "不同的最终回复"):
                reply.execute(self.args, self.env)
            self.assertEqual(invoke.call_count, 2)

    def test_dry_run_never_sends(self):
        self.args.dry_run = True
        with patch.object(reply, "invoke", return_value=self.preview) as invoke:
            self.assertEqual(reply.execute(self.args, self.env)["action"], "preview")
            self.assertEqual(invoke.call_count, 1)
            self.assertIn("--dry-run", invoke.call_args.args[1])
            self.assertEqual(list((self.root / "receipts").glob("*.json")), [])

    def test_missing_configuration_does_not_send(self):
        self.env.pop("SLACK_TEAM_ID")
        with patch.object(reply, "invoke") as invoke:
            with self.assertRaisesRegex(reply.ReplyError, "缺少"):
                reply.execute(self.args, self.env)
            invoke.assert_not_called()

    def test_wrong_actor_stops_before_send(self):
        self.preview["actor"]["selected"] = "bot"
        with patch.object(reply, "invoke", return_value=self.preview) as invoke:
            with self.assertRaisesRegex(reply.ReplyError, "预检身份"):
                reply.execute(self.args, self.env)
            self.assertEqual(invoke.call_count, 1)

    def test_wrong_workspace_stops_before_send(self):
        self.preview["actor"]["team_id"] = "T2"
        with patch.object(reply, "invoke", return_value=self.preview) as invoke:
            with self.assertRaisesRegex(reply.ReplyError, "预检身份"):
                reply.execute(self.args, self.env)
            self.assertEqual(invoke.call_count, 1)

    def test_wrong_message_destination_is_not_resent(self):
        self.sent["message"]["thread_ts"] = "wrong"
        with patch.object(reply, "invoke", side_effect=[self.preview, self.sent]) as invoke:
            with self.assertRaisesRegex(reply.ReplyError, "目标不匹配"):
                reply.execute(self.args, self.env)
            with self.assertRaisesRegex(reply.ReplyError, "结果不明"):
                reply.execute(self.args, self.env)
            self.assertEqual(invoke.call_count, 2)


if __name__ == "__main__":
    unittest.main()
