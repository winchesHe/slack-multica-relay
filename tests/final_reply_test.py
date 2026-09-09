"""验证发送前快照的归属、证据、缺失字段和一次发送边界。"""
import importlib.util
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
