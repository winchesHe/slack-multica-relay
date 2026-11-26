import contextlib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bind_slack_thread", Path(__file__).resolve().parents[1] / "scripts/bind-slack-thread.py")
bind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bind)
ISSUE_ID = "01234567-89ab-cdef-0123-456789abcdef"
BASE = ["--workspace-id", "ws", "--project-id", "project", "--assignee-id", "agent",
        "--team-id", "T1", "--channel-id", "C1", "--root-ts", "100.000001",
        "--sender-user-id", "U1", "--mention-user-id", "U2", "--root-text", "Dorea <@U2>"]


class BindSlackThreadTest(unittest.TestCase):
    def args(self, extra=()):
        return bind.parser().parse_args(["--render-only"] + BASE + list(extra))

    def test_render_hash_scope_and_payload(self):
        result = bind.render(self.args(), "Jira 原描述\n\n原链接：https://example.test/AI-1\n")
        self.assertEqual(result["scope"], hashlib.sha256(b"ws:project:agent").hexdigest())
        self.assertEqual(result["threadDigest"], hashlib.sha256(b"T1:C1:100.000001").hexdigest())
        self.assertEqual(result["description"].splitlines()[0], result["marker"])
        self.assertTrue(result["description"].endswith("Jira 原描述\n\n原链接：https://example.test/AI-1\n"))
        self.assertEqual(result["description"].count(bind.PAYLOAD_START), 1)
        self.assertEqual(result["description"].count(bind.PAYLOAD_END), 1)
        payload = json.loads(re.search(r"```json\n([\s\S]*)\n```", result["description"])[1])
        self.assertEqual(payload["eventPayload"], {"teamId": "T1", "channelId": "C1", "messageTs": "100.000001",
            "threadTs": "100.000001", "senderUserId": "U1", "text": "Dorea <@U2>", "mention": {"type": "user", "id": "U2"}})
        bind.read_binding(result["description"], result, self.args())

    def test_squad_and_explicit_stable_scope(self):
        result = bind.render(self.args(["--assignee-type", "squad"]))
        self.assertEqual(result["scope"], hashlib.sha256(b"ws:project:squad:agent").hexdigest())
        explicit = bind.render(self.args(["--assignee-type", "squad", "--scope-id", "stable"]))
        self.assertEqual(explicit["scope"], hashlib.sha256(b"ws:project:stable").hexdigest())

    def test_safe_fence_and_reserved_text_roundtrip(self):
        args = self.args()
        args.root_text = "``````json\n" + bind.PAYLOAD_START + "\n" + bind.PAYLOAD_END + "\n<@U2> & 中文"
        rendered = bind.render(args)
        block = rendered["description"].split(bind.PAYLOAD_START)[1].split(bind.PAYLOAD_END)[0].strip()
        match = re.fullmatch(r"(`{3,})json\n([\s\S]*)\n\1", block)
        self.assertGreater(len(match[1]), 6)
        self.assertEqual(json.loads(match[2])["eventPayload"]["text"], args.root_text)
        self.assertNotIn("<@U2>", rendered["description"])
        self.assertEqual(rendered["description"].count(bind.PAYLOAD_START), 1)
        bind.read_binding(rendered["description"], rendered, args)

    def test_body_reserved_markers_are_refused_without_destroying_original(self):
        for marker in (bind.PAYLOAD_START, bind.PAYLOAD_END, "<!-- relay-thread:any -->"):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                bind.render(self.args(), "保留正文\n" + marker)

    def test_size_limits(self):
        args = self.args()
        args.root_text = "中" * (48 * 1024)
        with self.assertRaisesRegex(ValueError, "relay_payload_too_large"):
            bind.render(args)
        with self.assertRaisesRegex(ValueError, "task_presentation_too_large"):
            bind.render(self.args(), "a" * (64 * 1024))

    def invoke(self, runner=None, extra=(), mode="--apply"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bind.main([mode] + BASE + (["--issue-id", ISSUE_ID] if mode == "--apply" else []) + list(extra), runner=runner)
        return code, out.getvalue(), err.getvalue()

    def runner(self, description, change_before_write=False, lost_write=False, corrupt_readback=False, wrong_scope=False):
        state = {"description": description, "commands": [], "gets": 0}

        def run(command, **kwargs):
            state["commands"].append(command)
            self.assertTrue(kwargs["check"])
            self.assertEqual(command[:5], ["multica", "--server-url", "https://multica.devops.moego.dev", "--workspace-id", "ws"])
            if "get" in command:
                state["gets"] += 1
                if change_before_write and state["gets"] == 2:
                    state["description"] += "并发改动"
                value = {"id": ISSUE_ID, "workspace_id": "other" if wrong_scope else "ws", "project_id": "project",
                         "description": state["description"]}
                return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
            self.assertIn("--no-start", command)
            state["description"] = command[command.index("--description") + 1]
            if corrupt_readback:
                state["description"] = state["description"].replace(bind.PAYLOAD_START, "damaged")
            if lost_write:
                raise subprocess.TimeoutExpired(command, 30, output="private-secret")
            return subprocess.CompletedProcess(command, 0, "{}", "")
        return run, state

    def test_apply_preserves_body_readbacks_and_is_idempotent(self):
        run, state = self.runner("Jira 原文\n")
        code, out, _ = self.invoke(run)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["action"], "bound")
        self.assertTrue(state["description"].endswith("Jira 原文\n"))
        commands = len(state["commands"])
        code, out, _ = self.invoke(run)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["action"], "unchanged")
        self.assertEqual(len(state["commands"]) - commands, 1)

    def test_different_or_damaged_binding_is_never_overwritten(self):
        valid = bind.render(self.args())["description"]
        for current in [valid.replace("relay-thread:", "relay-thread:different:"),
                        valid.replace(bind.PAYLOAD_END, ""), "header\n" + valid,
                        valid + "\n" + bind.PAYLOAD_START,
                        valid.replace('"channelId": "C1"', '"channelId": "C2"')]:
            with self.subTest(current=current):
                run, state = self.runner(current)
                self.assertEqual(self.invoke(run)[0], 1)
                self.assertEqual(len(state["commands"]), 1)

    def test_concurrent_edit_and_wrong_workspace_stop_before_update(self):
        for options in [{"change_before_write": True}, {"wrong_scope": True}]:
            run, state = self.runner("Jira", **options)
            self.assertEqual(self.invoke(run)[0], 1)
            self.assertFalse(any("update" in command for command in state["commands"]))

    def test_ambiguous_write_has_no_blind_retry_or_private_output(self):
        run, state = self.runner("Jira", lost_write=True)
        code, out, err = self.invoke(run)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("private-secret", err)
        self.assertEqual(sum("update" in command for command in state["commands"]), 1)
        self.assertEqual(json.loads(self.invoke(run)[1])["action"], "unchanged")

    def test_readback_corruption_fails(self):
        run, _ = self.runner("Jira", corrupt_readback=True)
        self.assertEqual(self.invoke(run)[0], 1)

    def test_render_only_reads_body_file_without_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_text("保留 Jira 正文", encoding="utf-8")
            code, out, _ = self.invoke(extra=["--body-file", str(body)], mode="--render-only")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["description"].endswith("保留 Jira 正文"))

    def test_invalid_cli_arguments_fail_before_any_external_call(self):
        for extra in [["--root-ts", "1e3"], ["--channel-id", "c1"], ["--scope-id", " "],
                      ["--issue-id", "wrong"], ["--server-url", "http://example.test"], ["--body-file", "unused"]]:
            with self.subTest(extra=extra), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.invoke(extra=extra)


if __name__ == "__main__":
    unittest.main()
