"""两个调查同时交接、真实启动回读、单一汇总与旧轮隔离的定向回归。"""
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duty_queue_test import add_repair, block, candidate, conclude, freeze, issue, lane_freeze, receipt, record, suspended


spec = importlib.util.spec_from_file_location('duty_dispatch', Path(__file__).parents[1] / 'scripts/duty-dispatch.py')
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)
REVISION = 'a' * 40
SCOPE = dict(round_id='scan', current_issue='scan', current_run='scan-run', agent_id='agent',
             project_id='project', workspace_id='workspace', runtime_id='runtime', revision=REVISION)


def run(issue_id, run_id=None, status='running'):
    return {'id': run_id or issue_id + '-run', 'issue_id': issue_id, 'agent_id': 'agent',
            'workspace_id': 'workspace', 'runtime_id': 'runtime', 'status': status}


def status_event(to_status, *, actor_type='agent', actor_id='agent',
                 created_at='2026-10-23T10:00:00Z', from_status='in_progress'):
    return {'action': 'status_changed', 'actor_type': actor_type, 'actor_id': actor_id,
            'created_at': created_at, 'details': {'from': from_status, 'to': to_status}}


def snapshot(rows):
    scan = issue('scan', dailyScan={'coverageComplete': True}, dailyExecution={
        'schemaVersion': 1, 'policy': dispatch.ISSUE_POLICY, 'investigationConcurrency': 2,
        'repairConcurrency': 1, 'schedulerRevision': REVISION, 'runtimeId': 'runtime'})
    scan.update(assignee_id='agent', status='in_progress')
    freeze(scan, rows)
    issues = [scan, *rows]
    for row in issues:
        row['workspace_id'] = 'workspace'
    return {'agent': {'id': 'agent', 'workspace_id': 'workspace', 'runtime_id': 'runtime',
                      'max_concurrent_tasks': 1}, 'issues': issues, 'runs': [run('scan')]}


class FakeNative:
    def __init__(self, data):
        self.data = data
        self.starts = []
        self.limits = []
        self.failure = None
        self.writes = []

    def snapshot(self, round_id=None):
        return copy.deepcopy(self.data)

    def issue(self, key):
        return copy.deepcopy(next(row for row in self.data['issues'] if row['id'] == key))

    def history(self, key):
        return copy.deepcopy([r for r in self.data['runs'] if r['issue_id'] == key])

    def timeline(self, key):
        return copy.deepcopy(self.data.get('timelines', {}).get(key, []))

    def metadata(self, key, name, value):
        self.writes.append((key, name))
        next(row for row in self.data['issues'] if row['id'] == key)['metadata'][name] = value

    def save_skip(self, issue_id, item, *, allow_write, before_write):
        if allow_write:
            before_write()
        row = next(row for row in self.data['issues'] if row['id'] == issue_id)
        from duty_queue_test import comment
        row['_comments'].append(comment(dispatch.queue.RECORD_MARKER, item, at='2026-10-23T10:00:00Z'))
        self.writes.append((issue_id, 'failureSkip'))

    def concurrency(self, limit):
        self.limits.append(limit)
        self.data['agent']['max_concurrent_tasks'] = limit

    def assign_without_start(self, key):
        next(row for row in self.data['issues'] if row['id'] == key)['assignee_id'] = 'agent'

    def status(self, key, value):
        self.writes.append((key, 'status'))
        next(row for row in self.data['issues'] if row['id'] == key)['status'] = value

    def rerun(self, key):
        self.starts.append(key)
        if self.failure != 'before':
            count = self.starts.count(key)
            self.data['runs'].append(run(key, 'new-' + key + ('' if count == 1 else f'-{count}'), 'queued'))
        if self.failure:
            raise dispatch.Error('模拟超时，不能从退出码推断启动失败')


class RepairAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = dispatch.Journal(Path(self.temp.name) / 'repair.json')
        self.rows = [candidate(key, 'confirmed') for key in ('first', 'second', 'third')]
        self.data = snapshot(self.rows)
        self.scan = self.data['issues'][0]
        freeze(self.scan, self.rows, complete=True)
        self.bugs = add_repair(self.scan, self.rows)
        for bug in self.bugs:
            bug.update(workspace_id='workspace')
        self.bugs[0]['assignee_id'] = 'agent'
        self.data['issues'] += self.bugs
        self.data['runs'] = [run(self.bugs[0]['id'])]
        self.scope = {**SCOPE, 'current_issue': self.bugs[0]['id'],
                      'current_run': self.data['runs'][0]['id']}
        self.native = FakeNative(self.data)

    def advance(self):
        return dispatch.repair_advance(self.native, self.journal, self.scope)

    def test_blocked_current_keeps_result_and_starts_next_once(self):
        block(self.scan, self.bugs[0])
        before = copy.deepcopy(self.bugs[0])
        result = self.advance()
        self.assertEqual((result['action'], result['issueId']), ('dispatch_repair', self.bugs[1]['id']))
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        self.assertEqual(batch['blockedBugIds'], [self.bugs[0]['id']])
        self.assertNotIn(self.bugs[0]['id'], batch.get('deliveredBugIds', []))
        self.assertEqual(self.bugs[0], before)
        self.advance()
        self.assertEqual(self.native.starts, [self.bugs[1]['id']])

    def test_missing_notice_and_manual_pause_never_advance(self):
        block(self.scan, self.bugs[0], notified=False)
        self.assertEqual(self.advance()['action'], 'resume_repair')
        self.assertEqual(self.native.starts, [])
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        batch['blockNotices'][0]['readbackVerified'] = True
        for state in ('paused', 'cancelled'):
            batch['state'] = state
            self.assertEqual(self.advance()['action'], 'attention')
        self.assertEqual(batch['index'], 1)

    def test_new_round_can_advance_a_parent_left_blocked_by_its_current_item(self):
        block(self.scan, self.bugs[0])
        dispatch.queue.meta(self.scan, 'dailyBatch')['state'] = 'blocked'
        self.assertEqual(self.advance()['issueId'], self.bugs[1]['id'])
        self.assertEqual(dispatch.queue.meta(self.scan, 'dailyBatch')['state'], 'running')

    def test_old_revision_keeps_blocked_parent_unchanged(self):
        block(self.scan, self.bugs[0])
        old = '701a7303acb6da9b4ce40e1838aafa0922b8a22a'
        dispatch.queue.meta(self.scan, 'dailyExecution')['schedulerRevision'] = old
        self.scope['revision'] = old
        dispatch.queue.meta(self.scan, 'dailyBatch')['state'] = 'blocked'
        self.assertEqual(self.advance()['action'], 'attention')
        self.assertEqual(self.native.starts, [])

    def test_timeout_after_successful_launch_is_reconciled_without_duplicate(self):
        block(self.scan, self.bugs[0])
        self.native.failure = 'after'
        self.assertEqual(self.advance()['action'], 'dispatch_repair')
        self.advance()
        self.assertEqual(self.native.starts, [self.bugs[1]['id']])

    def test_skip_delivered_successor_preserves_investigations(self):
        block(self.scan, self.bugs[0])
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        batch['completedDeliveries'] = [receipt(self.bugs[1])]
        investigations = copy.deepcopy(self.rows)
        self.assertEqual(self.advance()['issueId'], self.bugs[2]['id'])
        self.assertEqual(dispatch.queue.meta(self.scan, 'dailyBatch')['index'], 3)
        self.assertEqual(self.rows, investigations)
        self.assertEqual(self.native.starts, [self.bugs[2]['id']])

    def test_final_block_closes_batch_without_creating_a_run(self):
        for bug in self.bugs:
            block(self.scan, bug)
        result = self.advance()
        self.assertEqual(result['action'], 'complete_round')
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        self.assertEqual(batch['outcome'], 'finished_with_blocked')
        self.assertEqual(batch['blockedBugIds'], [bug['id'] for bug in self.bugs])
        self.assertIsNone(batch['currentIssueId'])
        self.assertEqual(self.native.starts, [])

    def test_resume_after_cursor_saved_before_launch(self):
        block(self.scan, self.bugs[0])
        original = self.native.metadata
        def interrupted(key, name, value):
            original(key, name, value)
            raise dispatch.Error('保存游标后中断')
        self.native.metadata = interrupted
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.native.metadata = original
        self.assertEqual(self.advance()['issueId'], self.bugs[1]['id'])
        self.assertEqual(self.native.starts, [self.bugs[1]['id']])

    def test_pause_after_cursor_write_prevents_launch(self):
        block(self.scan, self.bugs[0])
        original = self.native.metadata
        def paused(key, name, value):
            original(key, name, copy.deepcopy(value))
            dispatch.queue.meta(self.scan, 'dailyBatch')['state'] = 'paused'
        self.native.metadata = paused
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertEqual(self.native.starts, [])

    def test_unknown_launch_result_does_not_repeat_rerun(self):
        block(self.scan, self.bugs[0])
        self.native.failure = 'before'
        with self.assertRaises(dispatch.Error):
            self.advance()
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertEqual(self.native.starts, [self.bugs[1]['id']])

    def test_human_rework_of_settled_past_item_does_not_block_queue(self):
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        batch.update(index=2, currentIssueId=self.bugs[1]['id'],
                     deliveredBugIds=[self.bugs[0]['id']], completedDeliveries=[receipt(self.bugs[0])])
        self.bugs[0]['metadata']['dailyBug']['state'] = 'running'
        block(self.scan, self.bugs[1])
        self.bugs[1]['assignee_id'] = 'agent'
        self.data['runs'] = [run(self.bugs[0]['id']), run(self.bugs[1]['id'])]
        self.scope.update(current_issue=self.bugs[1]['id'], current_run=self.data['runs'][1]['id'])
        self.assertEqual(self.advance()['issueId'], self.bugs[2]['id'])

    def test_unsettled_future_run_still_prevents_overlap(self):
        block(self.scan, self.bugs[0])
        self.data['runs'].append(run(self.bugs[2]['id']))
        self.assertEqual(self.advance()['action'], 'wait')
        self.assertEqual(self.native.starts, [])

    def test_cancellation_after_snapshot_is_not_overwritten(self):
        for bug in self.bugs:
            block(self.scan, bug)
        original = self.native.issue
        def cancelled(key):
            if key == self.scan['id']:
                self.scan['status'] = 'cancelled'
            return original(key)
        self.native.issue = cancelled
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertEqual(self.native.writes, [])
        self.assertEqual(self.scan['status'], 'cancelled')

    def test_retracted_current_receipt_prevents_cursor_write(self):
        block(self.scan, self.bugs[0])
        original = self.native.issue
        def retracted(key):
            if key == self.bugs[0]['id']:
                self.bugs[0]['metadata']['dailyBug']['state'] = 'running'
            return original(key)
        self.native.issue = retracted
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertEqual(self.native.writes, [])

    def test_invalid_earlier_receipt_cannot_publish_completed(self):
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        batch.update(index=3, currentIssueId=self.bugs[2]['id'],
                     deliveredBugIds=[bug['id'] for bug in self.bugs[:2]],
                     completedDeliveries=[receipt(bug) for bug in self.bugs[:2]])
        self.bugs[0]['metadata']['dailyBug']['deliveryVersion'] += 1
        block(self.scan, self.bugs[2])
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertEqual(dispatch.queue.meta(self.scan, 'dailyBatch')['state'], 'running')
        self.assertEqual(self.native.writes, [])

    def test_lane_finish_is_retryable_after_status_failure_and_after_success(self):
        lanes = lane_freeze(self.scan, ['first', 'second', 'third'], complete=True)
        execution = dispatch.queue.meta(self.scan, 'dailyExecution')
        execution.update(policy=dispatch.LANE_POLICY, laneCount=2)
        for lane in lanes:
            lane.update(workspace_id='workspace', assignee_id='agent')
        self.data['issues'] += lanes
        for bug, key in zip(self.bugs, ('first', 'second', 'third')):
            conclude(lanes[0], key, 'confirmed')
            bug['metadata']['dailyBug']['investigationIssueId'] = 'scan:intercom:' + key
            block(self.scan, bug)
        comments = {row['id']: copy.deepcopy(row.get('_comments', [])) for row in self.data['issues']}
        def loading_snapshot(round_id=None):
            data = copy.deepcopy(self.data)
            for row in data['issues']:
                row.pop('_comments', None)
            dispatch.queue.attach_comments(data['issues'], lambda key: comments[key], round_id=round_id)
            return data
        self.native.snapshot = loading_snapshot
        original = self.native.status
        self.native.status = lambda *_: (_ for _ in ()).throw(dispatch.Error('状态写入失败'))
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertNotEqual(dispatch.queue.meta(self.scan, 'dailyRound')['state'], 'completed')
        self.native.status = original
        self.assertEqual(self.advance()['action'], 'complete_round')
        self.assertEqual(self.advance()['action'], 'complete_round')
        self.assertEqual(self.native.starts, [])

    def test_other_repair_run_prevents_overlap(self):
        block(self.scan, self.bugs[0])
        self.data['issues'].append(issue('other-scan', dailyBatch={
            'state': 'running', 'currentIssueId': self.bugs[2]['id']}))
        self.data['runs'].append(run(self.bugs[2]['id']))
        self.assertEqual(self.advance()['action'], 'wait')
        self.assertEqual(self.native.starts, [])

    def test_repair_reentry_resumes_unassigned_current_issue_without_active_run(self):
        block(self.scan, self.bugs[0])
        dispatch.queue.meta(self.scan, 'dailyBatch').update(
            index=2, currentIssueId=self.bugs[1]['id'], blockedBugIds=[self.bugs[0]['id']])
        self.data['runs'] = [run(self.bugs[0]['id'], status='completed')]

        # 带已结束但错配的 current_run（属于 bugs[0] 却指向 bugs[1]）在任何归属状态下都被拒绝。
        mismatch_scope = {**self.scope, 'current_issue': self.bugs[1]['id']}
        with self.assertRaisesRegex(dispatch.Error, '归属已改变'):
            dispatch.repair_advance(self.native, self.journal, mismatch_scope)
        self.bugs[1]['assignee_id'] = 'agent'
        with self.assertRaisesRegex(dispatch.Error, '真实执行中 run'):
            dispatch.repair_advance(self.native, self.journal, mismatch_scope)
        self.bugs[1]['assignee_id'] = None

        # bugs[1] 自身上一条 run 已失败，或省略 current_run（None）重入时，都会幂等指派并启动 bugs[1]。
        self.data['runs'].append(run(self.bugs[1]['id'], 'bug2-failed', status='failed'))
        failed_scope = {**self.scope, 'current_issue': self.bugs[1]['id'], 'current_run': 'bug2-failed'}
        first = dispatch.repair_advance(self.native, self.journal, failed_scope)
        self.assertEqual((first['action'], first['issueId']), ('dispatch_repair', self.bugs[1]['id']))
        self.assertEqual(self.native.issue(self.bugs[1]['id'])['assignee_id'], 'agent')
        self.assertEqual(self.native.starts, [self.bugs[1]['id']])

        # 已有活动 queued run 时，重入省略 current_run 返回 wait，不重复启动。
        reentry_scope = {**self.scope, 'current_issue': self.bugs[1]['id'], 'current_run': None}
        second = dispatch.repair_advance(self.native, self.journal, reentry_scope)
        self.assertEqual(second['action'], 'wait')
        self.assertEqual(self.native.starts, [self.bugs[1]['id']])

        # 被转交其它 Agent 或存在另一条未结算 bug 的活动 run 时仍保护不强推。
        self.data['runs'] = [run(self.bugs[2]['id'], status='running')]
        blocked = dispatch.repair_advance(self.native, self.journal, reentry_scope)
        self.assertEqual(blocked['action'], 'wait')
        self.data['runs'] = []
        self.bugs[1]['assignee_id'] = 'other-agent'
        with self.assertRaisesRegex(dispatch.Error, '归属已改变'):
            dispatch.repair_advance(self.native, self.journal, reentry_scope)

    def test_cli_repair_advance_omits_current_run_and_plan_passes_round_id(self):
        block(self.scan, self.bugs[0])
        dispatch.queue.meta(self.scan, 'dailyBatch').update(
            index=2, currentIssueId=self.bugs[1]['id'], blockedBugIds=[self.bugs[0]['id']])
        self.data['runs'] = []
        seen_rounds = []
        orig_snapshot = self.native.snapshot
        def spy_snapshot(round_id=None):
            seen_rounds.append(round_id)
            return orig_snapshot(round_id=round_id)
        self.native.snapshot = spy_snapshot

        common_argv = [
            '--server-url', 'https://multica.example',
            '--workspace-id', 'workspace', '--project-id', 'project',
            '--agent-id', 'agent', '--runtime-id', 'runtime',
            '--round-id', 'scan', '--revision', REVISION,
        ]
        repair_argv = ['duty-dispatch.py', 'repair-advance', *common_argv, '--current-issue', self.bugs[1]['id']]
        plan_argv = ['duty-dispatch.py', 'plan', *common_argv, '--current-issue', 'scan', '--current-run', 'scan-run']

        with (patch.object(dispatch, 'Native', return_value=self.native),
              patch.object(Path, 'home', return_value=Path(self.temp.name))):
            buf = io.StringIO()
            with patch('sys.argv', repair_argv), contextlib.redirect_stdout(buf):
                rc = dispatch.main()
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual((payload['action'], payload['issueId']), ('dispatch_repair', self.bugs[1]['id']))

            # 切换 snapshot 验证 plan 命令与 advance 都会传递 round_id。
            self.data['runs'] = [run('scan')]
            buf = io.StringIO()
            with patch('sys.argv', plan_argv), contextlib.redirect_stdout(buf):
                rc = dispatch.main()
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())['action'], 'finalize_round_here')
            self.assertEqual(
                dispatch.advance(self.native, self.journal, {**SCOPE, 'current_issue': 'scan', 'current_run': 'scan-run'})['action'],
                'finalize_round_here',
            )

        self.assertEqual(seen_rounds, ['scan', 'scan', 'scan'])

    def test_agent_closed_scan_reopens_in_repair_while_member_closed_stays_stopped(self):
        block(self.scan, self.bugs[0])
        self.scan['status'] = 'done'
        self.data['timelines'] = {'scan': [status_event('done', actor_type='agent', actor_id='agent')]}
        result = self.advance()
        self.assertEqual((result['action'], result['issueId']), ('dispatch_repair', self.bugs[1]['id']))
        self.assertEqual(self.scan['status'], 'in_progress')

        self.scan['status'] = 'done'
        self.data['timelines']['scan'].append(
            status_event('done', actor_type='member', actor_id='winches', created_at='2026-10-23T11:00:00Z'))
        with self.assertRaises(dispatch.Error):
            self.advance()
        self.assertEqual(self.scan['status'], 'done')

    def test_agent_closed_verified_bug_restores_status_and_unverified_or_human_stays_attention(self):
        # 1. 已核验交付却被 Agent 误置 done -> 恢复为 in_review 并推进下一项。
        batch = dispatch.queue.meta(self.scan, 'dailyBatch')
        batch['completedDeliveries'] = [receipt(self.bugs[0])]
        self.bugs[0]['status'] = 'done'
        self.data['timelines'] = {self.bugs[0]['id']: [status_event('done')]}
        first = self.advance()
        self.assertEqual((first['action'], first['issueId']), ('dispatch_repair', self.bugs[1]['id']))
        self.assertEqual(self.bugs[0]['status'], 'in_review')

        # 2. 已核验受阻却被 Agent 误置 done -> 恢复为 blocked 并推进下一项。
        block(self.scan, self.bugs[1])
        self.bugs[1]['assignee_id'] = 'agent'
        self.bugs[1]['status'] = 'done'
        self.data['timelines'][self.bugs[1]['id']] = [status_event('done')]
        self.data['runs'] = [run(self.bugs[1]['id'])]
        second_scope = {**self.scope, 'current_issue': self.bugs[1]['id'],
                        'current_run': self.data['runs'][0]['id']}
        second = dispatch.repair_advance(self.native, self.journal, second_scope)
        self.assertEqual((second['action'], second['issueId']), ('dispatch_repair', self.bugs[2]['id']))
        self.assertEqual(self.bugs[1]['status'], 'blocked')

        # 3. 尚未形成交付或受阻回执时被置 done -> 保持 attention，不伪造状态。
        self.bugs[2]['assignee_id'] = 'agent'
        self.bugs[2]['status'] = 'done'
        self.data['timelines'][self.bugs[2]['id']] = [status_event('done')]
        self.data['runs'] = [run(self.bugs[2]['id'])]
        third_scope = {**self.scope, 'current_issue': self.bugs[2]['id'],
                       'current_run': self.data['runs'][0]['id']}
        third = dispatch.repair_advance(self.native, self.journal, third_scope)
        self.assertEqual(third['action'], 'attention')
        self.assertEqual(self.bugs[2]['status'], 'done')

        # 4. 真人关闭已核验交付的任务 -> 保持 attention，不自动重开。
        batch['completedDeliveries'].append(receipt(self.bugs[2]))
        self.data['timelines'][self.bugs[2]['id']].append(
            status_event('done', actor_type='member', actor_id='winches', created_at='2026-10-23T11:00:00Z'))
        fourth = dispatch.repair_advance(self.native, self.journal, third_scope)
        self.assertEqual(fourth['action'], 'attention')
        self.assertEqual(self.bugs[2]['status'], 'done')


class PlanTests(unittest.TestCase):
    def test_available_slots_are_filled_without_exceeding_two(self):
        data = snapshot([candidate(str(i)) for i in range(4)])
        for occupied in range(3):
            with self.subTest(occupied=occupied):
                data['runs'] = [run('scan'), *[run(str(i)) for i in range(occupied)]]
                result = dispatch.plan(data, **SCOPE)
                self.assertEqual(result['issueIds'], [str(i) for i in range(occupied, 2)])

    def test_draining_conclusions_do_not_leave_the_next_slot_empty(self):
        data = snapshot([candidate('first', 'excluded'), candidate('second', 'confirmed'), candidate('third')])
        data['runs'] += [run('first'), run('second')]
        result = dispatch.plan(data, **SCOPE)
        self.assertEqual((result['completed'], result['remaining'], result['issueIds']), (2, 1, ['third']))

    def test_all_conclusions_return_to_original_scan_before_repair(self):
        for rows in ([], [candidate(state='confirmed')], [suspended()]):
            with self.subTest(rows=len(rows)):
                data = snapshot(rows)
                result = dispatch.plan(data, **SCOPE)
                self.assertEqual(result['action'], 'finalize_round_here')
                self.assertFalse(data['issues'][0]['metadata']['dailyRound']['investigationComplete'])

    def test_missing_evidence_is_still_pending_and_never_becomes_delivery(self):
        row = suspended()
        del row['metadata']['dailyInvestigation']['blocker']['attempts']['verification']
        data = snapshot([row])
        result = dispatch.plan(data, **SCOPE)
        self.assertEqual((result['remaining'], result['issueIds']), (1, ['external']))
        self.assertNotIn('dailyBug', row['metadata'])

    def test_legacy_round_and_changed_version_or_runtime_cannot_be_migrated(self):
        original = snapshot([candidate()])
        for key, value in [('dailyExecution', None), ('schedulerRevision', 'b' * 40), ('runtimeId', 'other')]:
            data = copy.deepcopy(original)
            if key == 'dailyExecution':
                del data['issues'][0]['metadata'][key]
            else:
                data['issues'][0]['metadata']['dailyExecution'][key] = value
            before = copy.deepcopy(data)
            with self.subTest(key=key), self.assertRaises(dispatch.Error):
                dispatch.plan(data, **SCOPE)
            self.assertEqual(data, before)

    def test_wrong_scope_transfer_or_duplicate_run_fails_closed(self):
        data = snapshot([candidate()])
        variants = []
        for key in ('agent_id', 'runtime_id', 'workspace_id', 'issue_id'):
            changed = copy.deepcopy(data)
            changed['runs'][0][key] = 'other'
            variants.append(changed)
        changed = copy.deepcopy(data)
        changed['issues'][1]['parent_issue_id'] = 'other'
        variants.append(changed)
        changed = copy.deepcopy(data)
        changed['issues'][0]['assignee_id'] = 'other'
        variants.append(changed)
        changed = copy.deepcopy(data)
        changed['runs'].append(run('scan', 'duplicate-run'))
        variants.append(changed)
        for index, changed in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(dispatch.Error):
                dispatch.plan(changed, **SCOPE)

    def test_paused_cancelled_or_unregistered_candidates_are_not_skipped_or_reopened(self):
        for state in ('paused', 'cancelled'):
            with self.subTest(state=state), self.assertRaises(dispatch.Error):
                dispatch.plan(snapshot([candidate(state=state)]), **SCOPE)
        data = snapshot([candidate()])
        data['issues'][0]['metadata']['dailyRound']['candidateCount'] = 2
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)

    def test_registration_and_coverage_cannot_be_bypassed(self):
        data = snapshot([candidate()])
        data['issues'][0]['metadata']['dailyRound']['registrationComplete'] = False
        self.assertEqual(dispatch.plan(data, **SCOPE)['action'], 'register_round')
        data['agent']['max_concurrent_tasks'] = 5
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)
        data = snapshot([candidate()])
        data['issues'][0]['metadata']['dailyScan']['coverageComplete'] = False
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)

    def test_early_repair_or_complete_marker_cannot_hide_pending_work(self):
        for key, value in [('dailyBatch', {'bugIds': ['early-bug']}),
                           ('dailyRound', {'investigationComplete': True})]:
            data = snapshot([candidate()])
            data['issues'][0]['metadata'].setdefault(key, {}).update(value)
            with self.subTest(key=key), self.assertRaises(dispatch.Error):
                dispatch.plan(data, **SCOPE)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def advance(self, native, scope=SCOPE):
        with dispatch.locked(self.root / 'dispatch.lock'):
            return dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), scope)

    def test_concurrent_completions_fill_two_slots_once_and_keep_latest_progress(self):
        data = snapshot([candidate('1', 'excluded'), candidate('2', 'confirmed'),
                         candidate('3'), candidate('4'), candidate('5')])
        data['runs'] = [run('1'), run('2')]
        for row in data['issues'][1:3]:
            row['assignee_id'] = 'agent'
        native = FakeNative(data)
        scopes = [{**SCOPE, 'current_issue': key, 'current_run': key + '-run'} for key in ('1', '2')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda scope: self.advance(native, scope), scopes))
        self.assertEqual(native.starts, ['3', '4'])
        self.assertEqual([r['action'] for r in results], ['dispatch', 'dispatch'])
        self.assertEqual(native.issue('scan')['metadata']['dailyDispatch']['completed'], 2)
        # 调度器不写任何调查结论、父轮完成标记或修复批次。
        self.assertEqual([key for _, key in native.writes if key != 'dailyDispatch'],
                         ['dailyInvestigation', 'dailyInvestigation'])
        self.assertFalse(native.issue('scan')['metadata']['dailyRound']['investigationComplete'])

    def test_last_two_workers_enqueue_one_finalizer_without_changing_concurrency(self):
        data = snapshot([candidate('first', 'excluded'), candidate('second', 'confirmed')])
        data['runs'] = [run('first'), run('second')]
        data['agent']['max_concurrent_tasks'] = 2
        for row in data['issues'][1:]:
            row['assignee_id'] = 'agent'
        native = FakeNative(data)
        scopes = [{**SCOPE, 'current_issue': key, 'current_run': key + '-run'} for key in ('first', 'second')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda scope: self.advance(native, scope), scopes))
        self.assertEqual(native.starts, ['scan'])
        self.assertEqual((native.data['agent']['max_concurrent_tasks'], native.limits), (2, []))
        self.assertTrue(all(r['action'] == 'finalize_round' for r in results))
        self.assertEqual(results[0]['receipts'][0]['runId'], results[1]['receipts'][0]['runId'])
        self.assertFalse(native.issue('scan')['metadata']['dailyRound']['investigationComplete'])

    def test_finalizer_does_not_rerun_itself(self):
        native = FakeNative(snapshot([candidate(state='excluded')]))
        result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('finalize_round_here', []))

    def test_last_worker_queues_a_successor_while_original_scan_is_exiting(self):
        data = snapshot([candidate('last', 'confirmed')])
        data['issues'][1]['assignee_id'] = 'agent'
        data['runs'].append(run('last'))
        native = FakeNative(data)
        worker = {**SCOPE, 'current_issue': 'last', 'current_run': 'last-run'}
        result = self.advance(native, worker)
        self.assertEqual((result['action'], native.starts), ('finalize_round', ['scan']))
        self.assertEqual(result['receipts'][0]['runId'], 'new-scan')
        self.assertEqual(native.data['runs'][0]['status'], 'running')
        # 旧 scan 的最后一次退出检查发生在后继排队后，只让出，不做第二次汇总。
        self.assertEqual(self.advance(native)['action'], 'yield_to_finalizer')
        self.advance(native, worker)
        self.assertEqual(native.starts, ['scan'])
        for old in native.data['runs'][:2]:
            old['status'] = 'completed'
        native.data['runs'][-1]['status'] = 'running'
        finalizer = {**SCOPE, 'current_run': 'new-scan'}
        self.assertEqual(self.advance(native, finalizer)['action'], 'finalize_round_here')
        self.assertEqual(native.issue('scan')['metadata']['dailyDispatch']['finalizerRunId'], 'new-scan')
        serial = dict(project_id='project', agent_id='agent', current_issue_id='scan')
        self.assertEqual(dispatch.queue.next_action(data['issues'], data['runs'], **serial)['action'],
                         'finalize_round')
        data['issues'][0]['metadata']['dailyRound'].update(
            state='ready_repair', investigationComplete=True, investigationIndex=record('index'))
        self.assertEqual(dispatch.queue.next_action(data['issues'], data['runs'], **serial)['action'],
                         'prepare_round_repair')

    def test_original_scan_with_draining_worker_queues_one_successor_and_exits(self):
        data = snapshot([candidate('last', 'excluded')])
        data['issues'][1]['assignee_id'] = 'agent'
        data['runs'].append(run('last'))
        native = FakeNative(data)
        result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('finalize_round', ['scan']))
        self.assertNotIn('dailyDispatch', native.issue('scan')['metadata'])
        self.assertEqual(self.advance(native)['action'], 'yield_to_finalizer')
        worker = {**SCOPE, 'current_issue': 'last', 'current_run': 'last-run'}
        self.advance(native, worker)
        self.assertEqual(native.starts, ['scan'])

    def test_expired_or_incomplete_conclusions_are_reinvestigated(self):
        for row in (candidate('bug', 'confirmed'), suspended()):
            row['metadata']['dailyInvestigation']['investigationRecord']['evidenceRevision'] = 'old'
            native = FakeNative(snapshot([row]))
            with self.subTest(issue=row['id']):
                result = self.advance(native)
                self.assertEqual((result['action'], native.starts), ('dispatch', [row['id']]))
                self.assertEqual(native.issue(row['id'])['metadata']['dailyInvestigation']['state'], 'investigating')

    def test_unknown_start_is_recovered_from_real_run_and_not_repeated(self):
        native = FakeNative(snapshot([candidate('first')]))
        native.failure = 'after'
        result = self.advance(native)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(result['receipts'][0]['runId'], 'new-first')
        self.advance(native)
        self.assertEqual(native.starts, ['first'])

    def test_unconfirmed_start_keeps_intent_and_never_repeats_write(self):
        native = FakeNative(snapshot([candidate('first')]))
        native.failure = 'before'
        self.assertEqual(self.advance(native)['action'], 'attention')
        self.assertEqual(self.advance(native)['action'], 'attention')
        self.assertEqual(native.starts, ['first'])
        self.assertNotIn('investigationRecord', native.issue('first')['metadata']['dailyInvestigation'])

    def test_failed_run_is_not_a_success_receipt_or_a_new_automatic_start(self):
        native = FakeNative(snapshot([candidate('first')]))
        self.advance(native)
        native.data['runs'][-1]['status'] = 'failed'
        result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('attention', ['first']))

    def test_legacy_round_never_changes_concurrency_or_starts_a_run(self):
        native = FakeNative(snapshot([candidate()]))
        del native.data['issues'][0]['metadata']['dailyExecution']
        with self.assertRaises(dispatch.Error):
            self.advance(native)
        self.assertEqual((native.starts, native.writes), ([], []))

    def test_metadata_mismatch_stops_launch_and_keeps_other_slot_eligible(self):
        native = FakeNative(snapshot([candidate('first')]))
        original = native.metadata
        def save(key, name, value):
            if key != 'scan':
                raise dispatch.Error('写后回读不一致')
            original(key, name, value)
        with patch.object(native, 'metadata', side_effect=save):
            result = self.advance(native)
        self.assertEqual((result['action'], native.starts), ('attention', []))


def lane_snapshot(keys, other_round=False):
    scan = issue('scan', dailyScan={'coverageComplete': True}, dailyExecution={
        'schemaVersion': 1, 'policy': dispatch.LANE_POLICY, 'laneCount': 2,
        'repairConcurrency': 1, 'schedulerRevision': REVISION, 'runtimeId': 'runtime'})
    scan.update(assignee_id='agent', status='in_progress')
    lanes = lane_freeze(scan, keys)
    issues = [scan, *lanes]
    if other_round:
        # 前一轮仍在修复：不再挡住本轮调查。
        old = issue('old', dailyRound={'state': 'repairing'}, dailyBatch={'state': 'running', 'bugIds': ['x']})
        issues.append(old)
    for row in issues:
        row['workspace_id'] = 'workspace'
        row['assignee_id'] = 'agent'
    return {'agent': {'id': 'agent', 'workspace_id': 'workspace', 'runtime_id': 'runtime',
                      'max_concurrent_tasks': 2}, 'issues': issues, 'runs': [run('scan')]}


class LaneDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def advance(self, native, scope=SCOPE):
        with dispatch.locked(self.root / 'dispatch.lock'):
            return dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), scope)

    def lane(self, native, number):
        return next(row for row in native.data['issues'] if row['id'] == f'lane-{number}')

    def finish(self, native, number, key, state='excluded'):
        """模拟工位运行：执行中的工位写好结论评论后，由同一运行交接。"""
        conclude(self.lane(native, number), key, state)
        current = next(item for item in native.data['runs'] if item['issue_id'] == f'lane-{number}'
                       and item['status'] in {'queued', 'running'})
        current['status'] = 'running'
        return {**SCOPE, 'current_issue': f'lane-{number}', 'current_run': current['id']}

    def test_scan_claims_one_candidate_per_lane_even_when_another_round_is_repairing(self):
        native = FakeNative(lane_snapshot(['a', 'b', 'c'], other_round=True))
        result = self.advance(native)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts, ['lane-1', 'lane-2'])
        self.assertEqual([self.lane(native, n)['metadata']['dailyLane']['currentCandidateId'] for n in (1, 2)],
                         ['intercom:a', 'intercom:b'])
        self.assertEqual(native.limits, [])

    def test_lane_reads_its_claim_and_cannot_hand_off_before_a_conclusion(self):
        native = FakeNative(lane_snapshot(['a', 'b', 'c']))
        self.advance(native)
        lane_run = next(item for item in native.data['runs'] if item['issue_id'] == 'lane-1')
        lane_run['status'] = 'running'
        scope = {**SCOPE, 'current_issue': 'lane-1', 'current_run': lane_run['id']}
        result = dispatch.plan(native.snapshot(), **scope)
        self.assertEqual((result['action'], result['candidateId']), ('investigate_candidate', 'intercom:a'))
        with self.assertRaises(dispatch.Error):
            self.advance(native, scope)

    def test_concluded_lane_claims_next_and_queues_only_its_own_successor(self):
        native = FakeNative(lane_snapshot(['a', 'b', 'c']))
        self.advance(native)
        native.data['runs'][-1]['status'] = 'running'
        result = self.advance(native, self.finish(native, 1, 'a'))
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts, ['lane-1', 'lane-2', 'lane-1'])
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')
        self.assertEqual(self.lane(native, 2)['metadata']['dailyLane']['currentCandidateId'], 'intercom:b')
        self.assertEqual(result['receipts'][0]['status'], 'queued')

    def test_last_conclusions_hand_back_to_one_finalizer(self):
        native = FakeNative(lane_snapshot(['a', 'b']))
        self.advance(native)
        native.data['runs'][-1]['status'] = 'running'
        first = self.advance(native, self.finish(native, 1, 'a'))
        self.assertEqual((first['action'], first['released']), ('dispatch', ['lane-1']))
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['state'], 'done')
        # 仍有未完成候选时原扫描只核对派发，不重复领取。
        idle = self.advance(native)
        self.assertEqual((idle['action'], idle['claims'], idle['issueIds']), ('dispatch', [], []))
        scope = self.finish(native, 2, 'b', 'confirmed')
        last = self.advance(native, scope)
        self.assertEqual((last['action'], native.starts[-1]), ('finalize_round', 'scan'))
        # 汇总后继在工位退出前开跑：再排一个自身后继，不在两边都退出后无人汇总。
        native.data['runs'][0]['status'] = 'completed'
        successor = next(item for item in native.data['runs'] if item['id'] == last['receipts'][0]['runId'])
        successor['status'] = 'running'
        again = self.advance(native, {**SCOPE, 'current_run': successor['id']})
        self.assertEqual((again['action'], native.starts.count('scan')), ('finalize_round', 2))
        for item in native.data['runs']:
            if item['issue_id'] != 'scan' or item['id'] == successor['id']:
                item['status'] = 'completed'
        final = next(item for item in native.data['runs'] if item['id'] == again['receipts'][0]['runId'])
        final['status'] = 'running'
        done = self.advance(native, {**SCOPE, 'current_run': final['id']})
        self.assertEqual(done['action'], 'finalize_round_here')
        self.assertEqual([(self.lane(native, n)['status'], self.lane(native, n)['metadata']['dailyLane']['state'])
                          for n in (1, 2)], [('done', 'done'), ('done', 'done')])

    def test_duplicate_claims_fail_closed(self):
        data = lane_snapshot(['a', 'b'])
        for number in (1, 2):
            data['issues'][number]['metadata']['dailyLane']['currentCandidateId'] = 'intercom:a'
        with self.assertRaises(dispatch.Error):
            dispatch.plan(data, **SCOPE)


class LaneFailureTests(LaneDispatchTests):
    def start(self, keys=('a', 'b', 'c')):
        data = lane_snapshot(list(keys))
        data['issues'][0]['metadata']['dailyExecution']['investigationFailurePolicy'] = dispatch.queue.FAILURE_POLICY
        native = FakeNative(data)
        self.advance(native)
        return native

    def failed_scope(self, native):
        latest = native.history('lane-1')[-1]
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'running' if value['id'] == latest['id'] else 'completed'
        lane = self.lane(native, 1)
        from duty_queue_test import comment
        item = candidate('a', 'investigating')['metadata']['dailyInvestigation']
        item.update(executionFailure={'kind': 'source_access_failed', 'error': 'channel_not_found'},
                    nextAction='恢复线程访问或取得安全对象 ID 后续查')
        lane['_comments'].append(comment(dispatch.queue.RECORD_MARKER, item, at='2026-10-23T09:00:00Z'))
        return {**SCOPE, 'current_issue': 'lane-1', 'current_run': latest['id']}

    def test_failure_retries_twice_then_skips_and_claims_next_without_blocking_other_lane(self):
        native = self.start()
        for attempt in range(2):
            scope = self.failed_scope(native)
            result = self.advance(native, scope)
            self.assertEqual((result['action'], native.starts.count('lane-1')), ('dispatch', attempt + 2))
            self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:a')
            # 同一次失败交接重复调用，复用同一个排队重试。
            self.advance(native, scope)
            self.assertEqual(native.starts.count('lane-1'), attempt + 2)
        scope = self.failed_scope(native)
        result = self.advance(native, scope)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts.count('lane-1'), 4)  # 三次 a，接着 c。
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')
        self.assertEqual(self.lane(native, 2)['metadata']['dailyLane']['currentCandidateId'], 'intercom:b')
        self.assertEqual(native.starts.count('lane-2'), 1)
        rows, pending = dispatch.queue.round_candidates(native.data['issues'][0], native.data['issues'], 'agent')
        skipped = rows[0]['metadata']['dailyInvestigation']
        self.assertEqual(skipped['state'], 'skipped_failure')
        self.assertEqual(len(skipped['failureSkip']['runIds']), 3)
        self.assertEqual(skipped['failureSkip']['reason'], 'channel_not_found')
        self.assertNotIn('verification', skipped)
        self.assertEqual(len(pending), 2)
        self.assertEqual(native.writes.count(('lane-1', 'failureSkip')), 1)

    def test_other_lane_recovers_proven_finished_run_and_exhausted_crash_is_skipped(self):
        native = self.start()
        for attempt in range(3):
            for value in native.data['runs']:
                if value['issue_id'] == 'lane-1':
                    value['status'] = 'failed'
            self.advance(native)
        rows, _ = dispatch.queue.round_candidates(native.data['issues'][0], native.data['issues'], 'agent')
        item = rows[0]['metadata']['dailyInvestigation']
        self.assertEqual(item['state'], 'skipped_failure')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')
        self.assertNotIn('investigationRecord', item)  # 崩溃无报告仍保留三次运行，不假造报告。

    def test_unknown_start_never_retries_or_skips(self):
        native = self.start()
        native.failure = 'before'
        scope = self.failed_scope(native)
        self.assertEqual(self.advance(native, scope)['action'], 'attention')
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        self.assertEqual(self.advance(native)['action'], 'attention')
        self.assertEqual(native.starts.count('lane-1'), 2)
        self.assertNotIn(('lane-1', 'failureSkip'), native.writes)

    def test_retry_timeout_with_real_run_is_reused(self):
        native = self.start()
        native.failure = 'after'
        scope = self.failed_scope(native)
        self.assertEqual(self.advance(native, scope)['action'], 'dispatch')
        self.advance(native, scope)
        self.assertEqual(native.starts.count('lane-1'), 2)

    def test_unrecorded_live_failure_cannot_release_or_retry(self):
        native = self.start()
        native.data['runs'][1]['status'] = 'running'
        scope = {**SCOPE, 'current_issue': 'lane-1', 'current_run': native.data['runs'][1]['id']}
        result = self.advance(native, scope)
        self.assertEqual(result['action'], 'attention')
        self.assertEqual(native.starts.count('lane-1'), 1)

    def test_recover_requires_idle_target_and_preserves_collection_receipts(self):
        native = self.start()
        recovery = {**SCOPE, 'current_issue': 'lane-1', 'current_run': None}
        with self.assertRaises(dispatch.Error):
            dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)
        native.data['runs'][1]['status'] = 'completed'
        before = copy.deepcopy(native.data['issues'][0]['metadata']['dailyScan'])
        result = dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(native.starts.count('lane-1'), 2)
        self.assertEqual(native.data['issues'][0]['metadata']['dailyScan'], before)
        with self.assertRaises(dispatch.Error):
            dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)

    def test_manual_cancel_is_preserved_instead_of_retried(self):
        native = self.start()
        native.data['runs'][1]['status'] = 'cancelled'
        result = self.advance(native)
        self.assertEqual(result['action'], 'attention')
        self.assertEqual(native.starts.count('lane-1'), 1)

    def test_last_failed_candidate_returns_to_finalizer_after_skip(self):
        native = self.start(('a',))
        for attempt in range(3):
            scope = self.failed_scope(native)
            result = self.advance(native, scope)
        self.assertEqual(result['action'], 'finalize_round')
        self.assertEqual(native.starts.count('lane-1'), 3)
        self.assertEqual(native.starts[-1], 'scan')

    def test_skip_preread_failure_can_still_send_once_after_read_recovers(self):
        native = self.start()
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        actual = native.save_skip
        with patch.object(native, 'save_skip', side_effect=dispatch.Error('首次评论只读失败')):
            self.assertEqual(self.advance(native, scope)['action'], 'attention')
        with patch.object(native, 'save_skip', wraps=actual) as save:
            self.assertEqual(self.advance(native, scope)['action'], 'dispatch')
        self.assertTrue(save.call_args.kwargs['allow_write'])
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')

    def test_operator_recovery_after_exhaustion_queues_only_agent_settlement(self):
        native = self.start()
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        recovery = {**scope, 'current_run': None}
        result = dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'), recovery, recovering=True)
        self.assertEqual(native.starts.count('lane-1'), 4)  # 第四次仅留档，不调查。
        self.assertNotIn(('lane-1', 'failureSkip'), native.writes)
        current = native.data['runs'][-1]
        current['status'] = 'running'
        scope = {**scope, 'current_run': current['id']}
        self.assertEqual(dispatch.plan(native.snapshot(), **scope)['action'], 'settle_failure')
        self.assertEqual(self.advance(native, scope)['action'], 'dispatch')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')

    def test_other_lane_unknown_intent_cannot_block_released_lane_dispatch(self):
        native = self.start(('a', 'b', 'c', 'd'))
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        other_scope = self.finish(native, 2, 'b')
        # lane2 的 c 启动未知，但 lane1 的 a 耗尽后仍应领取 d。
        original = native.rerun
        def fail_other(key):
            if key == 'lane-2':
                native.starts.append(key)
                raise dispatch.Error('启动结果未知')
            original(key)
        with patch.object(native, 'rerun', side_effect=fail_other):
            result = self.advance(native, other_scope)
        self.assertEqual(result['action'], 'attention')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:d')
        self.assertTrue(any(r['issueId'] == 'lane-1' for r in result['receipts']))
        self.assertTrue(any(r['issueId'] == 'lane-2' for r in result['errors']))

    def test_skip_comment_unknown_result_is_not_sent_again(self):
        native = self.start()
        for attempt in range(2):
            self.advance(native, self.failed_scope(native))
        scope = self.failed_scope(native)
        calls = []
        def fail(issue_id, item, *, allow_write, before_write):
            calls.append(allow_write)
            if allow_write:
                before_write()
            raise dispatch.Error('评论结果不明')
        with patch.object(native, 'save_skip', side_effect=fail):
            self.assertEqual(self.advance(native, scope)['action'], 'attention')
            self.assertEqual(self.advance(native, scope)['action'], 'attention')
        self.assertEqual(calls, [True, False])
        self.assertEqual(native.starts.count('lane-1'), 3)

    def test_agent_closed_lane_reopens_on_advance_and_recover(self):
        native = self.start(('a', 'b', 'c', 'd'))
        # 1. lane-1 自己把工位卡误置为 done 后交接 -> advance 核验时间线后自动重开并继续领取 c。
        scope1 = self.finish(native, 1, 'a')
        self.lane(native, 1)['status'] = 'done'
        native.data['timelines'] = {'lane-1': [status_event('done', actor_type='agent', actor_id='agent')]}
        res1 = self.advance(native, scope1)
        self.assertEqual(res1['action'], 'dispatch')
        self.assertEqual(self.lane(native, 1)['status'], 'in_progress')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:c')

        # 2. lane-1 再次把工位卡误置 done 且运行退出后 -> recover 核验时间线后自动重开并重派 d。
        for value in native.data['runs']:
            if value['issue_id'] == 'lane-1':
                value['status'] = 'completed'
        conclude(self.lane(native, 1), 'c', 'excluded')
        self.lane(native, 1)['status'] = 'done'
        native.data['timelines']['lane-1'].append(
            status_event('done', actor_type='agent', actor_id='agent', created_at='2026-10-23T10:30:00Z'))
        rec = dispatch.advance(native, dispatch.Journal(self.root / 'journal.json'),
                               {**SCOPE, 'current_issue': 'lane-1', 'current_run': None}, recovering=True)
        self.assertEqual(rec['action'], 'dispatch')
        self.assertEqual(self.lane(native, 1)['status'], 'in_progress')
        self.assertEqual(self.lane(native, 1)['metadata']['dailyLane']['currentCandidateId'], 'intercom:d')

    def test_member_closed_or_unverified_timeline_lane_stays_stopped(self):
        cases = [
            ('member_latest', [status_event('done', actor_type='agent', actor_id='agent', created_at='2026-10-23T09:00:00Z'),
                               status_event('in_progress', actor_type='member', actor_id='winches', created_at='2026-10-23T09:30:00Z', from_status='done'),
                               status_event('done', actor_type='member', actor_id='winches', created_at='2026-10-23T10:00:00Z')]),
            ('other_agent', [status_event('done', actor_type='agent', actor_id='other-agent')]),
            ('empty_timeline', []),
        ]
        prev_root = self.root
        try:
            for label, events in cases:
                with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                    self.root = Path(tmp)
                    native = self.start(('a', 'b', 'c'))
                    scope = self.finish(native, 1, 'a')
                    self.lane(native, 1)['status'] = 'done'
                    native.data['timelines'] = {'lane-1': events}
                    res = self.advance(native, scope)
                    self.assertEqual(res['action'], 'attention')
                    self.assertEqual(self.lane(native, 1)['status'], 'done')
                    self.assertEqual(native.starts.count('lane-1'), 1)

            # timeline CLI 查询失败同样 fail-closed。
            with tempfile.TemporaryDirectory() as tmp:
                self.root = Path(tmp)
                native = self.start(('a', 'b', 'c'))
                scope = self.finish(native, 1, 'a')
                self.lane(native, 1)['status'] = 'done'
                with patch.object(native, 'timeline', side_effect=dispatch.Error('timeline unavailable')):
                    res = self.advance(native, scope)
                self.assertEqual(res['action'], 'attention')
                self.assertEqual(self.lane(native, 1)['status'], 'done')
        finally:
            self.root = prev_root

    def test_agent_closed_scan_reopens_on_advance_while_member_closed_stays_stopped(self):
        native = self.start(('a', 'b'))
        s1 = self.finish(native, 1, 'a')
        self.advance(native, s1)
        s2 = self.finish(native, 2, 'b')
        native.data['issues'][0]['status'] = 'done'
        native.data['timelines'] = {'scan': [status_event('done', actor_type='agent', actor_id='agent')]}
        res = self.advance(native, s2)
        self.assertEqual(res['action'], 'finalize_round')
        self.assertEqual(native.issue('scan')['status'], 'in_progress')

        # 真人关闭 scan 时即使触发 advance 也保持停止。
        native.data['issues'][0]['status'] = 'done'
        native.data['timelines']['scan'].append(
            status_event('done', actor_type='member', actor_id='winches', created_at='2026-10-23T11:00:00Z'))
        with self.assertRaises(dispatch.Error):
            self.advance(native)
        self.assertEqual(native.issue('scan')['status'], 'done')


class ReleaseRepairTests(unittest.TestCase):
    def test_waiting_round_is_started_only_after_the_other_batch_finishes(self):
        data = lane_snapshot(['a'])
        scan, lanes = data['issues'][0], data['issues'][1:3]
        lane_freeze(scan, ['a'], complete=True)
        conclude(lanes[0], 'a', 'confirmed')
        old = issue('old', dailyRound={'state': 'repairing'},
                    dailyBatch={'mode': 'scan', 'state': 'running', 'bugIds': ['x'], 'total': 1, 'index': 1,
                                'currentIssueId': 'x'})
        data['issues'].append(old)
        native = FakeNative(data)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        journal = dispatch.Journal(Path(temp.name) / 'release.json')
        kwargs = dict(agent_id='agent', runtime_id='runtime', project_id='project')
        self.assertEqual(dispatch.release_repair(native, journal, **kwargs)['action'], 'wait')
        old['metadata']['dailyBatch']['state'] = 'completed'
        result = dispatch.release_repair(native, journal, **kwargs)
        self.assertEqual((result['action'], native.starts), ('released', ['scan']))

    def test_agent_closed_waiting_round_reopens_in_release_repair_while_member_closed_stays_skipped(self):
        data = lane_snapshot(['a'])
        scan, lanes = data['issues'][0], data['issues'][1:3]
        lane_freeze(scan, ['a'], complete=True)
        conclude(lanes[0], 'a', 'confirmed')
        scan['status'] = 'done'
        data['timelines'] = {'scan': [status_event('done', actor_type='member', actor_id='winches')]}
        native = FakeNative(data)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        kwargs = dict(agent_id='agent', runtime_id='runtime', project_id='project')
        idle = dispatch.release_repair(native, dispatch.Journal(Path(temp.name) / 'release1.json'), **kwargs)
        self.assertEqual((idle['action'], scan['status'], native.starts), ('idle', 'done', []))

        data['timelines']['scan'].append(
            status_event('done', actor_type='agent', actor_id='agent', created_at='2026-10-23T11:00:00Z'))
        released = dispatch.release_repair(native, dispatch.Journal(Path(temp.name) / 'release2.json'), **kwargs)
        self.assertEqual((released['action'], scan['status'], native.starts), ('released', 'in_progress', ['scan']))


class NativeTests(unittest.TestCase):
    def test_skip_comment_preserves_content_and_requires_agent_authored_readback(self):
        from duty_queue_test import comment
        native = dispatch.Native(['multica'], 'project', 'agent')
        item = {'roundId': 'scan', 'candidateId': 'intercom:a', 'state': 'skipped_failure'}
        saved = comment(dispatch.queue.RECORD_MARKER, item)
        with patch.object(native, 'call', side_effect=[[], {}, [saved]]) as call:
            native.save_skip('lane-1', item, allow_write=True, before_write=lambda: None)
        args, content = call.call_args_list[1].args
        self.assertIn('--content-stdin', args)
        self.assertIn('```json\n', content)
        member_comment = comment(dispatch.queue.RECORD_MARKER, item, author='member')
        with patch.object(native, 'call', return_value=[member_comment]) as call:
            with self.assertRaises(dispatch.Error):
                native.save_skip('lane-1', item, allow_write=False, before_write=lambda: None)
        self.assertTrue(all('add' not in entry.args[0] for entry in call.call_args_list))

    def test_all_project_pages_are_required_before_agent_run_snapshot(self):
        native = dispatch.Native(['multica'], 'project', 'agent')
        with patch.object(native, 'call', side_effect=[
            {'id': 'project'}, {'id': 'agent'},
            {'issues': [{'id': 'one'}], 'offset': 0, 'total': 2, 'has_more': True},
            {'issues': [{'id': 'two'}], 'offset': 1, 'total': 2, 'has_more': False}, []]) as call:
            result = native.snapshot()
        self.assertEqual([row['id'] for row in result['issues']], ['one', 'two'])
        self.assertEqual(call.call_args.args[0], ['agent', 'tasks', 'agent'])

    def test_partial_pagination_and_unconfirmed_concurrency_fail_closed(self):
        native = dispatch.Native(['multica'], 'project', 'agent')
        with patch.object(native, 'call', side_effect=[{'id': 'project'}, {'id': 'agent'},
                          {'issues': [], 'offset': 0, 'total': 1, 'has_more': False}]):
            with self.assertRaises(dispatch.Error):
                native.snapshot()

    def test_timeline_calls_issue_timeline_and_requires_list_response(self):
        native = dispatch.Native(['multica'], 'project', 'agent')
        events = [status_event('done')]
        with patch.object(native, 'call', return_value=events) as call:
            self.assertEqual(native.timeline('lane-1'), events)
        self.assertEqual(call.call_args.args[0], ['issue', 'timeline', 'lane-1'])
        with patch.object(native, 'call', return_value={'unexpected': True}):
            with self.assertRaises(dispatch.Error):
                native.timeline('lane-1')


class ScreeningGateTests(unittest.TestCase):
    def test_rejects_dispatch_when_screening_incomplete(self):
        data = lane_snapshot(['a', 'b'])
        data['issues'][0]['metadata']['dailyScan']['screeningComplete'] = False
        with self.assertRaisesRegex(dispatch.Error, '初筛未完成'):
            dispatch.plan(data, **SCOPE)

    def test_rejects_dispatch_when_pending_screening_positive(self):
        data = lane_snapshot(['a', 'b'])
        data['issues'][0]['metadata']['dailyScan']['counts'] = {
            'collected': 372, 'suspected': 47, 'pendingScreening': 280}
        with self.assertRaisesRegex(dispatch.Error, '280 条反馈待初筛'):
            dispatch.plan(data, **SCOPE)

    def test_rejects_dispatch_when_registration_attention_present(self):
        data = lane_snapshot(['a', 'b'])
        data['issues'][0]['metadata']['dailyRound']['registrationAttention'] = {
            'kind': 'incomplete_screening_frozen_manifest'}
        with self.assertRaisesRegex(dispatch.Error, 'registrationAttention'):
            dispatch.plan(data, **SCOPE)

    def test_allows_dispatch_when_screening_complete_and_zero_pending(self):
        data = lane_snapshot(['a', 'b'])
        data['issues'][0]['metadata']['dailyScan']['screeningComplete'] = True
        data['issues'][0]['metadata']['dailyScan']['counts'] = {
            'collected': 372, 'suspected': 47, 'pendingScreening': 0}
        result = dispatch.plan(data, **SCOPE)
        self.assertEqual(result['action'], 'dispatch')

    def test_supports_four_lanes_and_four_concurrency_while_preserving_two_lane_rounds(self):
        data = lane_snapshot(['a', 'b', 'c', 'd', 'e'])
        scan = data['issues'][0]
        lane3 = issue('lane-3', status='in_progress', assignee_id='agent',
                      dailyLane={'schemaVersion': 1, 'mode': 'investigate_lane', 'roundId': 'scan',
                                 'lane': 3, 'laneCount': 4, 'state': 'ready', 'currentCandidateId': None})
        lane4 = issue('lane-4', status='in_progress', assignee_id='agent',
                      dailyLane={'schemaVersion': 1, 'mode': 'investigate_lane', 'roundId': 'scan',
                                 'lane': 4, 'laneCount': 4, 'state': 'ready', 'currentCandidateId': None})
        lane3['parent_issue_id'] = lane4['parent_issue_id'] = 'scan'
        lane3['workspace_id'] = lane4['workspace_id'] = 'workspace'
        for lane in data['issues'][1:3]:
            lane['metadata']['dailyLane']['laneCount'] = 4
        data['issues'].extend([lane3, lane4])
        scan['metadata']['dailyExecution']['laneCount'] = 4
        scan['metadata']['dailyRound']['laneIssueIds'] = ['lane-1', 'lane-2', 'lane-3', 'lane-4']
        data['agent']['max_concurrent_tasks'] = 4
        result = dispatch.plan(data, **SCOPE)
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(len(result['claims']), 4)
        self.assertEqual([c['candidateId'] for c in result['claims']],
                         ['intercom:a', 'intercom:b', 'intercom:c', 'intercom:d'])


if __name__ == '__main__':
    unittest.main()
