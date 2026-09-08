"""验证单运行运维命令的读前、读后和不明写结果边界。"""
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

scripts = Path(__file__).parents[1] / "scripts"
reply_spec = importlib.util.spec_from_file_location("reply", scripts / "reply.py")
reply = importlib.util.module_from_spec(reply_spec)
reply_spec.loader.exec_module(reply)
spec = importlib.util.spec_from_file_location("footer_recovery", scripts / "footer-recovery.py")
recovery = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"reply": reply}):
    spec.loader.exec_module(recovery)


class RecoveryCommandTests(unittest.TestCase):
    def execute(self, operation, responses):
        args = ["footer-recovery.py", operation, "--issue", "44444444-4444-4444-4444-444444444444",
                "--task", "55555555-5555-5555-5555-555555555555"]
        env = {"RELAY_RECOVERY_URL": "https://relay.test/api/footer/recovery", "CRON_SECRET": "c" * 32}
        with patch.object(sys, "argv", args), patch.dict(recovery.os.environ, env, clear=True), \
                patch.object(recovery, "request", side_effect=responses) as request, \
                patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = recovery.main()
            return code, [call.args[3] for call in request.call_args_list], stderr.getvalue()

    def test_retry_reads_before_and_after_write(self):
        code, operations, _ = self.execute("retry", [{"messageTs": "1", "done": False}, {"action": "accepted"}, {"done": False}])
        self.assertEqual(code, 0)
        self.assertEqual(operations, ["inspect", "retry", "inspect"])

    def test_done_is_not_replayed(self):
        code, operations, _ = self.execute("retry", [{"messageTs": "1", "done": True}])
        self.assertEqual(code, 0)
        self.assertEqual(operations, ["inspect"])

    def test_missing_binding_is_not_replayed(self):
        code, operations, _ = self.execute("retry", [{"messageTs": None}])
        self.assertEqual(code, 1)
        self.assertEqual(operations, ["inspect"])

    def test_unknown_write_result_does_not_repeat_or_leak_error(self):
        code, operations, stderr = self.execute("retry", [{"messageTs": "1"}, urllib.error.URLError("private upstream content")])
        self.assertEqual(code, 1)
        self.assertEqual(operations, ["inspect", "retry"])
        self.assertNotIn("private upstream content", stderr)
        self.assertIn("inspect", stderr)

    def test_inspect_never_writes(self):
        code, operations, _ = self.execute("inspect", [{"messageTs": "1", "stopped": {"reason": "footer_unavailable"}}])
        self.assertEqual(code, 0)
        self.assertEqual(operations, ["inspect"])
