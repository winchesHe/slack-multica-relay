"""duty-prescreen.py 与 duty-record.py 定向单元测试。"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prescreen = _load('duty_prescreen', 'scripts/duty-prescreen.py')
record_mod = _load('duty_record', 'scripts/duty-record.py')


class PrescreenTests(unittest.TestCase):
    def test_auto_classifies_cs_duplicate_and_empty_window_and_compacts_remaining(self):
        intercom_art = {
            'manifest': {'runId': 'daily-intercom-1', 'sourceKey': 'intercom'},
            'evidence': [
                {
                    'evidenceId': 'ic-1', 'sourceKey': 'intercom', 'sourceObjectId': '101',
                    'sourceUrl': 'https://app.intercom.com/a/inbox/x/conversation/101',
                    'csTickets': ['CS-55001'], 'jiraKeys': ['CS-55001'], 'messages': [],
                },
                {
                    'evidenceId': 'ic-2', 'sourceKey': 'intercom', 'sourceObjectId': '102',
                    'sourceUrl': 'https://app.intercom.com/a/inbox/x/conversation/102',
                    'csTickets': [], 'jiraKeys': [],
                    'messages': [{'inWindow': True, 'authorType': 'admin', 'body': 'internal note only'}],
                },
                {
                    'evidenceId': 'ic-3', 'sourceKey': 'intercom', 'sourceObjectId': '103',
                    'sourceUrl': 'https://app.intercom.com/a/inbox/x/conversation/103',
                    'csTickets': [], 'jiraKeys': [],
                    'messages': [{'inWindow': True, 'authorType': 'user', 'body': 'already seen bug'}],
                },
                {
                    'evidenceId': 'ic-4', 'sourceKey': 'intercom', 'sourceObjectId': '104',
                    'sourceUrl': 'https://app.intercom.com/a/inbox/x/conversation/104',
                    'csTickets': [], 'jiraKeys': ['MER-123'], 'moegoCompanyIds': ['999'],
                    'messages': [{'inWindow': True, 'authorType': 'user', 'body': 'checkout crashes on save'}],
                },
            ],
        }
        canny_art = {
            'manifest': {'runId': 'daily-canny-1', 'sourceKey': 'canny'},
            'evidence': [
                {
                    'evidenceId': 'cn-1', 'sourceKey': 'canny', 'sourceObjectId': 'p1',
                    'sourceUrl': 'https://moego.canny.io/p/1',
                    'title': 'Fee modal stale', 'content': 'Add fee does not refresh', 'imageURLs': ['https://img/1.png'],
                },
            ],
        }
        res = prescreen.prescreen_artifacts(
            [intercom_art, canny_art],
            known_urls=['https://app.intercom.com/a/inbox/x/conversation/103/'],
        )
        self.assertEqual(res['summary'], {
            'total': 5, 'csFollowed': 1, 'duplicate': 1, 'autoNonBug': 1, 'needsLlmScreening': 2,
        })
        by_ev = {x['evidenceId']: x for x in res['autoClassified']}
        self.assertEqual(by_ev['ic-1']['classification'], 'cs_followed')
        self.assertEqual(by_ev['ic-2']['classification'], 'non_bug')
        self.assertEqual(by_ev['ic-3']['classification'], 'duplicate')
        need = {x['evidenceId']: x for x in res['needsScreening']}
        self.assertEqual(need['ic-4']['jiraKeysHint'], ['MER-123'])
        self.assertEqual(need['cn-1']['hasAttachments'], True)


class RecordSubmitTests(unittest.TestCase):
    def test_submit_uploads_report_writes_comment_verifies_readback_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report = tmp_path / 'report.md'
            report.write_text('# Investigation Report\nEvidence ok', encoding='utf-8')
            rec_file = tmp_path / 'record.json'
            rec_file.write_text(json.dumps({
                'candidateId': 'r2:intercom:104',
                'feedbackDate': '2026-11-26',
                'source': 'intercom',
                'sourceUrl': 'https://app.intercom.com/a/inbox/x/conversation/104',
                'state': 'excluded',
                'conclusionRef': 'expected_behavior',
                'evidenceRevision': 'rev-1',
            }), encoding='utf-8')
            fake_dispatch = tmp_path / 'duty-dispatch.py'
            fake_dispatch.write_text('#!/usr/bin/env python3\n', encoding='utf-8')

            calls = []
            saved_comments = []

            def fake_run_json(cmd, *, stdin_text=None):
                calls.append((cmd, stdin_text))
                if 'comment' in cmd and 'add' in cmd and '--attachment' in cmd:
                    return {'id': 'c-upload', 'attachments': [{'id': 'att-real-123'}]}
                if 'comment' in cmd and 'add' in cmd and '--content-stdin' in cmd:
                    saved_comments.append({
                        'id': 'c-record', 'author_type': 'agent', 'author_id': 'agent-1',
                        'created_at': '2026-11-27T01:50:00Z', 'content': stdin_text,
                    })
                    return saved_comments[-1]
                if 'comment' in cmd and 'list' in cmd:
                    return saved_comments
                if str(fake_dispatch) in cmd:
                    return {'protocolVersion': 2, 'action': 'dispatch', 'remaining': 10}
                raise AssertionError(f'unexpected cmd: {cmd}')

            args = argparse.Namespace(
                profile=None, server_url='https://multica.devops.moego.dev', workspace_id='ws-1',
                agent_id='agent-1', round_id='round-1', lane_issue='lane-1',
                report_file=str(report), record_json_file=str(rec_file),
                summary_line='已排除，属于预期配置行为。', project_id='proj-1',
                runtime_id='rt-1', revision='a' * 40, current_run='run-1',
                dispatch_script=str(fake_dispatch), skip_advance=False,
            )
            res = record_mod.submit(args, run_json=fake_run_json)
            self.assertTrue(res['ok'])
            self.assertEqual(res['reportAttachmentId'], 'att-real-123')
            self.assertEqual(res['recordCommentId'], 'c-record')
            self.assertTrue(res['readbackVerified'])
            self.assertEqual(res['advance']['action'], 'dispatch')
            self.assertEqual(len(calls), 4)


if __name__ == '__main__':
    unittest.main()
