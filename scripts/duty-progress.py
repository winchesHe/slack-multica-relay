"""只读推导每日值守阶段快照；复用调度器的证据与交付门槛，不推进业务状态。"""
import importlib.util
import json
import re
from pathlib import Path


spec = importlib.util.spec_from_file_location('duty_queue', Path(__file__).with_name('duty-queue.py'))
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
SOURCES = ('canny', 'facebook', 'intercom')
V3_INVESTIGATION_MARKER = 'daily-investigation:v3'
V3_MANIFEST_MARKER = 'daily-round-manifest:v3'
V3_FINISH_MARKER = 'daily-scan-finish:v3'
V3_CONCLUSIONS = {'confirmed', 'duplicate', 'cs_followed', 'excluded', 'suspended_external', 'skipped_failure'}
V3_STOPPED = {'cancelled', 'canceled', 'done'}
PR_URL = re.compile(r'https://github\.com/[^/]+/[^/]+/pull/\d+')


def count(value):
    return type(value) is int and value >= 0


def is_v3_scan(issue):
    """判断一个扫描父任务是否使用 v3 双读协议。"""
    execution = queue.meta(issue, 'dailyExecution')
    scan = queue.meta(issue, 'dailyScan')
    return (bool(scan) and (execution.get('protocolVersion') == 3
                            or execution.get('policy') == 'lightweight_agent_driven_v3')
            and not any(queue.meta(issue, key) for key in ('dailyRound', 'dailyBatch', 'dailyDispatch')))


def _comment_value(comment, marker):
    content = comment.get('content') or ''
    if not content.startswith('<!-- ' + marker + ' '):
        return None
    match = re.search(r'```json\n(.*?)\n```', content, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _agent_comments(issue, marker, agent_id):
    values = []
    for comment in sorted(issue.get('_comments') or [], key=lambda item: item.get('created_at') or ''):
        item = _comment_value(comment, marker)
        if item is None:
            continue
        is_agent = comment.get('author_type') == 'agent' and comment.get('author_id') == agent_id
        is_override = (marker == V3_INVESTIGATION_MARKER and comment.get('author_type') == 'member'
                       and item.get('manualOverride') is True)
        if is_agent or is_override:
            values.append((comment.get('created_at') or '', item, is_override))
    return values


def v3_manifest(scan, agent_id):
    """返回一致的 v3 清单回执；无有效清单时返回 None。"""
    valid = []
    for created, item, _ in _agent_comments(scan, V3_MANIFEST_MARKER, agent_id):
        entries = item.get('candidates')
        ids = [entry.get('candidateId') if isinstance(entry, dict) else None
               for entry in entries] if isinstance(entries, list) else []
        if (item.get('protocolVersion') != 3 or item.get('roundId') != scan['id']
                or item.get('feedbackDate') != queue.meta(scan, 'dailyScan').get('targetDate')
                or type(item.get('candidateCount')) is not int or item['candidateCount'] != len(entries or [])
                or any(not queue.text(candidate_id) for candidate_id in ids) or len(set(ids)) != len(ids)
                or queue.candidate_set_hash(ids) != item.get('candidateSetSha256')
                or not queue.valid_attachment_id(item.get('attachmentId')) or not queue.text(item.get('sha256'))
                or item.get('readbackHashVerified') is not True
                or any(not isinstance(entry, dict) or entry.get('feedbackDate') != item['feedbackDate']
                       for entry in (entries or []))):
            continue
        valid.append((created, item))
    if not valid:
        return None
    first = valid[0][1]
    signature = (first.get('candidateSetSha256'), json.dumps(first.get('candidates'), sort_keys=True,
                                                               ensure_ascii=False))
    if any((item.get('candidateSetSha256'), json.dumps(item.get('candidates'), sort_keys=True,
                                                       ensure_ascii=False)) != signature
           for _, item in valid[1:]):
        raise queue.QueueError('v3 扫描清单回执互相冲突')
    return valid[-1][1]


def v3_finish(scan, agent_id):
    values = []
    for created, item, _ in _agent_comments(scan, V3_FINISH_MARKER, agent_id):
        if item.get('protocolVersion') == 3 and item.get('roundId') == scan['id']:
            values.append((created, item))
    return values[-1][1] if values else None


def v3_lanes(scan, issues, agent_id):
    rows = [row for row in issues if queue.meta(row, 'dailyLane').get('roundId') == scan['id']]
    if len(rows) != 2:
        raise queue.QueueError('v3 调查工位数量不是 2')
    lanes = sorted(rows, key=lambda row: queue.meta(row, 'dailyLane').get('lane', 0))
    numbers = []
    for row in lanes:
        lane = queue.meta(row, 'dailyLane')
        if (lane.get('schemaVersion') != 1 or lane.get('protocolVersion') != 3
                or lane.get('mode') != 'investigate_lane' or lane.get('roundId') != scan['id']
                or lane.get('lane') not in (1, 2) or any(key in lane for key in
                                                           ('state', 'currentCandidateId', 'failureHandoffCandidateId'))
                or row.get('parent_issue_id') != scan['id']
                or row.get('assignee_id') not in (None, agent_id)
                or row.get('status') in V3_STOPPED):
            raise queue.QueueError('v3 调查工位协议或父子关系无效')
        numbers.append(lane['lane'])
    if numbers != [1, 2]:
        raise queue.QueueError('v3 调查工位编号不是 1 和 2')
    return lanes


def _v3_record_valid(item, entry, agent_record):
    conclusion = item.get('conclusion') or item.get('state')
    if (item.get('schemaVersion') != 1 or item.get('protocolVersion') != 3
            or item.get('mode') != 'investigate' or item.get('roundId') != entry.get('_roundId')
            or item.get('sourceScanId') != entry.get('_roundId')
            or item.get('candidateId') != entry.get('candidateId')
            or item.get('feedbackDate') != entry.get('feedbackDate')
            or conclusion not in V3_CONCLUSIONS
            or not queue.valid_source_anchor(item, entry)):
        return False
    if agent_record and (not isinstance(item.get('fencingToken'), int) or item['fencingToken'] < 1):
        return False
    if conclusion == 'skipped_failure':
        failure = item.get('failureSkip')
        if not isinstance(failure, dict):
            return False
        candidate = dict(item)
        candidate['nextAction'] = item.get('nextAction') or failure.get('resumeCondition')
        return queue.valid_failure_skip(candidate)
    report = item.get('report') or {}
    if (not queue.valid_attachment_id(report.get('attachmentId')) or not queue.text(report.get('sha256'))
            or report.get('readbackVerified') is not True or not queue.text(item.get('evidenceRevision'))):
        return False
    if conclusion == 'confirmed' and not queue.valid_confirmation(item):
        return False
    if conclusion == 'suspended_external' and not queue.valid_external_block(item):
        return False
    if conclusion in {'duplicate', 'excluded', 'cs_followed'} and not queue.text(item.get('conclusionRef')):
        return False
    return True


def _v3_latest_records(scan, lanes, manifest, agent_id):
    entries = {entry['candidateId']: {**entry, '_roundId': scan['id']} for entry in manifest['candidates']}
    records = []
    for issue in [scan, *lanes]:
        records.extend(_agent_comments(issue, V3_INVESTIGATION_MARKER, agent_id))
    by_candidate = {}
    for created, item, manual in records:
        entry = entries.get(item.get('candidateId'))
        if not entry or not _v3_record_valid(item, entry, not manual):
            continue
        candidates = by_candidate.setdefault(item['candidateId'], {'manual': [], 'agent': []})
        candidates['manual' if manual else 'agent'].append((created, item))
    selected = {}
    for candidate_id, candidates in by_candidate.items():
        if candidates['manual']:
            manual = max(candidates['manual'], key=lambda pair: pair[0])
            latest_agent = max(candidates['agent'], key=lambda pair: pair[0], default=None)
            if latest_agent is None or manual[0] >= latest_agent[0]:
                selected[candidate_id] = manual[1]
                continue
        if candidates['agent']:
            selected[candidate_id] = max(candidates['agent'],
                                          key=lambda pair: (pair[1].get('fencingToken'), pair[0]))[1]
    seen_attachments, seen_intercom = set(), set()
    for candidate_id, item in selected.items():
        conclusion = item.get('conclusion') or item.get('state')
        if conclusion == 'skipped_failure':
            continue
        attachment = (item.get('report') or {}).get('attachmentId')
        if attachment in seen_attachments:
            raise queue.QueueError('v3 调查复用了同一份报告附件')
        seen_attachments.add(attachment)
        if conclusion == 'confirmed':
            tokens = {token for token in queue.source_tokens(item.get('sourceUrl')) |
                      queue.source_tokens(item.get('evidenceRevision')) if token.startswith('intercom:')}
            if tokens & seen_intercom:
                raise queue.QueueError('v3 调查复用了同一份 Intercom 会话证据')
            seen_intercom |= tokens
    return entries, selected


def investigation_v3(scan, issues):
    agent_id = scan.get('assignee_id')
    manifest = v3_manifest(scan, agent_id)
    if manifest is None:
        if queue.meta(scan, 'dailyScan').get('coverageComplete') is True:
            return {'state': 'registering', 'completed': None, 'total': None, 'confirmed': None}, []
        return {'state': 'registering', 'completed': None, 'total': None, 'confirmed': None}, []
    lanes = v3_lanes(scan, issues, agent_id)
    entries, selected = _v3_latest_records(scan, lanes, manifest, agent_id)
    members, states = [], []
    for entry in manifest['candidates']:
        candidate_id = entry['candidateId']
        item = selected.get(candidate_id, {'schemaVersion': 1, 'protocolVersion': 3, 'mode': 'investigate',
                                            'roundId': scan['id'], 'sourceScanId': scan['id'],
                                            'candidateId': candidate_id, 'feedbackDate': entry['feedbackDate'],
                                            'state': 'ready'})
        row = {'id': f"{scan['id']}:{candidate_id}", 'project_id': scan.get('project_id'),
               'workspace_id': scan.get('workspace_id'), 'parent_issue_id': scan['id'],
               'assignee_id': scan.get('assignee_id'), 'status': 'in_progress',
               'identifier': None, 'title': '反馈调查｜' + (entry.get('summary') or candidate_id),
               'created_at': scan.get('created_at'), 'metadata': {'dailyInvestigation': item}}
        members.append(row)
        states.append(item.get('conclusion') or item.get('state'))
    completed = sum(state in V3_CONCLUSIONS for state in states)
    confirmed_ids = [f"{scan['id']}:{entry['candidateId']}" for entry, state in zip(manifest['candidates'], states)
                     if state == 'confirmed']
    result = {'state': 'completed' if completed == len(states) else 'investigating',
              'total': len(states), 'completed': completed, 'confirmed': len(confirmed_ids),
              'confirmedIssueIds': confirmed_ids,
              'duplicate': states.count('duplicate'), 'excluded': states.count('excluded'),
              'csFollowed': states.count('cs_followed'), 'skippedFailure': states.count('skipped_failure')}
    return result, members


def _v3_receipts(issue, scan, agent_id):
    valid = []
    for created, item, _ in _agent_comments(issue, 'daily-delivery:v3', agent_id):
        report, slack, ledger = item.get('report') or {}, item.get('slack') or {}, item.get('ledger') or {}
        bug = queue.meta(issue, 'dailyBug')
        prs = item.get('prs') or ([item.get('pr')] if item.get('pr') else [])
        if (item.get('protocolVersion') != 3 or item.get('issueId') != issue['id']
                or item.get('roundId') != scan['id'] or item.get('candidateId') != bug.get('candidateId')
                or type(item.get('deliveryVersion')) is not int or item['deliveryVersion'] < 1
                or not isinstance(prs, list) or not prs or not all(isinstance(pr, str) and PR_URL.fullmatch(pr) for pr in prs)
                or not isinstance(item.get('prHeads'), list) or not item['prHeads']
                or not all(isinstance(head, str) and re.fullmatch(r'[0-9a-fA-F]{7,40}', head) for head in item['prHeads'])
                or not queue.valid_attachment_id(report.get('attachmentId')) or not queue.text(report.get('sha256'))
                or report.get('readbackVerified') is not True or not queue.text(slack.get('permalink'))
                or slack.get('readbackVerified') is not True or not queue.text(ledger.get('issue'))
                or not queue.text(ledger.get('commentId')) or ledger.get('readbackVerified') is not True):
            continue
        valid.append((created, 'delivered', item))
    for created, item, _ in _agent_comments(issue, 'daily-blocked:v3', agent_id):
        blocker, slack, ledger = item.get('blocker') or {}, item.get('slack') or {}, item.get('ledger') or {}
        if (item.get('protocolVersion') != 3 or item.get('issueId') != issue['id']
                or item.get('roundId') != scan['id'] or item.get('candidateId') != queue.meta(issue, 'dailyBug').get('candidateId')
                or not all(queue.text(blocker.get(key)) for key in ('kind', 'reason', 'resumeCondition'))
                or not queue.text(slack.get('permalink')) or slack.get('readbackVerified') is not True
                or not queue.text(ledger.get('issue')) or not queue.text(ledger.get('commentId'))
                or ledger.get('readbackVerified') is not True):
            continue
        valid.append((created, 'blocked', item))
    return max(valid, key=lambda value: value[0]) if valid else None


def v3_latest_receipt(issue, scan):
    return _v3_receipts(issue, scan, scan.get('assignee_id'))


def collection(scan, scan_record=None):
    data = queue.meta(scan, 'dailyScan')
    values = data.get('collected')
    # 旧轮只约定最终台账保存采集数；读取时兼容，不为看板迁移旧轮 metadata。
    if values is None:
        values = (scan_record or {}).get('collected')
    if values is not None and (not isinstance(values, dict)
                              or any(not count(value) for value in values.values())):
        raise queue.QueueError('采集数量不是有效非负整数')
    known = {key: values[key] for key in SOURCES if isinstance(values, dict) and count(values.get(key))}
    covered = data.get('coverageComplete', (scan_record or {}).get('coverageComplete'))
    complete_counts = len(known) == len(SOURCES)
    state = 'completed' if covered is True and complete_counts else 'collecting'
    sources = data.get('sources') or {}
    if not isinstance(sources, dict):
        raise queue.QueueError('来源状态格式无效')
    if any(isinstance(value, dict) and value.get('status') in {'failed', 'error'} for value in sources.values()):
        state = 'incomplete'
    return {'state': state, 'collected': known or None,
            'total': sum(known.values()) if complete_counts else None, 'coverageComplete': covered}


def investigation(scan, issues):
    if is_v3_scan(scan):
        return investigation_v3(scan, issues)
    info = queue.meta(scan, 'dailyRound')
    if not info:
        return {'state': 'legacy', 'completed': None, 'total': None, 'confirmed': None}, []
    if info.get('registrationComplete') is not True:
        return {'state': 'registering', 'completed': None, 'total': info.get('candidateCount'), 'confirmed': None}, []
    members, pending = queue.round_candidates(scan, issues, scan.get('assignee_id'))
    for row in members:
        if row.get('project_id') != scan.get('project_id') or row.get('workspace_id') != scan.get('workspace_id'):
            raise queue.QueueError('调查任务不属于原扫描的项目或工作区')
        item = queue.meta(row, 'dailyInvestigation')
        if item.get('schemaVersion') != 1 or item.get('mode') != 'investigate' or item.get('state') not in queue.STATES:
            raise queue.QueueError('调查入口协议或状态无效')
    pending_ids = {row['id'] for row in pending}
    concluded = [row for row in members if row['id'] not in pending_ids]
    states = [queue.meta(row, 'dailyInvestigation')['state'] for row in concluded]
    result = {'state': 'investigating' if pending else 'completed', 'total': len(members),
              'completed': len(concluded), 'confirmed': sum(state in queue.FIXABLE for state in states),
              'confirmedIssueIds': [row['id'] for row in concluded
                                    if queue.meta(row, 'dailyInvestigation')['state'] in queue.FIXABLE],
              'duplicate': states.count('duplicate'), 'excluded': states.count('excluded'),
              'csFollowed': states.count('cs_followed'), 'skippedFailure': states.count('skipped_failure')}
    return result, members


def _active_run(issue_id, runs, agent_id):
    return runs is not None and any(run.get('issue_id') == issue_id and run.get('agent_id') == agent_id
                                    and run.get('status') in queue.IN_FLIGHT for run in runs)


def repair_v3(scan, issues, investigated, members, runs, warnings):
    if investigated['state'] != 'completed':
        return {'state': 'not_started', 'completed': 0, 'total': None, 'blocked': 0, 'index': None,
                'needsRecovery': []}, []
    confirmed = [row for row in members
                 if (queue.meta(row, 'dailyInvestigation').get('conclusion') or
                     queue.meta(row, 'dailyInvestigation').get('state')) == 'confirmed']
    if not confirmed:
        return {'state': 'not_needed', 'completed': 0, 'total': 0, 'blocked': 0, 'index': None,
                'needsRecovery': []}, []
    confirmed_by_candidate = {queue.meta(row, 'dailyInvestigation')['candidateId']: row for row in confirmed}
    bugs = []
    for row in issues:
        item = queue.meta(row, 'dailyBug')
        if item.get('mode') != 'bug' or item.get('protocolVersion') != 3:
            continue
        if item.get('sourceScanId') == scan['id'] or item.get('batchId') == scan['id']:
            if row.get('parent_issue_id') != scan['id']:
                raise queue.QueueError('v3 修复任务必须直接挂在扫描父任务下')
            required = {'schemaVersion': 1, 'protocolVersion': 3, 'mode': 'bug',
                        'batchId': scan['id'], 'sourceScanId': scan['id'],
                        'investigationRef': f"{scan['id']}:{item.get('candidateId')}",
                        'feedbackDate': queue.meta(scan, 'dailyScan').get('targetDate')}
            if any(item.get(key) != value for key, value in required.items()):
                raise queue.QueueError('v3 修复任务协议无效')
            if any(key in item for key in ('state', 'index', 'total', 'completeDelivery', 'deliveryVersion', 'reviewState')):
                raise queue.QueueError('v3 修复任务包含已废弃状态字段')
            if item.get('candidateId') not in confirmed_by_candidate:
                continue
            bugs.append(row)
    by_candidate = {queue.meta(row, 'dailyBug').get('candidateId'): row for row in bugs}
    if len(by_candidate) != len(bugs) or set(by_candidate) != set(confirmed_by_candidate):
        raise queue.QueueError('v3 修复任务与确认候选集合不一致')
    completed = blocked = 0
    recovery = []
    attention = False
    for candidate_id, bug in by_candidate.items():
        receipt = _v3_receipts(bug, scan, scan.get('assignee_id'))
        if receipt:
            if receipt[1] == 'delivered':
                completed += 1
            else:
                blocked += 1
            continue
        if bug.get('status') == 'in_review':
            attention = True
            warnings.append(f"{bug.get('identifier', bug['id'])} 处于 in_review 但缺少有效 deliveryReceipt")
        elif (runs is not None and bug.get('status') == 'in_progress'
              and not _active_run(bug['id'], runs, scan.get('assignee_id'))):
            recovery.append(bug['id'])
            warnings.append(f"{bug.get('identifier', bug['id'])} 处于 in_progress 且无活动运行，等待 recover；未计为 blocked")
    if attention:
        state = 'attention'
    elif completed + blocked == len(bugs):
        state = 'completed_with_blocked' if blocked else 'completed'
    else:
        state = 'repairing'
    return {'state': state, 'completed': completed, 'blocked': blocked, 'total': len(bugs), 'index': None,
            'currentIssueId': None, 'needsRecovery': recovery}, bugs


def repair(scan, issues, investigated, members, runs=None, warnings=None):
    if is_v3_scan(scan):
        return repair_v3(scan, issues, investigated, members, runs, warnings if warnings is not None else [])
    batch, info = queue.meta(scan, 'dailyBatch'), queue.meta(scan, 'dailyRound')
    if not batch.get('bugIds'):
        if batch.get('state') == 'preparing':
            state = 'preparing'
        elif investigated['state'] == 'completed' and investigated.get('confirmed') == 0:
            state = 'not_needed'
        else:
            state = 'not_started'
        return {'state': state, 'completed': 0, 'total': None}, []
    if investigated['state'] != 'completed' or info.get('investigationComplete') is not True:
        raise queue.QueueError('调查尚未全部留档，不能将已有修复登记显示为正常修复')
    if not queue.verified_record(info.get('investigationIndex')) or queue.meta(scan, 'dailyScan').get('coverageComplete') is not True:
        raise queue.QueueError('完整调查索引或来源覆盖尚未核对')
    confirmed = [row for row in members if queue.meta(row, 'dailyInvestigation')['state'] in queue.FIXABLE]
    bugs = queue.round_bugs(scan, confirmed, {row['id']: row for row in issues})
    complete = sum(queue.verified_delivery(batch, row) for row in bugs)
    # 只统计已回读受阻通知的项；队列越过它们继续，受阻不计入完整交付。
    blocked = sum(queue.verified_block(batch, row) for row in bugs)
    state = 'repairing'
    if batch.get('state') in {'blocked', 'paused', 'cancelled'}:
        state = batch['state']
    elif batch.get('state') in queue.FINISHED_BATCHES:
        final = queue.round_plan(scan, issues, {row['id']: row for row in issues}, scan.get('assignee_id'))
        if final['action'] != 'complete_round':
            raise queue.QueueError('修复完成标记缺少对应版本的完整交付回执')
        state = {'finished_with_skips': 'completed_with_skips',
                 'finished_with_blocked': 'completed_with_blocked'}.get(final.get('outcome'), 'completed')
    current = batch.get('currentIssueId')
    if state == 'repairing' and (type(batch.get('index')) is not int
            or not 1 <= batch['index'] <= len(bugs) or current != bugs[batch['index'] - 1]['id']):
        raise queue.QueueError('固定修复游标与当前任务不一致')
    return {'state': state, 'completed': complete, 'blocked': blocked, 'total': len(bugs), 'index': batch.get('index'),
            'currentIssueId': current}, bugs



def attach_comments(issues, fetch, include_finished=False, round_id=None):
    """同时读取 legacy 工位轮次和 v3 扫描、工位、Bug 的评论证据。"""
    queue.attach_comments(issues, fetch, include_finished=include_finished, round_id=round_id)
    scans = [issue for issue in issues if is_v3_scan(issue)
             and (round_id is None or issue.get('id') == round_id)
             and (include_finished or issue.get('status') not in V3_STOPPED or issue.get('id') == round_id)]
    scan_ids = {scan['id'] for scan in scans}
    targets = {issue['id']: issue for issue in scans}
    for issue in issues:
        lane = queue.meta(issue, 'dailyLane')
        bug = queue.meta(issue, 'dailyBug')
        if lane.get('protocolVersion') == 3 and lane.get('roundId') in scan_ids:
            targets[issue['id']] = issue
        if (bug.get('protocolVersion') == 3
                and (bug.get('batchId') in scan_ids or bug.get('sourceScanId') in scan_ids
                     or issue.get('parent_issue_id') in scan_ids)):
            targets[issue['id']] = issue
    for issue in targets.values():
        comments = fetch(issue['id'])
        if not isinstance(comments, list):
            raise queue.QueueError('评论响应不完整')
        issue['_comments'] = comments
    return issues


def snapshot(scan, issues, runs, now, issue_url_base, scan_record=None):
    """阶段结论来自已保存证据；当前处理只认真实 run，不把 index 或 intent 当成正在执行。"""
    result = {'snapshotAt': now.isoformat(), 'scanIssue': scan['identifier'], 'activityKnown': runs is not None,
              'scanUrl': issue_url_base + scan['identifier'], 'current': [], 'warnings': [], 'needsRecovery': []}
    members, bugs = [], []
    for name in ('collection', 'investigation', 'repair'):
        try:
            if name == 'collection':
                result[name] = collection(scan, scan_record)
            elif name == 'investigation':
                result[name], members = investigation(scan, issues)
            else:
                result[name], bugs = repair(scan, issues, result['investigation'], members, runs, result['warnings'])
        except (queue.QueueError, KeyError, TypeError, ValueError):
            # 阶段读取失败不能伪装为 0 或未扫描，也不能阻断其它已有事实的展示。
            result[name] = {'state': 'attention', 'completed': None, 'total': None, 'confirmed': None}
            label = {'collection': '采集', 'investigation': '调查', 'repair': '修复'}[name]
            result['warnings'].append(f"{scan['identifier']} 的{label}阶段数据不完整或证据待核对")
    lanes = []
    if is_v3_scan(scan):
        try:
            if v3_manifest(scan, scan.get('assignee_id')) is not None:
                lanes = v3_lanes(scan, issues, scan.get('assignee_id'))
        except queue.QueueError:
            result['warnings'].append(f"{scan['identifier']} 的调查工位登记待核对")
    elif queue.lane_store(scan):
        try:
            lanes = queue.round_lanes(scan, issues, scan.get('assignee_id'))
        except queue.QueueError:
            result['warnings'].append(f"{scan['identifier']} 的调查工位登记待核对")
    by_id = {row['id']: row for row in [scan, *members, *bugs, *lanes]}
    member_ids, bug_ids = {row['id'] for row in members} | {row['id'] for row in lanes}, {row['id'] for row in bugs}
    titles = {queue.meta(row, 'dailyInvestigation').get('candidateId'): row['title'].split('｜')[-1] for row in members}
    if runs is None:
        result['warnings'].append('值守运行状态读取失败，当前处理任务未知')
    else:
        for run in sorted(runs, key=lambda row: row.get('status') != 'running'):
            row = by_id.get(run.get('issue_id'))
            if (not row or run.get('status') not in queue.IN_FLIGHT
                    or run.get('agent_id') != scan.get('assignee_id')):
                continue
            scan_phase = '采集' if result['collection']['state'] != 'completed' else '扫描汇总'
            phase = '调查' if row['id'] in member_ids else '修复' if row['id'] in bug_ids else scan_phase
            summary = row['title'].split('｜')[-1]
            claim = queue.meta(row, 'dailyLane').get('currentCandidateId')
            if claim in titles:
                # 工位卡展示「工位 n/2：正在查的候选」，便于看出两路各自在查什么。
                summary = f'{summary}：{titles[claim]}'
            result['current'].append({'identifier': row.get('identifier') or row['id'],
                                      'url': issue_url_base + (row.get('identifier') or row['id']),
                                      'summary': summary, 'phase': phase, 'status': run['status']})
    if result['investigation']['state'] == 'attention' or result['repair']['state'] == 'attention':
        result['activityKnown'] = False
    if result.get('repair', {}).get('needsRecovery'):
        result['needsRecovery'].extend(result['repair']['needsRecovery'])
    if runs is not None:
        known_work = (result['collection']['state'] != 'completed'
                      or result['investigation']['state'] not in {'completed', 'legacy'}
                      or result['repair']['state'] not in {'completed', 'completed_with_blocked', 'completed_with_skips', 'not_needed'})
        if (is_v3_scan(scan) and scan.get('status') == 'in_progress' and known_work
                and not _active_run(scan['id'], runs, scan.get('assignee_id'))):
            result['needsRecovery'].append(scan['id'])
            result['warnings'].append(f"{scan.get('identifier', scan['id'])} 处于 in_progress 且无活动运行，等待 recover")
        if is_v3_scan(scan) and result['investigation']['state'] == 'investigating':
            for lane in lanes:
                if (lane.get('status') == 'in_progress'
                        and not _active_run(lane['id'], runs, scan.get('assignee_id'))
                        and lane['id'] not in result['needsRecovery']):
                    result['needsRecovery'].append(lane['id'])
                    result['warnings'].append(f"{lane.get('identifier', lane['id'])} 处于 in_progress 且无活动运行，等待 recover")
    inv, fix = result['investigation'], result['repair']
    if result['collection']['state'] != 'completed':
        next_action = '继续完成本轮采集与覆盖核对，成功来源不重采'
    elif inv['state'] == 'registering':
        next_action = '完成本轮清单登记后调查'
    elif inv['state'] == 'investigating':
        next_action = '完成本轮剩余调查并汇总，再串行修复'
    elif inv['state'] == 'attention' or fix['state'] == 'attention':
        next_action = '核对阶段证据或暂停现场，保留已有结果'
    elif fix['state'] == 'blocked':
        next_action = '受阻项单列保留证据，不自动补验收；该批次仍停在阻塞位置，接续状态待核对'
    elif fix['state'] in {'paused', 'cancelled'}:
        next_action = '保留暂停或取消现场，等待真人决定后续范围'
    elif fix['state'] == 'not_needed':
        next_action = '本轮无新增可修项，保留完整调查结论'
    elif fix['state'] in {'completed', 'completed_with_skips'}:
        next_action = '工程处理已结束，按既有流程审查 PR 与交付报告'
    elif fix['state'] == 'completed_with_blocked':
        next_action = '固定队列已结束；受阻项单列保留证据，仅真人明确要求恢复时续作'
    elif fix['state'] == 'repairing':
        next_action = '当前项完整验收交付后，串行继续固定修复队列'
    else:
        next_action = '统一汇总调查结论，登记固定修复队列'
    result['nextAction'] = next_action
    return result
