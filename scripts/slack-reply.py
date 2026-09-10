#!/usr/bin/env python3
"""Send a Relay reply with a code-generated attribution footer."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
from pathlib import Path


def envelope_from_text(text):
    body = text.split('\n', 1)[1]
    lines = body.replace('\r\n', '\n').split('\n')
    start, end = '<!-- relay-payload:v1 -->', '<!-- /relay-payload -->'
    if start in lines or end in lines:
        if lines.count(start) != 1 or lines.count(end) != 1:
            raise ValueError('invalid_payload_block')
        block = '\n'.join(lines[lines.index(start) + 1:lines.index(end)]).strip()
        match = re.fullmatch(r'(`{3,})json\n([\s\S]*)\n\1', block)
        if not match:
            raise ValueError('invalid_payload_block')
        body = match[2]
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get('eventPayload'), dict):
        raise ValueError('invalid_payload')
    return data


def footer_for(config, envelope):
    name = config['displayName']
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or re.search(r'[\n\r<>]', name):
        raise ValueError('invalid_display_name')
    footer = name
    snapshot = envelope.get('replyContext')
    if not isinstance(snapshot, dict):
        return footer
    if (snapshot.get('type') != 'slack_reply_context' or snapshot.get('source') != 'agent_config'
            or snapshot.get('status') != 'available' or snapshot.get('agentId') != config['agentId']):
        return footer
    model = snapshot.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}', model):
        return footer
    footer += ' · 配置模型：' + model
    if snapshot.get('serviceTier') == 'priority':
        footer += ' · ⚡ Fast'
    return footer


def format_duration(seconds):
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 0 <= seconds <= 604800:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours}h {minutes:02d}m'
    if minutes:
        return f'{minutes}m {seconds:02d}s'
    return f'{seconds}s'


def icon(config, name):
    defaults = {'time': '⏱️', 'model': '🤖', 'tools': '🔧', 'skills': '🪄', 'github': '🔗', 'multica': '↗️'}
    value = config.get('icons', {}).get(name, defaults[name])
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or re.search(r'[\r\n<>`|]', value):
        raise ValueError('invalid_footer_icon')
    return value


def run_statistics(path, issue_id, task_id=None, config=None):
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('issue_id') != issue_id
            or (task_id is not None and data.get('run_id') != task_id)):
        raise ValueError('invalid_run_context')
    stats = data.get('statistics')
    if not isinstance(stats, dict):
        raise ValueError('invalid_run_context')
    config = config or {}
    rendered = []
    duration = format_duration(stats.get('duration_seconds'))
    if duration:
        rendered.append(icon(config, 'time') + ' ' + duration)
    model = stats.get('model')
    if (stats.get('model_source') == 'agent_config' and isinstance(model, str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}', model)):
        rendered.append(icon(config, 'model') + ' ' + model)
    for key in ('tools', 'skills'):
        value = stats.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10000:
            rendered.append(f'{icon(config, key)} {value} {key}')
    identifier, url = data.get('issue_identifier'), data.get('issue_url')
    if isinstance(identifier, str) and re.fullmatch(r'[A-Z][A-Z0-9]{0,31}-[1-9][0-9]{0,15}', identifier) and isinstance(url, str):
        target = urllib.parse.urlsplit(url)
        if (target.scheme == 'https' and target.hostname and not target.username and not target.password
                and not target.query and not target.fragment and not re.search(r'[\s<>|\\]', url)
                and re.fullmatch(r'/[a-z0-9]+(?:-[a-z0-9]+)*/issues/' + re.escape(issue_id), target.path)):
            rendered.append(f'{icon(config, "multica")} <{url}|{identifier}>')
    return ' · '.join(rendered) or None


def github_footers(path, config=None):
    if not path:
        return []
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('pullRequests'), list):
        raise ValueError('invalid_github_context')
    branches = data.get('branches', [])
    if not isinstance(branches, list) or len(data['pullRequests']) + len(branches) > 5:
        raise ValueError('invalid_github_context')
    rows, seen = [], set()
    for item in data['pullRequests']:
        if not isinstance(item, dict):
            raise ValueError('invalid_github_context')
        repository, branch, number, url = (item.get(key) for key in ('repository', 'branch', 'number', 'url'))
        if (not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
                or not isinstance(branch, str) or not 1 <= len(branch) <= 200 or re.search(r'[\r\n`<>]', branch)
                or not isinstance(number, int) or isinstance(number, bool) or number < 1
                or url != f'https://github.com/{repository}/pull/{number}'):
            raise ValueError('invalid_github_context')
        if url in seen:
            continue
        seen.add(url)
        repo = repository.split('/', 1)[1]
        branch = branch.replace('&', '&amp;')
        rows.append(f'{icon(config or {}, "github")} {repo} · `{branch}` · <{url}|PR #{number}>')
    branch_keys = set()
    for item in branches:
        if not isinstance(item, dict):
            raise ValueError('invalid_github_context')
        repository, branch = item.get('repository'), item.get('branch')
        if (not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
                or not isinstance(branch, str) or not 1 <= len(branch) <= 200 or re.search(r'[\r\n`<>]', branch)):
            raise ValueError('invalid_github_context')
        key = (repository, branch)
        if key in branch_keys or any(x['repository'] == repository and x['branch'] == branch for x in data['pullRequests']):
            continue
        branch_keys.add(key)
        rows.append(f'{icon(config or {}, "github")} {repository.split("/", 1)[1]} · `{branch.replace("&", "&amp;")}`')
    return rows


def render_reply(config, envelope, text, delivery_block_id=None, statistics=None, github_rows=None):
    event = envelope['eventPayload']
    if event.get('teamId') != config['teamId'] or not re.fullmatch(r'[CDG][A-Z0-9]+', event.get('channelId', '')):
        raise ValueError('invalid_reply_scope')
    if not re.fullmatch(r'\d+\.\d{1,6}', event.get('threadTs', '')):
        raise ValueError('invalid_thread')
    footer = footer_for(config, {} if statistics else envelope)
    body = text.strip()
    # Strip only an explicit attribution prefix, not a name in ordinary prose.
    body = re.sub(r'^\s*' + re.escape(config['displayName']) + r'\s*[:：]\s*', '', body, count=1).strip()
    if not body or len(body) > 35000:
        raise ValueError('invalid_reply_length')
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': body[i:i + 3000]}} for i in range(0, len(body), 3000)]
    context_lines = [line for line in [statistics, *(github_rows or []), footer] if line]
    for line in context_lines[:-1]:
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': line}]})
    context = {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': context_lines[-1]}]}
    if delivery_block_id:
        context['block_id'] = delivery_block_id
    blocks.append(context)
    return {'channel': event['channelId'], 'thread_ts': event['threadTs'], 'text': body + '\n\n' + '\n'.join(context_lines),
            'blocks': blocks, 'unfurl_links': False, 'unfurl_media': False}


def read_source(config, issue_id, comment_id, runner=subprocess.run):
    command = ['multica', '--server-url', config['serverUrl'], '--workspace-id', config['workspaceId']]
    def get(args):
        result = runner(command + args + ['--output', 'json'], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError('multica_read_failed')
        return json.loads(result.stdout)
    issue = get(['issue', 'get', issue_id])
    if (issue.get('workspace_id') != config['workspaceId'] or issue.get('project_id') != config['projectId']
            or issue.get('assignee_id') != config['agentId'] or issue.get('assignee_type') != 'agent'):
        raise ValueError('invalid_issue_scope')
    source = issue.get('description', '')
    if comment_id:
        rows = get(['issue', 'comment', 'list', issue_id, '--thread', comment_id, '--tail', '0'])
        row = next((row for row in rows if row.get('id') == comment_id), None)
        if not row:
            raise ValueError('source_comment_missing')
        source = row.get('content', '')
    if not source.startswith('<!-- relay-message:' if comment_id else '<!-- relay-thread:'):
        raise ValueError('invalid_relay_source')
    return envelope_from_text(source)


def delivery_identity(config, issue_id, comment_id):
    fields = [config['workspaceId'], config['projectId'], config['agentId'], issue_id, comment_id or '']
    return hashlib.sha256('\0'.join(fields).encode()).hexdigest()


def slack_call(token, method, payload=None, query=None, opener=urllib.request.urlopen, timeout=20):
    url = 'https://slack.com/api/' + method
    if query:
        url += '?' + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json; charset=utf-8'},
        method='GET' if payload is None else 'POST')
    with opener(request, timeout=timeout) as response:
        data = json.load(response)
    if data.get('ok') is not True:
        raise ValueError('slack_lookup_failed' if payload is None else 'slack_send_rejected')
    return data


def find_delivered_reply(token, envelope, block_id, oldest, opener=urllib.request.urlopen):
    event = envelope['eventPayload']
    cursor, seen = '', set()
    deadline = time.monotonic() + 15
    for _ in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('slack_lookup_timeout')
        query = {'channel': event['channelId'], 'ts': event['threadTs'], 'oldest': oldest,
                 'inclusive': 'true', 'limit': '200'}
        if cursor:
            query['cursor'] = cursor
        data = slack_call(token, 'conversations.replies', query=query, opener=opener,
                          timeout=max(0.1, min(10, remaining)))
        messages = data.get('messages')
        if not isinstance(messages, list):
            raise ValueError('slack_lookup_failed')
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get('blocks', []), list):
                continue
            if any(isinstance(block, dict) and block.get('block_id') == block_id for block in message['blocks']):
                message_ts = message.get('ts')
                if not isinstance(message_ts, str) or not re.fullmatch(r'\d+\.\d{1,6}', message_ts):
                    raise ValueError('slack_lookup_failed')
                return message_ts
        cursor = data.get('response_metadata', {}).get('next_cursor', '').strip()
        if not cursor:
            return None
        if cursor in seen:
            raise ValueError('slack_lookup_failed')
        seen.add(cursor)
    raise ValueError('reply_lookup_limit')


def delivery_paths(config_path, identity):
    root = config_path.resolve().parent / '.slack-reply-state'
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    return root / (identity + '.json'), root / (identity + '.lock')


def delivery_lock(lock_path):
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    handle = os.fdopen(fd, 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError('reply_delivery_busy')
    return handle


def read_delivery_state(state_path):
    if not state_path.exists():
        return None
    state = json.loads(state_path.read_text())
    if (not isinstance(state, dict) or state.get('version') != 1
            or state.get('phase') not in ('attempting', 'accepted', 'sent')):
        raise ValueError('invalid_delivery_state')
    if state['phase'] == 'attempting' and not re.fullmatch(r'\d+\.\d{6}', state.get('attemptedAt', '')):
        raise ValueError('invalid_delivery_state')
    if state['phase'] == 'attempting' and not re.fullmatch(r'\d+\.\d{6}', state.get('lookupFromTs', '')):
        raise ValueError('invalid_delivery_state')
    if state['phase'] in ('accepted', 'sent') and not re.fullmatch(r'\d+\.\d{1,6}', state.get('messageTs', '')):
        raise ValueError('invalid_delivery_state')
    return state


def write_delivery_state(state_path, state):
    fd, temporary = tempfile.mkstemp(prefix='.delivery-', dir=state_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(state, handle, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
        directory_fd = os.open(state_path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def slack_timestamp(now=None):
    value = time.time() if now is None else now
    seconds = int(value)
    return f'{seconds}.{int((value-seconds)*1_000_000):06d}'


def main(argv=None, opener=urllib.request.urlopen, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--issue-id', required=True)
    parser.add_argument('--comment-id')
    parser.add_argument('--text-file', required=True)
    parser.add_argument('--run-context-file')
    parser.add_argument('--github-context-file')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    envelope = read_source(config, args.issue_id, args.comment_id, runner)
    identity = delivery_identity(config, args.issue_id, args.comment_id)
    block_id = 'relay-delivery-' + identity
    stats = run_statistics(args.run_context_file, args.issue_id, os.environ.get('MULTICA_TASK_ID'), config)
    github_rows = github_footers(args.github_context_file, config)
    payload = render_reply(config, envelope, Path(args.text_file).read_text(), block_id, stats, github_rows)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False))
        return
    token = os.environ.get('SLACK_USER_TOKEN')
    if not token:
        raise ValueError('slack_user_token_missing')
    state_path, lock_path = delivery_paths(config_path, identity)
    with delivery_lock(lock_path):
        state = read_delivery_state(state_path)
        if state and state['phase'] == 'sent':
            print(json.dumps({'ok': True, 'duplicate': True, 'channel': payload['channel'], 'message_ts': state['messageTs'],
                              'thread_ts': payload['thread_ts'], 'footer': payload['blocks'][-1]['elements'][0]['text']}, ensure_ascii=False))
            return
        if state and state['phase'] == 'accepted':
            verified = find_delivered_reply(token, envelope, block_id, state['messageTs'], opener)
            if not verified:
                raise ValueError('slack_send_unverified')
            write_delivery_state(state_path, {'version': 1, 'phase': 'sent', 'messageTs': verified})
            print(json.dumps({'ok': True, 'duplicate': True, 'channel': payload['channel'], 'message_ts': verified,
                              'thread_ts': payload['thread_ts'], 'footer': payload['blocks'][-1]['elements'][0]['text']}, ensure_ascii=False))
            return
        if state and state['phase'] == 'attempting':
            existing = find_delivered_reply(token, envelope, block_id, state['lookupFromTs'], opener)
            if existing:
                write_delivery_state(state_path, {'version': 1, 'phase': 'sent', 'messageTs': existing})
                print(json.dumps({'ok': True, 'duplicate': True, 'channel': payload['channel'], 'message_ts': existing,
                                  'thread_ts': payload['thread_ts'], 'footer': payload['blocks'][-1]['elements'][0]['text']}, ensure_ascii=False))
                return
            # Absence after an ambiguous POST is not proof that Slack did not commit it.
            raise ValueError('slack_delivery_unknown')
        attempted_at = slack_timestamp()
        lookup_from = slack_timestamp(time.time() - 300)
        write_delivery_state(state_path, {'version': 1, 'phase': 'attempting', 'attemptedAt': attempted_at,
                                          'lookupFromTs': lookup_from})
        try:
            data = slack_call(token, 'chat.postMessage', payload=payload, opener=opener)
        except ValueError as error:
            if str(error) == 'slack_send_rejected':
                state_path.unlink(missing_ok=True)
            raise
        message_ts = data.get('ts')
        if not isinstance(message_ts, str) or not re.fullmatch(r'\d+\.\d{1,6}', message_ts):
            raise ValueError('slack_send_unverified')
        write_delivery_state(state_path, {'version': 1, 'phase': 'accepted', 'messageTs': message_ts})
        verified = find_delivered_reply(token, envelope, block_id, message_ts, opener)
        if not verified:
            raise ValueError('slack_send_unverified')
        write_delivery_state(state_path, {'version': 1, 'phase': 'sent', 'messageTs': verified})
        print(json.dumps({'ok': True, 'duplicate': False, 'channel': data.get('channel'), 'message_ts': verified,
                          'thread_ts': payload['thread_ts'], 'footer': payload['blocks'][-1]['elements'][0]['text']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Never echo HTTP bodies, command output, authorization headers or private source content.
        message = str(error) if type(error) is ValueError and re.fullmatch(r'[a-z_]+', str(error)) else 'reply_failed_verify_before_retry'
        print(json.dumps({'ok': False, 'error': message}), file=sys.stderr)
        sys.exit(1)
