import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(TESTS_DIR))

from duty_ops_test import FakeNative, issue, manifest  # noqa: E402

_ops_spec = importlib.util.spec_from_file_location("duty_ops", SCRIPTS_DIR / "duty-ops.py")
ops_module = importlib.util.module_from_spec(_ops_spec)
_ops_spec.loader.exec_module(ops_module)

_wd_spec = importlib.util.spec_from_file_location("duty_watchdog", SCRIPTS_DIR / "duty-watchdog.py")
wd_module = importlib.util.module_from_spec(_wd_spec)
_wd_spec.loader.exec_module(wd_module)


class WatchdogFakeNative(FakeNative):
    def call(self, args, content=None, timeout=None, ambiguous=False):
        if args[:2] == ["issue", "list"]:
            status_filter = args[args.index("--status") + 1] if "--status" in args else None
            rows = [
                row for row in self.issues.values()
                if status_filter is None or row.get("status") == status_filter
            ]
            return {"issues": rows, "has_more": False, "offset": 0, "total": len(rows)}
        return super().call(args, content=content, timeout=timeout, ambiguous=ambiguous)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.native = WatchdogFakeNative([])
        self.ops = ops_module.Ops(self.native, None, self.temp.name, agent_id="agent",
                                  runtime_id="12345678-1234-4234-8234-123456789abc", workspace_id="ws")

    def test_idle_when_only_legacy_or_blocked_or_terminal_issues_exist(self):
        self.native.issues["legacy-scan"] = issue("legacy-scan", status="in_progress", metadata={
            "dailyExecution": {"protocolVersion": 2, "policy": "lane_investigation_serial_repair_v2"}})
        self.native.issues["legacy-lane"] = issue("legacy-lane", parent_issue_id="legacy-scan", status="in_progress", metadata={
            "dailyLane": {"mode": "investigate_lane", "roundId": "legacy-scan", "state": "investigating"}})
        self.native.issues["blocked-v3"] = issue("blocked-v3", status="blocked", metadata={
            "dailyExecution": {"protocolVersion": 3, "policy": ops_module.POLICY}})
        self.native.issues["review-v3"] = issue("review-v3", status="in_review", metadata={
            "dailyExecution": {"protocolVersion": 3, "policy": ops_module.POLICY}})

        res = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent")
        self.assertEqual(res["status"], "idle")
        self.assertEqual(res["activeRounds"], [])
        self.assertEqual(res["recovered"], [])
        self.assertFalse(any(c[0] == "rerun" for c in self.native.calls))

    def test_recovers_stalled_v3_round_and_circuit_breaks_without_progress(self):
        scan = issue("scan-w", identifier="GRM-900", status="in_progress")
        lanes = [issue(f"lw-{n}", parent_issue_id="scan-w", status="in_progress", metadata={"dailyLane": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "investigate_lane", "roundId": "scan-w", "lane": n, "laneCount": 2}})
            for n in (1, 2)]
        for row in (scan, *lanes):
            self.native.issues[row["id"]] = row
            self.native.comments_by_issue[row["id"]] = row["_comments"]
            self.native.runs_by_issue[row["id"]] = []
        wm = Path(self.temp.name) / "manifest-w.json"
        wm.write_text(json.dumps(manifest(scan_id="scan-w", count=2)), encoding="utf-8")
        self.ops.init_scan_manifest("scan-w", wm, target_date="2026-10-21",
                                    scheduler_revision="a" * 40, runtime_id=self.ops.runtime_id, agent_id="agent")

        dry = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent", dry_run=True)
        self.assertEqual(dry["status"], "dry_run")
        self.assertEqual(dry["activeRounds"], ["scan-w"])
        self.assertEqual(len(dry["recovered"]), 1)
        self.assertFalse(any(c[0] == "rerun" for c in self.native.calls))

        # 1st real watchdog run restarts lw-1 & lw-2.
        res1 = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent",
                                      max_no_progress_restarts=2)
        self.assertEqual(res1["status"], "ok")
        self.assertTrue(res1["recovered"][0]["mutated"])
        self.assertEqual(res1["recovered"][0]["consecutiveRestarts"], 1)

        # Simulate runs exiting again with 0 progress.
        self.native.runs_by_issue["lw-1"] = [{"id": r["id"], "issue_id": "lw-1", "agent_id": "agent", "status": "completed"}
                                             for r in self.native.runs_by_issue["lw-1"]]
        self.native.runs_by_issue["lw-2"] = [{"id": r["id"], "issue_id": "lw-2", "agent_id": "agent", "status": "completed"}
                                             for r in self.native.runs_by_issue["lw-2"]]
        res2 = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent",
                                      max_no_progress_restarts=2)
        self.assertEqual(res2["recovered"][0]["consecutiveRestarts"], 2)

        # Simulate runs exiting again with still 0 progress -> 3rd attempt triggers circuit breaker!
        self.native.runs_by_issue["lw-1"] = [{"id": r["id"], "issue_id": "lw-1", "agent_id": "agent", "status": "completed"}
                                             for r in self.native.runs_by_issue["lw-1"]]
        self.native.runs_by_issue["lw-2"] = [{"id": r["id"], "issue_id": "lw-2", "agent_id": "agent", "status": "completed"}
                                             for r in self.native.runs_by_issue["lw-2"]]
        res3 = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent",
                                      max_no_progress_restarts=2)
        self.assertEqual(res3["status"], "circuit_open")
        self.assertEqual(len(res3["circuitOpen"]), 1)
        self.assertEqual(res3["recovered"], [])

        # Once round completes (moves to in_review), watchdog returns idle and prunes state!
        self.native.issues["scan-w"]["status"] = "in_review"
        self.native.issues["lw-1"]["status"] = "done"
        self.native.issues["lw-2"]["status"] = "done"
        res_done = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent",
                                          max_no_progress_restarts=2)
        self.assertEqual(res_done["status"], "idle")
        wd_state = json.loads((Path(self.temp.name) / "watchdog-state.json").read_text(encoding="utf-8"))
        self.assertEqual(wd_state["rounds"], {})

    def test_releases_terminal_repair_slot_holder_with_wait_queue(self):
        self.native.issues["scan-done"] = issue("scan-done", status="in_review", metadata={
            "dailyExecution": {"protocolVersion": 3, "policy": ops_module.POLICY}})
        self.native.issues["scan-wait"] = issue("scan-wait", status="in_progress", metadata={
            "dailyExecution": {"protocolVersion": 3, "policy": ops_module.POLICY}})
        self.native.comments_by_issue["scan-done"] = []
        self.native.comments_by_issue["scan-wait"] = []
        self.native.runs_by_issue["scan-done"] = []
        self.native.runs_by_issue["scan-wait"] = []
        (Path(self.temp.name) / "global-repair-slot.json").write_text(
            json.dumps({"holder": {"roundId": "scan-done", "acquiredAt": "2026-11-21T00:00:00Z"}, "waitQueue": ["scan-wait"]}), encoding="utf-8"
        )
        res = wd_module.run_watchdog(ops_module, self.ops, project_id="project", agent_id="agent")
        self.assertEqual(res["slotReleased"]["wokenRoundId"], "scan-wait")
        self.assertTrue(any(c[:2] == ("rerun", "scan-wait") for c in self.native.calls))


if __name__ == "__main__":
    unittest.main()
