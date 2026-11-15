import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/duty-slack-format.py'
SPEC = importlib.util.spec_from_file_location('duty_slack_format', SCRIPT)
formatter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(formatter)


def delivered():
    return {
        'kind': 'delivered', 'summary': '未知性别显示为空', 'current': 4, 'total': 7,
        'source': {'label': '客户反馈', 'url': 'https://example.com/feedback/1'},
        'issue': {'label': 'GRM-1938', 'url': 'https://multica.example.com/grm/issues/GRM-1938'},
        'sections': {
            'story': ['未填写性别时显示为 Female。'],
            'cause': [[{'code': 'toGenderName'}, ' 把所有非 Male 值转换为 Female。']],
            'scope': ['仅映射有效性别。'], 'impact': ['覆盖未知值与合法性别。'],
            'prs': [[{'label': 'server #12', 'url': 'https://github.com/example/server/pull/12'}]],
            'acceptance': ['未知值为空，合法值保持正确。'],
            'next': ['进入下一项。'],
        },
    }


def all_elements(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from all_elements(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_elements(child)


class DutySlackFormatTest(unittest.TestCase):
    def test_delivery_preserves_content_links_code_and_fixed_headings(self):
        data = delivered()
        before = copy.deepcopy(data)
        blocks, body = formatter.render(data)
        elements = list(all_elements(blocks))
        headings = [e['text'] for e in elements if e.get('type') == 'text' and e.get('style', {}).get('bold')]
        self.assertEqual(headings, [
            '每日 Bug 值守｜4/7｜未知性别显示为空', '问题来源链接：', 'Multica 任务链接：',
            '问题背景 / Story：', '问题根因：', '修复范围 / Scope：', '影响面：',
            'PR：', '验收结论与报告：', '下一步：',
        ])
        self.assertEqual([e['url'] for e in elements if e.get('type') == 'link'], [
            data['source']['url'], data['issue']['url'], data['sections']['prs'][0][0]['url'],
        ])
        self.assertIn('toGenderName 把所有非 Male 值转换为 Female。', body)
        self.assertTrue(any(e.get('type') == 'text' and e.get('style', {}).get('code') for e in elements))
        self.assertEqual(data, before)
        self.assertNotIn('**', body)
        self.assertNotIn('\\n', body)

    def test_blocked_keeps_failure_boundaries_and_explicit_notification(self):
        data = delivered()
        data['kind'] = 'blocked'
        data['notify'] = ['U123ABC']
        data['sections'] = {
            'story': ['客户不在场扣款失败。'], 'cause': ['参数使用在场语义。'],
            'progress': ['已修改参数，未执行动态扣款。'], 'blocker': ['测试凭据不可用。'],
            'conclusion': ['受阻，尚未完整交付。'], 'prs': ['未交付 PR，历史证据在任务保留。'],
            'next': ['接续下一项。'],
        }
        blocks, body = formatter.render(data)
        self.assertIn('（受阻）', body)
        self.assertIn('受阻，尚未完整交付。', body)
        self.assertNotIn('修复范围', body)
        self.assertEqual([e['user_id'] for e in all_elements(blocks) if e.get('type') == 'user'], ['U123ABC'])
        self.assertEqual(body.count('<@U123ABC>'), 1)

    def test_update_stays_short_without_reintroducing_first_delivery_fields(self):
        data = {'kind': 'update', 'summary': '退款动态复验', 'issue': delivered()['issue'],
                'version': 2, 'sections': {'prs': ['未创建'], 'acceptance': ['仍未通过，退款失败。']}}
        _, body = formatter.render(data)
        self.assertIn('（第 2 版）', body)
        self.assertIn('仍未通过，退款失败。', body)
        self.assertNotIn('问题来源', body)
        data['sections']['story'] = ['多余背景']
        with self.assertRaises(ValueError):
            formatter.render(data)

    def test_missing_columns_and_mixed_syntax_fail_before_output(self):
        for bad in ['**假加粗**', '[链接](https://example.com)', '<@U123>', '@here', '一行\\n二行', '一行\n二行']:
            with self.subTest(bad=bad):
                data = delivered()
                data['sections']['story'] = [bad]
                with self.assertRaises(ValueError):
                    formatter.render(data)
        for mutation in ('missing', 'unknown', 'empty', 'old_url', 'wrong_number', 'oversized'):
            with self.subTest(mutation=mutation):
                data = delivered()
                if mutation == 'missing':
                    del data['sections']['cause']
                elif mutation == 'unknown':
                    data['sections']['extra'] = ['额外栏目']
                elif mutation == 'empty':
                    data['sections']['cause'] = []
                elif mutation == 'old_url':
                    data['issue']['url'] = 'https://multica.example.com/issues/a-uuid'
                elif mutation == 'wrong_number':
                    data['issue']['url'] = 'https://multica.example.com/grm/issues/GRM-1937'
                else:
                    data['sections']['story'] = ['长' * 1900] * 10
                with self.assertRaises(ValueError):
                    formatter.render(data)

    def test_invalid_links_cannot_turn_into_notifications_or_silent_plain_text(self):
        for url in ['javascript:alert(1)', 'http://example.com', 'https://user:pass@example.com',
                    'https://example.com/<@U123>', 'https://example.com/a b']:
            data = delivered()
            data['source']['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                formatter.render(data)

    def test_cli_writes_consistent_payload_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input.json'
            source.write_text(json.dumps(delivered()), encoding='utf-8')
            result = subprocess.run([sys.executable, str(SCRIPT), '--input', str(source),
                                     '--output-dir', str(root / 'out')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            blocks, body = formatter.render(delivered())
            self.assertEqual(json.loads((root / 'out/blocks.json').read_text()), blocks)
            self.assertEqual((root / 'out/fallback.txt').read_text(), body)
            original = (root / 'out/blocks.json').read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), '--input', str(root / 'out/blocks.json'),
                                     '--output-dir', str(root / 'out')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / 'out/blocks.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
