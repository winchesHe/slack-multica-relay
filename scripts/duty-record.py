#!/usr/bin/env python3
"""单条调查一键收口组合脚本：
原子完成「上传调查报告附件 → 注入真实 attachmentId 并追加 daily-investigation:v3 评论
→ 回读评论校验 JSON 一致 → 调用同目录 duty-dispatch.py advance 交接」，
保持全部现有协议与回读门禁不变，把收口从 ~10 轮 LLM 往返压成 1 次调用。"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


RECORD_MARKER = 'daily-investigation:v3'
PLACEHOLDERS = {'', 'pending', 'pending-upload', 'pending_upload', 'tbd', 'todo', 'none', 'null'}


class RecordError(ValueError):
    pass


def _cli_base(args):
    cli = ['multica']
    if args.profile:
        cli += ['--profile', args.profile]
    cli += ['--server-url', args.server_url, '--workspace-id', args.workspace_id]
    return cli


def _run_json(cmd, *, stdin_text=None):
    proc = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecordError(f'命令失败({proc.returncode}): {" ".join(cmd[:6])} :: {(proc.stderr or proc.stdout).strip()}')
    raw = (proc.stdout or '').strip()
    start = min((idx for idx in (raw.find('{'), raw.find('[')) if idx >= 0), default=-1)
    if start < 0:
        raise RecordError(f'命令未返回 JSON: {raw[:200]}')
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise RecordError(f'JSON 解析失败: {exc}') from exc


def build_record_comment(record, summary_line):
    round_id = str(record.get('roundId') or '').strip()
    candidate_id = str(record.get('candidateId') or '').strip()
    if not round_id or not candidate_id:
        raise RecordError('record 缺少 roundId 或 candidateId')
    if record.get('mode') != 'investigate':
        raise RecordError('工位调查结论记录的 mode 必须为 investigate')
    inv_rec = record.get('investigationRecord')
    att_id = (inv_rec or {}).get('attachmentId') if isinstance(inv_rec, dict) else None
    if not isinstance(att_id, str) or att_id.strip().lower() in PLACEHOLDERS:
        raise RecordError('investigationRecord.attachmentId 缺失或为占位符')
    header = f'<!-- {RECORD_MARKER} round={round_id} candidate={candidate_id} -->'
    line = (summary_line or f'已完成候选 `{candidate_id}` 调查留档，结论 `{record.get("state")}`。').strip()
    body = json.dumps(record, ensure_ascii=False, indent=2)
    return f'{header}\n{line}\n\n```json\n{body}\n```'


def verify_comment_readback(comments, *, agent_id, round_id, candidate_id, attachment_id):
    marker_prefix = f'<!-- {RECORD_MARKER} '
    matched = []
    for comment in comments:
        if comment.get('author_type') != 'agent' or comment.get('author_id') != agent_id:
            continue
        content = comment.get('content') or ''
        if not content.startswith(marker_prefix):
            continue
        m = re.search(r'```json\n(.*?)\n```', content, re.S)
        if not m:
            continue
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if parsed.get('roundId') == round_id and parsed.get('candidateId') == candidate_id:
            matched.append((comment.get('created_at') or '', comment.get('id'), parsed))
    if not matched:
        raise RecordError('回读工位评论未找到刚写入的 daily-investigation:v3 记录')
    matched.sort(key=lambda row: row[0])
    _, comment_id, latest = matched[-1]
    actual_att = (latest.get('investigationRecord') or {}).get('attachmentId')
    if actual_att != attachment_id:
        raise RecordError(f'回读 attachmentId 不一致: expected={attachment_id} actual={actual_att}')
    return comment_id, latest


def submit(args, *, run_json=_run_json):
    report_path = Path(args.report_file).resolve()
    if not report_path.is_file() or report_path.stat().st_size == 0:
        raise RecordError(f'调查报告文件不存在或为空: {report_path}')
    record = json.loads(Path(args.record_json_file).read_text(encoding='utf-8'))
    if not isinstance(record, dict):
        raise RecordError('record-json-file 必须是 JSON 对象')
    record.setdefault('schemaVersion', 1)
    record['mode'] = 'investigate'
    record['roundId'] = args.round_id
    record.setdefault('sourceScanId', args.round_id)
    record['laneIssueId'] = args.lane_issue

    cli = _cli_base(args)
    # 1. 上传报告附件并获取真实 attachmentId
    upload_note = f'调查报告附件：{record.get("candidateId", report_path.name)}'
    upload_cmd = cli + [
        'issue', 'comment', 'add', args.lane_issue,
        '--content', upload_note,
        '--attachment', str(report_path),
        '--allow-external-file',
        '--output', 'json',
    ]
    uploaded = run_json(upload_cmd)
    attachments = uploaded.get('attachments') if isinstance(uploaded, dict) else None
    if not isinstance(attachments, list) or not attachments or not attachments[0].get('id'):
        raise RecordError('上传调查报告后未返回真实 attachmentId')
    attachment_id = str(attachments[0]['id']).strip()
    if attachment_id.lower() in PLACEHOLDERS:
        raise RecordError(f'无效 attachmentId: {attachment_id}')

    inv_rec = record.get('investigationRecord')
    if not isinstance(inv_rec, dict):
        inv_rec = {}
    inv_rec['attachmentId'] = attachment_id
    if record.get('evidenceRevision') and not inv_rec.get('evidenceRevision'):
        inv_rec['evidenceRevision'] = record['evidenceRevision']
    record['investigationRecord'] = inv_rec

    # 2. 追加 daily-investigation:v3 评论
    comment_body = build_record_comment(record, args.summary_line)
    add_cmd = cli + ['issue', 'comment', 'add', args.lane_issue, '--content-stdin', '--output', 'json']
    run_json(add_cmd, stdin_text=comment_body)

    # 3. 回读校验
    comments = run_json(cli + ['issue', 'comment', 'list', args.lane_issue, '--output', 'json'])
    if not isinstance(comments, list):
        raise RecordError('回读评论列表格式异常')
    comment_id, _ = verify_comment_readback(
        comments,
        agent_id=args.agent_id,
        round_id=args.round_id,
        candidate_id=record['candidateId'],
        attachment_id=attachment_id,
    )

    result = {
        'ok': True,
        'roundId': args.round_id,
        'laneIssueId': args.lane_issue,
        'candidateId': record['candidateId'],
        'state': record.get('state'),
        'reportAttachmentId': attachment_id,
        'recordCommentId': comment_id,
        'readbackVerified': True,
    }

    # 4. 可选立即调用同目录 duty-dispatch.py advance 交接
    if not args.skip_advance:
        dispatch_script = Path(args.dispatch_script) if args.dispatch_script else Path(__file__).with_name('duty-dispatch.py')
        if not dispatch_script.is_file():
            raise RecordError(f'未找到调度脚本: {dispatch_script}')
        if not (args.project_id and args.runtime_id and args.revision and args.current_run):
            raise RecordError('启用 advance 时必须提供 --project-id、--runtime-id、--revision、--current-run')
        adv_cmd = [
            sys.executable, str(dispatch_script), 'advance',
            '--server-url', args.server_url,
            '--workspace-id', args.workspace_id,
            '--project-id', args.project_id,
            '--agent-id', args.agent_id,
            '--runtime-id', args.runtime_id,
            '--revision', args.revision,
            '--round-id', args.round_id,
            '--current-issue', args.lane_issue,
            '--current-run', args.current_run,
        ]
        result['advance'] = run_json(adv_cmd)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('submit', help='上传报告 + 写入 daily-investigation:v3 评论 + 回读校验 + 调用 advance')
    p.add_argument('--profile')
    p.add_argument('--server-url', required=True)
    p.add_argument('--workspace-id', required=True)
    p.add_argument('--agent-id', required=True)
    p.add_argument('--round-id', required=True)
    p.add_argument('--lane-issue', required=True)
    p.add_argument('--report-file', required=True)
    p.add_argument('--record-json-file', required=True)
    p.add_argument('--summary-line', default='')
    p.add_argument('--project-id')
    p.add_argument('--runtime-id')
    p.add_argument('--revision')
    p.add_argument('--current-run')
    p.add_argument('--dispatch-script')
    p.add_argument('--skip-advance', action='store_true')
    args = parser.parse_args(argv)
    try:
        out = submit(args)
    except RecordError as exc:
        print(f'调查收口失败：{exc}', file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
