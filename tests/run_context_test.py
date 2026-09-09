"""验证运行资料整理的归属、统计和代码证据。"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("run_context", Path(__file__).parents[1] / "multica-skills/multica-final-reply/scripts/run_context.py")
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
            with self.assertRaises(final.RunContextError):
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
        self.assertEqual(final.statistics(data)["model"], "gpt-6-astra")

    def test_unavailable_or_invalid_agent_config_hides_only_model(self):
        self.run.update(agent_id="agent-current", model="do-not-fallback")
        invalid = [final.RunContextError("读取失败"), None, [],
                   {"id": "other", "workspace_id": "ws", "model": "gpt-6-astra"},
                   {"id": "agent-current", "workspace_id": "other", "model": "gpt-6-astra"},
                   {"id": "agent-current", "workspace_id": "ws", "model": ""},
                   {"id": "agent-current", "workspace_id": "ws", "model": "<!here>"}]
        for agent in invalid:
            with self.subTest(agent=agent), patch.object(final, "query", side_effect=[[self.run], agent, []]) as query:
                data = final.snapshot("issue", self.env)
                self.assertNotIn("model", data["run"])
                self.assertEqual(query.call_count, 3)
                self.assertIn("duration_seconds", final.statistics(data))

    def test_missing_current_run_never_uses_previous(self):
        with patch.object(final, "query", return_value=[{"id": "other"}]):
            with self.assertRaises(final.RunContextError):
                final.snapshot("issue", self.env)

    def test_log_failure_still_has_duration_without_invented_counts(self):
        with patch.object(final, "query", side_effect=[[self.run], final.RunContextError("失败")]):
            data = final.snapshot("issue", self.env)
        self.assertEqual(data["messages"], [])
        self.assertEqual(final.statistics(self.data), {"duration_seconds": 62})

    def test_skill_names_deduplicate_and_tokens_never_render(self):
        self.run.update(model="gpt-6-astra", usage=[{"input_tokens": 999}])
        for _ in range(2):
            self.message("tool_use", tool="exec_command", input={"command": "/bin/zsh -lc 'rtk proxy cat /skills/slack/SKILL.md'"})
            self.message("tool_result", tool="exec_command", output="---\nname: slack\ndescription: test\n---\n正文")
        stats = final.statistics(self.data)
        self.assertEqual(stats["tools"], 2)
        self.assertEqual(stats["skills"], 1)
        self.assertEqual(stats["model"], "gpt-6-astra")
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
        self.assertEqual(final.statistics(self.data)["skills"], 2)
        self.data["messages"][-1]["output"] = "---\nname: a\n---\n部分输出"
        self.assertNotIn("skills", final.statistics(self.data))

    def test_extracts_pr_and_branch_evidence_without_querying_github(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo --json url,headRefName"})
        self.message("tool_result", tool="exec_command", output='{"url":"https://github.com/owner/repo/pull/12","headRefName":"feature/a"}')
        self.message("tool_use", tool="exec_command", input={"cmd": "git -C /work/repo branch --show-current"})
        self.message("tool_result", tool="exec_command", output="feature/a\n")
        with patch.object(final, "query") as query:
            result = final.summarize(self.data)
            query.assert_not_called()
        self.assertEqual(result["code_evidence"][0]["call_seq"], 1)
        self.assertEqual(result["code_evidence"][0]["result_seq"], 2)
        self.assertIn("headRefName", result["code_evidence"][0]["output"])
        self.assertEqual(result["code_evidence"][1]["output"], "feature/a\n")

    def test_document_examples_are_not_code_evidence(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "cat /skills/github/SKILL.md"})
        self.message("tool_result", tool="exec_command", output="示例 https://github.com/owner/repo/pull/12")
        self.assertEqual(final.summarize(self.data)["code_evidence"], [])

    def test_concurrent_results_are_not_assigned_to_wrong_pr(self):
        for number in (12, 15):
            self.message("tool_use", tool="exec_command", input={"cmd": f"gh pr view {number} --repo owner/repo"})
        for number in (15, 12):
            self.message("tool_result", tool="exec_command", output=f"PR {number}")
        rows = final.summarize(self.data)["code_evidence"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("output" not in row for row in rows))

    def test_long_output_is_explicitly_marked_incomplete(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo"})
        self.message("tool_result", tool="exec_command", output="x" * 5000)
        row = final.summarize(self.data)["code_evidence"][0]
        self.assertTrue(row["output_truncated"])
        self.assertEqual(len(row["output"]), 4000)

    def test_foreign_messages_do_not_leak_into_summary(self):
        self.message("tool_use", tool="exec_command", input={"cmd": "gh pr view 12 --repo owner/repo"})
        self.data["messages"][0]["issue_id"] = "other"
        summary = final.summarize(self.data)
        self.assertEqual(summary["code_evidence"], [])
        self.assertIsNone(summary["last_message_seq"])


if __name__ == "__main__":
    unittest.main()
