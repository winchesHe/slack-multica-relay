"""Closed Loop B v3 的并发、租约、intent、回执和 legacy 隔离回归。"""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest


SPEC = importlib.util.spec_from_file_location("duty_ops", Path(__file__).parents[1] / "scripts" / "duty-ops.py")
ops_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops_module)


class FakeNative:
    def __init__(self, issues, *, ambiguous=False):
        self.issues = {row["id"]: row for row in issues}
        self.comments_by_issue = {row["id"]: row.setdefault("_comments", []) for row in issues}
        self.runs_by_issue = {row["id"]: row.pop("_runs", []) for row in issues}
        self.attachments = {}
        self.next_attachment = 0
        self.next_run = 0
        self.ambiguous = ambiguous
        self.ledger_failure = False
        self.calls = []
        self.lock = threading.Lock()

    def issue(self, issue_id):
        with self.lock:
            return copy.deepcopy(self.issues[issue_id])

    def list_project(self, project_id):
        with self.lock:
            return copy.deepcopy([row for row in self.issues.values() if row.get("project_id") == project_id])

    def comments(self, issue_id):
        with self.lock:
            return copy.deepcopy(self.comments_by_issue[issue_id])

    def runs(self, issue_id):
        with self.lock:
            return copy.deepcopy(self.runs_by_issue.setdefault(issue_id, []))

    def metadata_set(self, issue_id, key, value):
        with self.lock:
            self.issues[issue_id].setdefault("metadata", {})[key] = copy.deepcopy(value)
            self.calls.append(("metadata", issue_id, key))

    def comment_add(self, issue_id, content_file, attachment_file=None):
        content = Path(content_file).read_text(encoding="utf-8")
        if self.ledger_failure and issue_id == "GRM-664":
            raise ops_module.AmbiguousWrite("ledger timeout")
        with self.lock:
            result = {}
            if attachment_file:
                self.next_attachment += 1
                aid = f"att-{self.next_attachment}"
                self.attachments[aid] = Path(attachment_file).read_bytes()
                result["attachmentId"] = aid
            if "<!-- " in content:
                self.comments_by_issue[issue_id].append({
                    "id": f"comment-{len(self.comments_by_issue[issue_id]) + 1}",
                    "author_type": "agent", "author_id": "agent",
                    "created_at": "2026-10-23T00:00:00Z", "content": content,
                })
                result["commentId"] = self.comments_by_issue[issue_id][-1]["id"]
            return result

    def attachment_download(self, attachment, directory):
        path = Path(directory) / "evidence.bin"
        path.write_bytes(self.attachments[attachment])
        return {"ok": True}

    def assign(self, issue_id, agent_id):
        with self.lock:
            self.issues[issue_id]["assignee_id"] = agent_id
            self.calls.append(("assign", issue_id))

    def rerun(self, issue_id):
        with self.lock:
            self.next_run += 1
            run = {"id": f"run-{self.next_run}", "issue_id": issue_id, "agent_id": "agent",
                   "runtime_id": "runtime", "status": "queued"}
            self.runs_by_issue.setdefault(issue_id, []).append(run)
            self.calls.append(("rerun", issue_id))
            if self.ambiguous:
                raise ops_module.AmbiguousWrite("fake timeout")

    def status(self, issue_id, value):
        with self.lock:
            self.issues[issue_id]["status"] = value
            self.calls.append(("status", issue_id, value))

    def timeline(self, issue_id):
        return self.issues[issue_id].get("_timeline", [])

    def create(self, args):
        with self.lock:
            self.next_run += 1
            issue_id = f"created-{self.next_run}"
            parent = args[args.index("--parent") + 1]
            row = {"id": issue_id, "project_id": "project", "parent_issue_id": parent,
                   "status": "backlog", "assignee_id": None, "metadata": {}, "_comments": []}
            self.issues[issue_id] = row
            self.comments_by_issue[issue_id] = row["_comments"]
            self.runs_by_issue[issue_id] = []
            return {"id": issue_id}


class FakeSlack:
    def __init__(self):
        self.messages = []
        self.history_calls = 0
        self.uploads = []
        self.edit_calls = []
        self.ambiguous_send = False

    def send(self, channel, text_file, blocks_file=None, thread_ts=None, dry_run=False):
        if dry_run:
            return {"preview_digest": "preview"}
        link = f"https://slack.example/{len(self.messages) + 1}"
        self.messages.append({"permalink": link, "text": Path(text_file).read_text(encoding="utf-8")})
        return {} if self.ambiguous_send else {"permalink": link}

    def upload(self, channel, file_path, digest, message, thread_ts=None):
        self.uploads.append((channel, digest, message, thread_ts))
        if self.messages:
            self.messages[-1].setdefault("files", []).append({"id": "file-1", "sha256": digest})
        return {"file": {"id": "file-1"}}

    def get(self, url):
        return {"ok": True, "permalink": url}

    def edit(self, url, text_file, blocks_file=None, dry_run=False):
        self.edit_calls.append((url, text_file, blocks_file, dry_run))
        for message in self.messages:
            if message["permalink"] == url:
                message["text"] = Path(text_file).read_text(encoding="utf-8")
        return {"ok": True, "permalink": url}

    def replies(self, url):
        return {"messages": self.messages}

    def history(self, channel, limit=200, thread_ts=None):
        self.history_calls += 1
        return {"messages": self.messages}


def issue(issue_id, **kwargs):
    return {"id": issue_id, "project_id": "project", "status": "in_progress", "assignee_id": "agent",
            "created_at": "2026-10-22T00:00:00Z", "metadata": {}, "_comments": [], **kwargs}


def manifest(scan_id="scan", count=1):
    candidates = [{"candidateId": f"intercom:{i}", "feedbackDate": "2026-10-21",
                   "summary": f"问题 {i}", "source": "intercom",
                   "sourceUrl": f"https://app.intercom.com/conversation/{i}"} for i in range(1, count + 1)]
    return {"protocolVersion": 3, "roundId": scan_id, "feedbackDate": "2026-10-21",
            "candidateCount": len(candidates), "candidates": candidates,
            "candidateSetSha256": ops_module.candidate_hash([item["candidateId"] for item in candidates])}


def v3_world(count=1):
    scan = issue("scan", status="in_progress", assignee_id="agent")
    lanes = []
    for number in (1, 2):
        lanes.append(issue(f"lane-{number}", parent_issue_id="scan", metadata={"dailyLane": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "investigate_lane", "roundId": "scan",
            "lane": number, "laneCount": 2}}))
    for row in [scan, *lanes]:
        row["project_id"] = "project"
    return [scan, *lanes]


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.native = FakeNative(v3_world())
        self.slack = FakeSlack()
        self.native.issues["scan"]["identifier"] = "GRM-1"
        self.native.issues["GRM-664"] = issue("GRM-664", status="in_progress", identifier="GRM-664")
        self.native.comments_by_issue["GRM-664"] = self.native.issues["GRM-664"]["_comments"]
        self.native.runs_by_issue["GRM-664"] = []
        self.now = dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc)
        self.ops = ops_module.Ops(self.native, self.slack, self.temp.name, now=self.now,
                                  agent_id="agent", runtime_id="12345678-1234-4234-8234-123456789abc")
        self.manifest_path = Path(self.temp.name) / "manifest.json"
        self.manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
        self.ops.init_scan_manifest("scan", self.manifest_path, target_date="2026-10-21",
                                    scheduler_revision="a" * 40,
                                    runtime_id="12345678-1234-4234-8234-123456789abc", agent_id="agent")

    def test_manifest_metadata_and_forbidden_keys(self):
        scan = self.native.issue("scan")
        self.assertEqual(ops_module.metadata(scan, "dailyExecution")["policy"], ops_module.POLICY)
        self.assertNotIn("dailyRound", scan["metadata"])
        self.assertNotIn("dailyBatch", scan["metadata"])
        self.assertIsNotNone(self.ops.manifest("scan", "agent"))

    def test_exited_child_run_without_conclusion_is_retried_before_skip(self):
        self.native.runs_by_issue["lane-1"] = [{"id": "child-1", "issue_id": "lane-1", "agent_id": "agent",
                                                "runtime_id": "runtime", "status": "running"}]
        first = self.ops.claim("scan", "lane-1")
        self.native.runs_by_issue["lane-1"][0]["status"] = "completed"
        second = self.ops.claim("scan", "lane-2")
        self.assertEqual(second["status"], "claimed")
        self.assertEqual(second["candidateId"], first["candidateId"])
        self.assertGreater(second["fencingToken"], first["fencingToken"])
        self.assertEqual(self.ops.state("scan")["failedRuns"][first["candidateId"]], ["child-1"])

    def test_fencing_token_rejects_stale_worker_after_ttl(self):
        first = self.ops.claim("scan", "lane-1", worker_id="a", ttl_seconds=1)
        later = ops_module.Ops(self.native, self.slack, self.temp.name,
                               now=self.now + dt.timedelta(seconds=2), agent_id="agent",
                               runtime_id=self.ops.runtime_id)
        second = later.claim("scan", "lane-2", worker_id="b", ttl_seconds=3600)
        self.assertGreater(second["fencingToken"], first["fencingToken"])
        report = Path(self.temp.name) / "report.html"
        report.write_text("<html>evidence</html>", encoding="utf-8")
        record = {"conclusion": "duplicate", "evidenceRevision": "intercom:1", "conclusionRef": "JIRA-1"}
        record_path = Path(self.temp.name) / "record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ops_module.OpsError):
            self.ops.submit_investigation("scan", "lane-1", "intercom:1", first["fencingToken"], record_path, report)

    def test_three_failed_runs_emit_skipped_failure(self):
        state = self.ops.state("scan")
        state["nextFencingToken"] = 3
        state["failedRuns"] = {"intercom:1": ["run-1", "run-2"]}
        state["leases"] = {"intercom:1": {"candidateId": "intercom:1", "laneIssueId": "lane-1",
                                              "fencingToken": 3, "runIds": ["run-3"],
                                              "expiresAt": "2026-10-22T00:00:00Z"}}
        self.ops.save_state("scan", state)
        result = self.ops.claim("scan", "lane-2")
        self.assertEqual(result["status"], "all_concluded")
        comments = self.native.comments("lane-1")
        self.assertTrue(any(ops_module.comment_json(c, ops_module.INVESTIGATION_MARKER)
                            and ops_module.comment_json(c, ops_module.INVESTIGATION_MARKER)["conclusion"] == "skipped_failure"
                            for c in comments))

    def test_ambiguous_rerun_reconciles_by_run_id_without_duplicate(self):
        self.native.ambiguous = True
        first = self.ops.dispatch_if_issue_idle("scan", round_id="scan")
        self.assertEqual(first["status"], "started")
        self.assertEqual([call for call in self.native.calls if call[0] == "rerun"], [("rerun", "scan")])
        second = self.ops.dispatch_if_issue_idle("scan", round_id="scan")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(len([call for call in self.native.calls if call[0] == "rerun"]), 1)

    def test_parent_wakeup_race_has_one_successor(self):
        self.native.runs_by_issue["scan"] = [{"id": "running", "issue_id": "scan", "agent_id": "agent",
                                               "runtime_id": "runtime", "status": "running"}]
        results = []
        barrier = threading.Barrier(2)
        def wake():
            barrier.wait()
            results.append(self.ops.rerun_parent_if_needed("scan"))
        threads = [threading.Thread(target=wake) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len([run for run in self.native.runs("scan") if run["status"] == "queued"]), 1)
        self.assertEqual(sum(result["status"] in {"started", "reconciled"} for result in results), 1)
        self.assertTrue(any(result["status"] == "reused" for result in results))

    def test_global_repair_slot_is_fifo_and_single_winner(self):
        for name in ("scan-a", "scan-b"):
            self.native.issues[name] = issue(name, status="in_progress")
            self.native.comments_by_issue[name] = self.native.issues[name]["_comments"]
            self.native.runs_by_issue[name] = []
        outcomes = []
        barrier = threading.Barrier(2)
        def acquire(round_id):
            barrier.wait()
            outcomes.append(self.ops.acquire_global_repair_slot(round_id))
        threads = [threading.Thread(target=acquire, args=(name,)) for name in ("scan-a", "scan-b")]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(item["status"] == "acquired" for item in outcomes), 1)
        busy = next(item for item in outcomes if item["status"] == "busy")
        self.assertIn(busy["roundId"], busy["waitQueue"])
        holder = next(item["roundId"] for item in outcomes if item["status"] == "acquired")
        released = self.ops.release_global_repair_slot(holder)
        self.assertEqual(released["wokenRoundId"], busy["roundId"])

    def test_legacy_recovery_is_read_only(self):
        scan = self.native.issues["scan"]
        scan["metadata"]["dailyExecution"] = {"protocolVersion": 2, "dailyRound": {"state": "investigating"}}
        before = copy.deepcopy(scan)
        result = self.ops.recover("scan", apply=True)
        self.assertEqual(result["status"], "legacy_round_delegate")
        self.assertEqual(self.native.issue("scan"), before)

    def test_inspect_distinguishes_missing_receipt_and_needs_recovery(self):
        bug = issue("bug-1", parent_issue_id="scan", status="in_review", metadata={"dailyBug": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": "scan",
            "sourceScanId": "scan", "candidateId": "intercom:1", "investigationRef": "scan:intercom:1",
            "feedbackDate": "2026-10-21", "source": "intercom", "sourceUrl": "https://app.intercom.com/conversation/1"}})
        bug["project_id"] = "project"
        self.native.issues["bug-1"] = bug
        self.native.comments_by_issue["bug-1"] = bug["_comments"]
        self.native.runs_by_issue["bug-1"] = []
        result = self.ops.inspect_round("scan", project_id="project")
        self.assertIn("bug-1", result["missingReceipt"])
        self.assertNotIn("bug-1", result["needsRecovery"])

    def test_valid_investigation_and_forbidden_bug_schema(self):
        entry = {**manifest()["candidates"][0], "_roundId": "scan"}
        good = {"schemaVersion": 1, "protocolVersion": 3, "mode": "investigate", "roundId": "scan",
                "sourceScanId": "scan", "candidateId": "intercom:1", "feedbackDate": "2026-10-21",
                "sourceUrl": entry["sourceUrl"], "laneIssueId": "lane-1", "fencingToken": 1,
                "conclusion": "confirmed", "evidenceRevision": "intercom:1",
                "report": {"attachmentId": "att", "sha256": "a", "readbackVerified": True},
                "verification": {"fixable": True, "productDecisionRequired": False,
                                  "expectedBehaviorRef": "e", "observedDeviationRef": "o", "dedupeRef": "d"}}
        self.assertTrue(ops_module.valid_investigation(good, entry, "lane-1"))
        bug = issue("bug", metadata={"dailyBug": {"schemaVersion": 1, "protocolVersion": 3, "mode": "bug",
                                                     "batchId": "scan", "sourceScanId": "scan",
                                                     "candidateId": "intercom:1", "investigationRef": "scan:intercom:1",
                                                     "feedbackDate": "2026-10-21", "source": "intercom",
                                                     "sourceUrl": entry["sourceUrl"], "state": "running"}})
        with self.assertRaises(ops_module.OpsError):
            self.ops._bug_metadata_valid(bug, self.native.issue("scan"))

    def test_in_progress_without_active_run_is_needs_recovery(self):
        self.native.issues["scan"]["status"] = "in_progress"
        self.native.runs_by_issue["scan"] = []
        result = self.ops.inspect_round("scan", project_id="project")
        self.assertIn("scan", result["needsRecovery"])
        self.assertEqual(result["missingReceipt"], [])

    def test_finish_scan_writes_finish_receipt_and_progress_reads_it(self):
        self.native.metadata_set("scan", "dailyScan", {
            "targetDate": "2026-10-21", "coverageComplete": True,
            "sources": {"canny": {"status": "success"}, "facebook": {"status": "success"},
                        "intercom": {"status": "success"}},
            "collected": {"canny": 1, "facebook": 0, "intercom": 1}})
        self.native.issues["scan"]["created_at"] = "2026-10-22T18:00:00Z"
        claim = self.ops.claim("scan", "lane-1")
        report = Path(self.temp.name) / "investigation.html"
        report.write_text("investigation", encoding="utf-8")
        record = {"conclusion": "confirmed", "evidenceRevision": "intercom:1",
                  "verification": {"fixable": True, "productDecisionRequired": False,
                                    "expectedBehaviorRef": "expected", "observedDeviationRef": "observed",
                                    "dedupeRef": "dedupe"}}
        record_path = Path(self.temp.name) / "investigation.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.ops.submit_investigation("scan", "lane-1", "intercom:1", claim["fencingToken"], record_path, report)
        evidence = Path(self.temp.name) / "scan-evidence-index.json"
        evidence.write_text(json.dumps({"records": [{"candidateId": "intercom:1", "state": "confirmed"}]}), encoding="utf-8")
        text = Path(self.temp.name) / "scan.txt"
        text.write_text("scan summary", encoding="utf-8")
        finish = self.ops.finish_scan("scan", evidence, ledger_issue="GRM-664", channel="C0C14HSSP2T",
                                      text_file=text, target_status="in_review")
        self.assertEqual(finish["protocolVersion"], 3)
        self.assertTrue(finish["evidenceIndex"]["readbackVerified"])
        self.assertTrue(any(ops_module.comment_json(comment, ops_module.FINISH_MARKER)
                            for comment in self.native.comments("scan")))
        ledger_rows = [ops_module.comment_json(comment, ops_module.BOARD_MARKER)
                       for comment in self.native.comments("GRM-664")]
        ledger_row = next(row for row in ledger_rows if row)
        self.assertEqual(ledger_row["scanDate"], "2026-10-23")
        self.assertEqual(self.ops._ledger("GRM-664", ledger_row),
                         next(comment["id"] for comment in self.native.comments("GRM-664")
                              if ops_module.comment_json(comment, ops_module.BOARD_MARKER) == ledger_row))
        progress_spec = importlib.util.spec_from_file_location("duty_progress", Path(__file__).parents[1] / "scripts" / "duty-progress.py")
        progress = importlib.util.module_from_spec(progress_spec)
        progress_spec.loader.exec_module(progress)
        issues = self.native.list_project("project")
        snapshot = progress.snapshot(self.native.issue("scan"), issues, [], self.now,
                                     "https://multica.example/issues/")
        self.assertEqual(snapshot["investigation"]["state"], "completed")
        self.assertEqual(snapshot["investigation"]["confirmed"], 1)
        self.assertFalse(snapshot["warnings"])

    def test_ledger_failure_recovery_reuses_slack_without_resend(self):
        bug = issue("bug-intent", parent_issue_id="scan", status="in_progress", metadata={"dailyBug": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": "scan",
            "sourceScanId": "scan", "candidateId": "intercom:1", "investigationRef": "scan:intercom:1",
            "feedbackDate": "2026-10-21", "source": "intercom",
            "sourceUrl": "https://app.intercom.com/conversation/1"}})
        self.native.issues[bug["id"]] = bug
        self.native.comments_by_issue[bug["id"]] = bug["_comments"]
        self.native.runs_by_issue[bug["id"]] = []
        report = Path(self.temp.name) / "partial.html"
        report.write_text("partial", encoding="utf-8")
        text = Path(self.temp.name) / "partial.txt"
        text.write_text("bug-intent delivery", encoding="utf-8")
        self.native.ledger_failure = True
        with self.assertRaises(ops_module.AmbiguousWrite):
            self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664",
                                 channel="C0C14HSSP2T", text_file=text,
                                 prs=["https://github.com/o/r/pull/2"], pr_heads=["b" * 40])
        sent = len(self.slack.messages)
        self.native.ledger_failure = False
        receipt = self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664",
                                       channel="C0C14HSSP2T", text_file=text,
                                       prs=["https://github.com/o/r/pull/2"], pr_heads=["b" * 40])
        self.assertEqual(len(self.slack.messages), sent)
        self.assertTrue(receipt["ledger"]["readbackVerified"])

        for bug_id, candidate_id in (("bug-1", "intercom:1"), ("bug-2", "intercom:2")):
            bug = issue(bug_id, parent_issue_id="scan", status="in_progress", metadata={"dailyBug": {
                "schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": "scan",
                "sourceScanId": "scan", "candidateId": candidate_id, "investigationRef": f"scan:{candidate_id}",
                "feedbackDate": "2026-10-21", "source": "intercom",
                "sourceUrl": f"https://app.intercom.com/conversation/{candidate_id.split(':')[-1]}"}})
            bug["project_id"] = "project"
            self.native.issues[bug_id] = bug
            self.native.comments_by_issue[bug_id] = bug["_comments"]
            self.native.runs_by_issue[bug_id] = []
        report = Path(self.temp.name) / "delivery.html"
        report.write_text("<html>delivery</html>", encoding="utf-8")
        text = Path(self.temp.name) / "slack.txt"
        text.write_text("bug-1 delivery", encoding="utf-8")
        receipt = self.ops.deliver_bug(bug_id="bug-1", report_file=report, ledger_issue="GRM-664",
                                       channel="C0C14HSSP2T", text_file=text,
                                       prs=["https://github.com/o/r/pull/1"], pr_heads=["a" * 40])
        self.assertTrue(receipt["report"]["readbackVerified"])
        self.assertEqual(self.native.issue("bug-1")["status"], "in_review")
        blocker = {"kind": "permission", "reason": "缺少测试权限", "resumeCondition": "权限恢复后重试"}
        text.write_text("bug-2 blocked", encoding="utf-8")
        blocked = self.ops.block_bug(bug_id="bug-2", blocker=blocker, ledger_issue="GRM-664",
                                     channel="C0C14HSSP2T", text_file=text)
        self.assertEqual(blocked["blocker"], blocker)
        self.assertEqual(self.native.issue("bug-2")["status"], "blocked")

    def test_ambiguous_slack_send_recovers_history_without_double_post(self):
        bug = issue("bug-slack", parent_issue_id="scan", status="in_progress", metadata={"dailyBug": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": "scan",
            "sourceScanId": "scan", "candidateId": "intercom:1", "investigationRef": "scan:intercom:1",
            "feedbackDate": "2026-10-21", "source": "intercom",
            "sourceUrl": "https://app.intercom.com/conversation/1"}})
        self.native.issues[bug["id"]] = bug
        self.native.comments_by_issue[bug["id"]] = bug["_comments"]
        self.native.runs_by_issue[bug["id"]] = []
        report = Path(self.temp.name) / "slack-ambiguous.html"
        report.write_text("ambiguous", encoding="utf-8")
        text = Path(self.temp.name) / "slack-ambiguous.txt"
        text.write_text("bug-slack delivery https://app.intercom.com/conversation/1", encoding="utf-8")
        self.slack.ambiguous_send = True
        with self.assertRaises(ops_module.AmbiguousWrite):
            self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664",
                                 channel="C0C14HSSP2T", text_file=text,
                                 prs=["https://github.com/o/r/pull/3"], pr_heads=["c" * 40])
        sent = len(self.slack.messages)
        self.slack.ambiguous_send = False
        self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664",
                             channel="C0C14HSSP2T", text_file=text,
                             prs=["https://github.com/o/r/pull/3"], pr_heads=["c" * 40])
        self.assertEqual(len(self.slack.messages), sent)
        self.assertGreaterEqual(self.slack.history_calls, 2)
    def test_attachment_id_prefers_nested_attachment_over_comment_id(self):
        self.assertEqual(ops_module.attachment_id({"id": "comment-uuid", "attachments": [{"id": "att-uuid"}]}), "att-uuid")
        self.assertIsNone(ops_module.attachment_id({"id": "comment-uuid", "content": "comment"}))

    def test_cli_contract_and_path_traversal_validation(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            class Result:
                stdout = json.dumps({"comments": []} if "comment" in command else {"id": "new"})
            return Result()
        native = ops_module.Native("https://multica.example", "workspace", runner=runner)
        native.comments("issue")
        native.create(["--project", "project", "--title", "lane", "--parent", "scan"])
        comment_call = next(call for call in calls if "comment" in call)
        self.assertIn("--full", comment_call)
        self.assertIn("--compact", comment_call)
        create_call = next(call for call in calls if "create" in call)
        self.assertIn("--parent", create_call)
        self.assertNotIn("--parent-id", create_call)
        self.assertEqual(ops_module.main(["inspect-round", "--workspace-id", "workspace",
                                          "--round-id", "../escape", "--state-dir", self.temp.name]), 1)

    def test_orphan_lane_is_reused_and_metadata_completed(self):
        self.native.issues.pop("lane-2")
        self.native.comments_by_issue.pop("lane-2")
        self.native.runs_by_issue.pop("lane-2")
        orphan = issue("orphan", parent_issue_id="scan", title="反馈调查｜10-21 反馈｜工位 2/2",
                       description="mode=investigate_lane\nroundId=scan\n")
        self.native.issues["orphan"] = orphan
        self.native.comments_by_issue["orphan"] = orphan["_comments"]
        self.native.runs_by_issue["orphan"] = []
        lanes = self.ops._ensure_lanes(self.native.issue("scan"), "project", "agent", "2026-10-21")
        self.assertEqual(len(lanes), 2)
        self.assertEqual(self.native.issue("orphan")["metadata"]["dailyLane"]["lane"], 2)

    def test_block_then_deliver_recovery_and_v3_progress_readback(self):
        bug = issue("bug-recover", parent_issue_id="scan", metadata={"dailyBug": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": "scan",
            "sourceScanId": "scan", "candidateId": "intercom:1", "investigationRef": "scan:intercom:1",
            "feedbackDate": "2026-10-21", "source": "intercom", "sourceUrl": "https://app.intercom.com/conversation/1"}})
        self.native.issues[bug["id"]] = bug
        self.native.comments_by_issue[bug["id"]] = bug["_comments"]
        self.native.runs_by_issue[bug["id"]] = []
        text = Path(self.temp.name) / "recovery.txt"
        text.write_text("blocked")
        blocker = {"kind": "permission", "reason": "缺少权限", "resumeCondition": "权限恢复"}
        self.ops.block_bug(bug_id=bug["id"], blocker=blocker, ledger_issue="GRM-664",
                           channel="C0C14HSSP2T", text_file=text)
        blocked_url = self.slack.messages[0]["permalink"]
        report = Path(self.temp.name) / "recovery.html"
        report.write_text("<html>done</html>")
        text.write_text("delivered")
        delivered = self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664",
                                          channel="C0C14HSSP2T", text_file=text,
                                          prs=["https://github.com/o/r/pull/10"], pr_heads=["a" * 40])
        self.assertEqual(delivered["deliveryVersion"], 1)
        self.assertNotEqual(delivered["slack"]["permalink"], blocked_url)
        self.assertTrue(any(call[0] == blocked_url for call in getattr(self.slack, "edit_calls", [])))
        progress_spec = importlib.util.spec_from_file_location("duty_progress_recovery", Path(__file__).parents[1] / "scripts" / "duty-progress.py")
        progress = importlib.util.module_from_spec(progress_spec); progress_spec.loader.exec_module(progress)
        self.assertEqual(progress.v3_latest_receipt(self.native.issue(bug["id"]), self.native.issue("scan"))[1], "delivered")
        board_spec = importlib.util.spec_from_file_location("duty_board_recovery", Path(__file__).parents[1] / "scripts" / "duty-board.py")
        board = importlib.util.module_from_spec(board_spec); board_spec.loader.exec_module(board)
        self.assertEqual(board.progress_module.v3_latest_receipt(self.native.issue(bug["id"]), self.native.issue("scan"))[1], "delivered")

    def test_delivery_rework_increments_version_and_uploads_to_thread(self):
        bug = issue("bug-rework", parent_issue_id="scan", metadata={"dailyBug": {
            "schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": "scan",
            "sourceScanId": "scan", "candidateId": "intercom:1", "investigationRef": "scan:intercom:1",
            "feedbackDate": "2026-10-21", "source": "intercom", "sourceUrl": "https://app.intercom.com/conversation/1"}})
        self.native.issues[bug["id"]] = bug; self.native.comments_by_issue[bug["id"]] = bug["_comments"]; self.native.runs_by_issue[bug["id"]] = []
        report = Path(self.temp.name) / "rework.html"; report.write_text("one")
        text = Path(self.temp.name) / "rework.txt"; text.write_text("one")
        self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664", channel="C0",
                             text_file=text, thread_ts="1234567890.123456", prs=["https://github.com/o/r/pull/1"], pr_heads=["a" * 40])
        report.write_text("two"); text.write_text("two")
        second = self.ops.deliver_bug(bug_id=bug["id"], report_file=report, ledger_issue="GRM-664", channel="C0",
                                      text_file=text, thread_ts="1234567890.123456", prs=["https://github.com/o/r/pull/2"], pr_heads=["b" * 40])
        self.assertEqual(second["deliveryVersion"], 2)
        self.assertEqual(self.slack.uploads[-1][3], "1234567890.123456")
        self.assertTrue(any("version=2" in comment["content"] for comment in self.native.comments(bug["id"])))

    def test_launch_reconciliation_and_parent_intent_guards(self):
        result = self.ops._launch_if_idle_unlocked("scan", "agent", round_id="scan")
        self.assertIn(result["status"], {"started", "reconciled"})
        self.native.runs_by_issue["scan"][0]["status"] = "completed"
        second = self.ops._launch_if_idle_unlocked("scan", "agent", round_id=None)
        self.assertEqual(second["status"], "started")
        self.assertTrue((Path(self.temp.name) / "dispatch-scan.json").exists())
        self.native.runs_by_issue["scan"] = [{"id": "running", "issue_id": "scan", "agent_id": "agent", "status": "running"}]
        active = self.ops._launch_if_idle_unlocked("scan", "agent", round_id="scan")
        self.assertEqual(active["status"], "active")
        before = len(self.native.runs_by_issue["scan"])
        self.native.issues["scan"]["status"] = "cancelled"
        self.ops._rerun_parent_if_needed_unlocked("scan", "agent")
        self.assertEqual(len(self.native.runs_by_issue["scan"]), before)

    def test_legacy_policy_and_top_level_metadata_are_read_only(self):
        scan = self.native.issues["scan"]
        scan["metadata"]["dailyRound"] = {"state": "old"}
        scan["metadata"]["dailyExecution"] = {"protocolVersion": 3, "policy": "old"}
        before = copy.deepcopy(scan)
        self.assertEqual(self.ops.inspect_round("scan")["status"], "legacy_round_delegate")
        self.assertEqual(self.ops.recover("scan", apply=True)["status"], "legacy_round_delegate")
        self.assertEqual(self.native.issue("scan"), before)

    def test_manual_override_and_intercom_source_token_deduplication(self):
        record = {"schemaVersion": 1, "protocolVersion": 3, "mode": "investigate", "roundId": "scan",
                  "sourceScanId": "scan", "candidateId": "intercom:1", "feedbackDate": "2026-10-21",
                  "sourceUrl": "https://app.intercom.com/conversation/1", "laneIssueId": "lane-1", "fencingToken": 1,
                  "conclusion": "duplicate", "evidenceRevision": "intercom:1", "manualOverride": True,
                  "report": {"attachmentId": "att-manual", "sha256": "x", "readbackVerified": True},
                  "conclusionRef": "JIRA-1"}
        self.native.comments_by_issue["lane-1"].append({"author_type": "member", "author_id": "member",
            "created_at": "2026-10-23T00:00:00Z", "content": ops_module.comment_text(ops_module.INVESTIGATION_MARKER, record, " round=scan candidate=intercom:1")})
        _, records = self.ops._records("scan", self.ops.manifest("scan"), [self.native.issue("lane-1")], "agent")
        self.assertEqual(records["intercom:1"]["conclusion"], "duplicate")
        self.assertIn("intercom:1", ops_module.source_tokens(record["sourceUrl"]))
        self.assertIn("intercom:1", ops_module.source_tokens(record["evidenceRevision"]))

    def test_update_scan_status_cli_and_explicit_identity_flags(self):
        parser_args = ops_module.build_parser().parse_args([
            "init-scan-manifest", "--workspace-id", "workspace", "--agent-id", "agent",
            "--project-id", "project", "--runtime-id", "12345678-1234-4234-8234-123456789abc",
            "--round-id", "scan", "--manifest-file", "manifest.json", "--scheduler-revision", "a" * 40])
        self.assertEqual((parser_args.agent_id, parser_args.project_id), ("agent", "project"))
        scan = {"id": "scan", "metadata": {"dailyScan": {"targetDate": "2026-10-21"}}}
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if "issue" in command and "get" in command:
                stdout = json.dumps(scan)
            elif "metadata" in command and "set" in command:
                key = command[command.index("--key") + 1]
                value = json.loads(command[command.index("--value") + 1])
                scan["metadata"][key] = value
                stdout = "{}"
            else:
                stdout = "{}"
            class Result:
                pass
            result = Result(); result.stdout = stdout
            return result
        original = ops_module.subprocess.run
        original_native = ops_module.Native
        ops_module.subprocess.run = runner
        ops_module.Native = lambda server, workspace: original_native(server, workspace, runner=runner)
        self.addCleanup(lambda: setattr(ops_module.subprocess, "run", original))
        self.addCleanup(lambda: setattr(ops_module, "Native", original_native))
        self.assertEqual(ops_module.main([
            "update-scan-status", "--workspace-id", "workspace", "--state-dir", self.temp.name,
            "--round-id", "scan", "--agent-id", "agent", "--source", "intercom",
            "--source-url", "https://intercom.example/1", "--collected", '{"intercom": 1}']), 0)
        self.assertTrue(any("--workspace-id" in call and "workspace" in call for call in calls))
        self.assertEqual(scan["metadata"]["dailyScan"]["collected"], {"intercom": 1})

if __name__ == "__main__":
    unittest.main()
