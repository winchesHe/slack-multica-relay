"""验证发送前快照的归属、证据、缺失字段和一次发送边界。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("final_reply", Path(__file__).parents[1] / "multica-skills/multica-final-reply/scripts/final_reply.py")
final = importlib.util.module_from_spec(spec)
spec.loader.exec_module(final)


class FinalReplyTests(unittest.TestCase):
    def setUp(self):
        self.env = {"MULTICA_TASK_ID": "run", "MULTICA_WORKSPACE_ID": "ws", "MULTICA_SERVER_URL": "https://multica.example"}
        self.run = {"id": "run", "issue_id": "issue", "workspace_id": "ws", "started_at": "2026-09-09T00:00:00Z"}
        self.data = {"scope": self.env, "run": self.run, "captured_at": "2026-09-09T00:01:02+00:00", "messages": []}

    def message(self, kind, **fields):
        self.data["messages"].append({"seq": len(self.data["messages"]) + 1, "type": kind, "task_id": "run", "issue_id": "issue", **fields})

    def test_snapshot_selects_current_not_latest_and_omits_private_metadata(self):
        self.run["attribution"] = {"email": "private@example"}
        with patch.object(final, "query", side_effect=[[{"id": "other"}, self.run], []]) as query:
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["run"]["id"], "run")
        self.assertNotIn("attribution", data["run"])
        self.assertIn("run", query.call_args.args[0])

    def test_scope_mismatch_does_not_fetch_messages(self):
        self.run["workspace_id"] = "other"
        with patch.object(final, "query", return_value=[self.run]) as query:
            with self.assertRaises(final.FinalReplyError):
                final.snapshot("issue", self.env)
            self.assertEqual(query.call_count, 1)

    def test_model_comes_from_current_run_agent_config(self):
        self.run.update(agent_id="agent-current", model="old-run-model")
        agent = {"id": "agent-current", "workspace_id": "ws", "model": " gpt-6-astra ", "instructions": "private"}
        with patch.object(final, "query", side_effect=[[self.run], agent, []]) as query:
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["run"]["model"], "gpt-6-astra")
        self.assertEqual(data["run"]["model_source"], "agent_config")
        self.assertNotIn("instructions", data["run"])
        self.assertIn("agent-current", query.call_args_list[1].args[0])
        self.assertIn(":agent_mdi_robot_outline_muted: gpt-6-astra", final.statistics(data))

    def test_unavailable_or_invalid_agent_config_hides_only_model(self):
        self.run.update(agent_id="agent-current", model="do-not-fallback")
        invalid = [final.FinalReplyError("读取失败"), None, [],
                   {"id": "other", "workspace_id": "ws", "model": "gpt-6-astra"},
                   {"id": "agent-current", "workspace_id": "other", "model": "gpt-6-astra"},
                   {"id": "agent-current", "workspace_id": "ws", "model": ""},
                   {"id": "agent-current", "workspace_id": "ws", "model": "<!here>"}]
        for agent in invalid:
            with self.subTest(agent=agent), patch.object(final, "query", side_effect=[[self.run], agent, []]) as query:
                data = final.snapshot("issue", self.env)
                self.assertNotIn("model", data["run"])
                self.assertEqual(query.call_count, 3)
                self.assertIn(":agent_time:", final.statistics(data))

    def test_missing_current_run_never_uses_previous(self):
        with patch.object(final, "query", return_value=[{"id": "other"}]):
            with self.assertRaises(final.FinalReplyError):
                final.snapshot("issue", self.env)

    def test_log_failure_still_has_duration_without_invented_counts(self):
        with patch.object(final, "query", side_effect=[[self.run], final.FinalReplyError("失败")]):
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["messages"], [])
        self.assertEqual(final.statistics(self.data), ":agent_time: 1m 2s")

    def test_skill_names_deduplicate_and_tokens_never_render(self):
        self.run.update(model="gpt-6-astra", usage=[{"input_tokens": 999}])
        for _ in range(2):
            self.message("tool_use", tool="exec_command", input={"command": "/bin/zsh -lc 'rtk proxy cat /skills/slack/SKILL.md'"})
            self.message("tool_result", tool="exec_command", output="---\nname: slack\ndescription: test\n---\n正文")
        stats = final.statistics(self.data)
        self.assertIn("2 tools", stats)
        self.assertIn("1 skills", stats)
        self.assertIn("gpt-6-astra", stats)
        self.assertNotIn("token", stats)

    def test_missing_or_foreign_log_sequence_hides_counts(self):
        self.message("tool_use", tool="exec_command")
        self.data["messages"][0]["task_id"] = "other"
        self.assertNotIn("tools", final.statistics(self.data))
        self.data["messages"][0].update(task_id="run", seq=2)
        self.assertNotIn("tools", final.statistics(self.data))

    def test_concurrent_skill_reads_are_not_guessed(self):
        for _ in range(2):
            self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/slack/SKILL.md"})
        for _ in range(2):
            self.message("tool_result", tool="exec_command", output="---\nname: slack\n---\n")
        self.assertNotIn("skills", final.statistics(self.data))

    def test_batch_skill_reads_require_all_headers(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/a/SKILL.md /skills/b/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="---\nname: a\n---\n正文\n---\nname: b\n---\n正文")
        self.assertIn("2 skills", final.statistics(self.data))
        self.data["messages"][-1]["output"] = "---\nname: a\n---\n部分输出"
        self.assertNotIn("skills", final.statistics(self.data))

    def test_pr_evidence_then_github_readback_with_deduplication(self):
        url = "https://github.com/owner/repo/pull/12"
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo --json headRefName"})
        with patch.object(final, "query", return_value={"url": url, "number": 12, "headRefName": "feature/a"}) as query:
            rows = final.artifacts(self.data, [{"url": url, "evidence_seq": 1}] * 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["branch"], "feature/a")
        self.assertEqual(query.call_count, 1)

    def test_document_url_cannot_be_a_pr_artifact(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/github/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="示例 https://github.com/owner/repo/pull/12")
        with patch.object(final, "query") as query:
            with self.assertRaises(final.FinalReplyError):
                final.artifacts(self.data, [{"url": "https://github.com/owner/repo/pull/12", "evidence_seq": 2}])
            query.assert_not_called()

    def test_multi_repository_prs_keep_their_verified_branches(self):
        selection = []
        responses = []
        for repo in ("owner/api", "owner/web"):
            url = f"https://github.com/{repo}/pull/12"
            self.message("tool_use", tool="exec_command", input={"cmd": f"gh pr view 12 --repo {repo}"})
            selection.append({"url": url, "evidence_seq": len(self.data["messages"])})
            responses.append({"url": url, "number": 12, "headRefName": repo.split("/")[1]})
        with patch.object(final, "query", side_effect=responses):
            rows = final.artifacts(self.data, selection)
        self.assertEqual([r["branch"] for r in rows], ["api", "web"])

    def test_github_mismatch_stops_before_preparing_message(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo"})
        with patch.object(final, "query", return_value={"url": "https://github.com/owner/repo/pull/13", "number": 13, "headRefName": "main"}):
            with self.assertRaises(final.FinalReplyError):
                final.artifacts(self.data, [{"url": "https://github.com/owner/repo/pull/12", "evidence_seq": 1}])

    def test_pr_number_prefix_does_not_match_other_pr(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr create --title title"})
        self.message("tool_result", tool="exec_command", output="https://github.com/owner/repo/pull/123")
        with self.assertRaises(final.FinalReplyError):
            final.artifacts(self.data, [{"url": "https://github.com/owner/repo/pull/12", "evidence_seq": 2}])

    def test_prepare_preserves_body_and_binds_content_to_current_run(self):
        with tempfile.TemporaryDirectory() as root:
            result = final.prepare(self.data, [], "结论", [{"type": "section", "text": {"type": "plain_text", "text": "结论"}}], Path(root) / "bundle")
            path = Path(result["bundle"])
            blocks = final.read(path.parent / "blocks.json")
            self.assertEqual(blocks[0]["text"]["text"], "结论")
            self.assertEqual(blocks[1]["type"], "context")
            (path.parent / "text.txt").write_text("篡改")
            with self.assertRaisesRegex(final.FinalReplyError, "内容已变化"):
                final.send(path, "C1", "100.000001", False, self.env)
            with self.assertRaisesRegex(final.FinalReplyError, "不属于当前运行"):
                final.send(path, "C1", "100.000001", False, {**self.env, "MULTICA_TASK_ID": "other"})

    def test_bundle_uses_packaged_sender_and_repeated_send_is_noop(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cli = root / "slack.py"
            cli.touch()
            identity = {**self.env, "MULTICA_TASK_ID": "55555555-5555-5555-5555-555555555555",
                        "MULTICA_WORKSPACE_ID": "11111111-1111-1111-1111-111111111111"}
            self.data["scope"] = identity
            self.run["issue_id"] = "44444444-4444-4444-4444-444444444444"
            prepared = final.prepare(self.data, [], "正文", [{"type": "section", "text": {"type": "plain_text", "text": "正文"}}], root / "bundle")
            env = {**identity, "RELAY_SLACK_CLI": str(cli), "SLACK_REPLY_ACTOR": "user",
                   "SLACK_TEAM_ID": "T1", "RELAY_RECEIPT_DIR": str(root / "receipts")}
            preview = {"status": "preview", "preview_digest": "digest", "actor": {"selected": "user", "team_id": "T1"}}
            sent = {"status": "sent", "operation_status": "succeeded", "actor": {"selected": "user"},
                    "message": {"channel_id": "C1", "thread_ts": "100.000001", "ts": "101.000001"}}
            responses = [subprocess.CompletedProcess([], 0, json.dumps(value)) for value in (preview, sent)]
            with patch.object(final.subprocess, "run", side_effect=responses) as run:
                self.assertEqual(final.send(prepared["bundle"], "C1", "100.000001", False, env)["action"], "sent")
                self.assertEqual(final.send(prepared["bundle"], "C1", "100.000001", False, env)["action"], "duplicate")
                self.assertEqual(run.call_count, 2)
                self.assertIn("--blocks-file", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
