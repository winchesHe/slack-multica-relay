#!/usr/bin/env python3
"""每日 Bug 值守确定性预过滤（0 token）：
在模型初筛前先自动归类已有 CS 工单、历史或本轮重复、以及无窗口内客户正文的 Intercom 会话，
并将剩余需模型判断的条目压缩成紧凑摘要，避免大上下文导致的漏筛或过宽筛选。"""
import argparse
import json
from pathlib import Path
import sys


def _norm_url(url):
    return (url or '').strip().rstrip('/')


def _extract_in_window_customer_text(item):
    messages = item.get('messages')
    if not isinstance(messages, list):
        return None, False
    texts = []
    has_attachments = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('attachments') or msg.get('attachmentUrls') or '[Image]' in str(msg.get('body') or msg.get('text') or ''):
            has_attachments = True
        if msg.get('inWindow') is not True:
            continue
        author = str(msg.get('authorType') or msg.get('author_type') or msg.get('role') or '').lower()
        if author and author not in {'user', 'lead', 'customer', 'contact'}:
            continue
        body = str(msg.get('body') or msg.get('text') or msg.get('content') or '').strip()
        if body:
            texts.append(body)
    return '\n'.join(texts).strip(), has_attachments


def prescreen_artifacts(artifacts, *, known_urls=None, known_object_ids=None, max_text_chars=800):
    seen_urls = {_norm_url(u) for u in (known_urls or []) if _norm_url(u)}
    seen_obj_ids = {str(x).strip() for x in (known_object_ids or []) if str(x).strip()}
    auto_classified = []
    needs_screening = []
    counts = {'total': 0, 'csFollowed': 0, 'duplicate': 0, 'autoNonBug': 0, 'needsLlmScreening': 0}

    for art in artifacts:
        manifest = art.get('manifest') if isinstance(art, dict) else {}
        run_id = (manifest or {}).get('runId') or ''
        evidence = art.get('evidence') if isinstance(art, dict) else None
        if not isinstance(evidence, list):
            raise ValueError('runArtifact 缺少 evidence 列表')
        for item in evidence:
            if not isinstance(item, dict):
                continue
            counts['total'] += 1
            source = str(item.get('sourceKey') or (manifest or {}).get('sourceKey') or '').lower()
            ev_id = str(item.get('evidenceId') or '')
            obj_id = str(item.get('sourceObjectId') or '').strip()
            url = _norm_url(item.get('sourceUrl') or item.get('slackUrl') or '')
            obj_key = f'{source}:{obj_id}' if source and obj_id else obj_id
            cs_tickets = [str(t).strip() for t in (item.get('csTickets') or []) if str(t).strip()]
            jira_keys = [str(k).strip() for k in (item.get('jiraKeys') or []) if str(k).strip()]

            if cs_tickets:
                counts['csFollowed'] += 1
                auto_classified.append({
                    'evidenceId': ev_id, 'sourceKey': source, 'sourceObjectId': obj_id,
                    'sourceUrl': url, 'runId': run_id, 'classification': 'cs_followed',
                    'reason': 'intercom_cs_tickets_non_empty', 'csTickets': cs_tickets,
                })
                if url:
                    seen_urls.add(url)
                if obj_key:
                    seen_obj_ids.add(obj_key)
                continue

            if (url and url in seen_urls) or (obj_key and obj_key in seen_obj_ids):
                counts['duplicate'] += 1
                auto_classified.append({
                    'evidenceId': ev_id, 'sourceKey': source, 'sourceObjectId': obj_id,
                    'sourceUrl': url, 'runId': run_id, 'classification': 'duplicate',
                    'reason': 'matched_known_or_in_round_source',
                })
                continue

            if url:
                seen_urls.add(url)
            if obj_key:
                seen_obj_ids.add(obj_key)

            if source == 'intercom' and isinstance(item.get('messages'), list):
                text, has_att = _extract_in_window_customer_text(item)
                if not text and not has_att:
                    counts['autoNonBug'] += 1
                    auto_classified.append({
                        'evidenceId': ev_id, 'sourceKey': source, 'sourceObjectId': obj_id,
                        'sourceUrl': url, 'runId': run_id, 'classification': 'non_bug',
                        'reason': 'no_customer_message_in_window',
                    })
                    continue
                snippet = text[:max_text_chars]
                entry = {
                    'evidenceId': ev_id, 'sourceKey': source, 'sourceObjectId': obj_id,
                    'sourceUrl': url, 'runId': run_id, 'updatedAt': item.get('updatedAt'),
                    'inWindowCustomerText': snippet, 'hasAttachments': has_att,
                    'moegoCompanyIds': (item.get('moegoCompanyIds') or [])[:10],
                }
                if jira_keys:
                    entry['jiraKeysHint'] = jira_keys
                needs_screening.append(entry)
            else:
                body = str(item.get('content') or '').strip()
                entry = {
                    'evidenceId': ev_id, 'sourceKey': source, 'sourceObjectId': obj_id,
                    'sourceUrl': url, 'runId': run_id, 'createdAt': item.get('createdAt'),
                    'title': str(item.get('title') or '').strip(),
                    'content': body[:max_text_chars],
                    'hasAttachments': bool(item.get('imageURLs')),
                }
                if jira_keys:
                    entry['jiraKeysHint'] = jira_keys
                needs_screening.append(entry)
            counts['needsLlmScreening'] += 1

    return {
        'schemaVersion': 1,
        'summary': counts,
        'autoClassified': auto_classified,
        'needsScreening': needs_screening,
    }


def _load_known(path):
    if not path:
        return [], []
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(raw, list):
        return raw, []
    if isinstance(raw, dict):
        return raw.get('urls') or [], raw.get('objectIds') or []
    raise ValueError('known-urls-json 必须是 URL 列表或 {urls, objectIds} 对象')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', action='append', required=True, help='collect-daily 生成的 runArtifact JSON 路径，可多次传入')
    parser.add_argument('--known-urls-json', help='可选：历史已调查的 URL 列表或 {urls, objectIds} JSON 文件')
    parser.add_argument('--max-text-chars', type=int, default=800)
    parser.add_argument('--output', help='输出结果 JSON 文件路径（省略则打印到 stdout）')
    args = parser.parse_args(argv)
    artifacts = [json.loads(Path(p).read_text(encoding='utf-8')) for p in args.artifact]
    known_urls, known_obj_ids = _load_known(args.known_urls_json)
    result = prescreen_artifacts(
        artifacts, known_urls=known_urls, known_object_ids=known_obj_ids, max_text_chars=args.max_text_chars)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + '\n', encoding='utf-8')
        print(json.dumps({'output': args.output, 'summary': result['summary']}, ensure_ascii=False))
    else:
        print(payload)
    return 0


if __name__ == '__main__':
    sys.exit(main())
