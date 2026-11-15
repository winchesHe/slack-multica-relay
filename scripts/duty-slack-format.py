#!/usr/bin/env python3
"""将每日 Bug 值守的结构化内容渲染为固定 Slack 样式；不执行发送。"""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


LAYOUTS = {
    'delivered': (
        ('story', '问题背景 / Story'), ('cause', '问题根因'),
        ('scope', '修复范围 / Scope'), ('impact', '影响面'),
        ('prs', 'PR'), ('acceptance', '验收结论与报告'), ('next', '下一步'),
    ),
    'blocked': (
        ('story', '问题背景 / Story'), ('cause', '问题根因'),
        ('progress', '当前进度'), ('blocker', '受阻原因'),
        ('conclusion', '处理结论'), ('prs', 'PR'), ('next', '下一步'),
    ),
    'update': (('prs', 'PR'), ('acceptance', '验收结论')),
}


def plain(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError('文字必须为 1–2000 字符的非空字符串')
    # 输入只承载事实；格式和通知由结构化字段生成，避免再次混用两种语法。
    if re.search(r'[\x00-\x1f<>`*]|\\[nrt]|\[[^\]]*\]\(|https?://|@', value):
        raise ValueError('文字不得包含换行、Markdown、裸链接或 @；请使用结构化行内元素')
    return value


def link(value):
    if not isinstance(value, dict) or set(value) != {'label', 'url'}:
        raise ValueError('链接必须包含且仅包含 label、url')
    label, url = plain(value['label']), value['url']
    if not isinstance(url, str) or len(url) > 2000 or re.search(r'[\s<>|]', url):
        raise ValueError('链接地址无效')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('链接必须使用无凭据的完整 HTTPS 地址')
    return {'type': 'link', 'text': label, 'url': url}


def inline(value):
    parts = [value] if isinstance(value, str) else value
    if not isinstance(parts, list) or not 1 <= len(parts) <= 30:
        raise ValueError('每行必须是文字或 1–30 个行内元素')
    result = []
    for part in parts:
        if isinstance(part, str):
            result.append({'type': 'text', 'text': plain(part)})
        elif isinstance(part, dict) and set(part) == {'code'}:
            result.append({'type': 'text', 'text': plain(part['code']), 'style': {'code': True}})
        else:
            result.append(link(part))
    return result


def section(elements):
    return {'type': 'rich_text_section', 'elements': elements}


def text(value, bold=False):
    return {'type': 'text', 'text': value, **({'style': {'bold': True}} if bold else {})}


def fallback(elements):
    result = []
    for element in elements:
        if element['type'] == 'link':
            result.append(f"{element['text']}（{element['url']}）")
        elif element['type'] == 'user':
            result.append(f"<@{element['user_id']}>")
        else:
            result.append(element['text'])
    return ''.join(result)


def render(data):
    if not isinstance(data, dict) or data.get('kind') not in LAYOUTS:
        raise ValueError('kind 必须为 delivered、blocked 或 update')
    kind = data['kind']
    required = {'kind', 'summary', 'issue', 'sections'}
    required |= {'version'} if kind == 'update' else {'current', 'total', 'source'}
    if not required <= data.keys() or data.keys() - required - {'notes', 'notify'}:
        raise ValueError('缺少必填字段或存在未支持的字段')
    issue = link(data['issue'])
    if not re.fullmatch(r'GRM-[1-9][0-9]*', issue['text']):
        raise ValueError('issue.label 必须为真实 GRM 任务编号')
    parsed = urlsplit(issue['url'])
    if parsed.path != '/grm/issues/' + issue['text'] or parsed.query or parsed.fragment:
        raise ValueError('任务链接必须使用 /grm/issues/GRM-编号')
    summary = plain(data['summary'])
    if kind == 'update':
        version = data['version']
        if type(version) is not int or version < 1:
            raise ValueError('version 必须为实际正整数版本')
        title = f"每日 Bug 值守｜{issue['text']}｜{summary}（第 {version} 版）"
    else:
        current, total = data['current'], data['total']
        if type(current) is not int or type(total) is not int or not 1 <= current <= total:
            raise ValueError('队列位置必须满足 1 <= current <= total')
        title = f'每日 Bug 值守｜{current}/{total}｜{summary}'
        if kind == 'blocked':
            title += '（受阻）'
    blocks = [{'type': 'rich_text', 'elements': [section([text(title, bold=True)])]}]
    lines = [title]
    if kind != 'update':
        source = link(data['source'])
        elements = [text('问题来源链接：', bold=True), source, text('\n'),
                    text('Multica 任务链接：', bold=True), issue]
        blocks.append({'type': 'rich_text', 'elements': [section(elements)]})
        lines.append(fallback(elements))
    sections = data['sections']
    layout = LAYOUTS[kind]
    if not isinstance(sections, dict) or set(sections) != {key for key, _ in layout}:
        raise ValueError('sections 必须完整匹配当前模板，禁止删减或改名栏目')
    for key, label in layout:
        entries = sections[key]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
            raise ValueError(f'{key} 必须为 1–10 条内容；未知或未完成请如实说明')
        rows = [inline(entry) for entry in entries]
        body = [section([text(label + '：', bold=True)])]
        if key == 'next' or kind == 'update':
            body.extend(section(row) for row in rows)
            lines.append(label + '：\n' + '\n'.join(fallback(row) for row in rows))
        else:
            body.append({'type': 'rich_text_list', 'style': 'bullet',
                         'elements': [section(row) for row in rows]})
            lines.append(label + '：\n' + '\n'.join('• ' + fallback(row) for row in rows))
        blocks.append({'type': 'rich_text', 'elements': body})
    notes = data.get('notes', [])
    if not isinstance(notes, list) or len(notes) > 3 or (kind == 'update' and notes):
        raise ValueError('首次消息最多 3 条补充说明，后续简报不增加补充栏目')
    for note in notes:
        row = inline(note)
        blocks.append({'type': 'rich_text', 'elements': [section(row)]})
        lines.append(fallback(row))
    notify = data.get('notify', [])
    if not isinstance(notify, list) or len(notify) > 10 or len(set(map(str, notify))) != len(notify):
        raise ValueError('notify 必须为不重复的 Slack user ID 列表，最多 10 人')
    if notify:
        if any(not isinstance(uid, str) or not re.fullmatch(r'U[A-Z0-9]+', uid) for uid in notify):
            raise ValueError('通知对象必须为已核实且已获授权的 Slack user ID')
        elements = [text('受阻通知：' if kind == 'blocked' else '通知：', bold=True)]
        for uid in notify:
            elements.extend([{'type': 'user', 'user_id': uid}, text(' ')])
        blocks.append({'type': 'rich_text', 'elements': [section(elements)]})
        lines.append(fallback(elements).rstrip())
    body = '\n\n'.join(lines) + '\n'
    if len(body) > 12000:
        raise ValueError('正文超过 12000 字符，请精简并将细节保留在报告中')
    return blocks, body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='结构化内容 JSON')
    parser.add_argument('--output-dir', required=True, type=Path, help='任务私有输出目录')
    args = parser.parse_args()
    if args.input.resolve() in {(args.output_dir / name).resolve() for name in ('blocks.json', 'fallback.txt')}:
        parser.error('输出不得覆盖输入文件')
    if args.input.stat().st_size > 200000:
        parser.error('输入不得超过 200 KB')
    try:
        blocks, body = render(json.loads(args.input.read_text(encoding='utf-8')))
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'blocks.json').write_text(json.dumps(blocks, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output_dir / 'fallback.txt').write_text(body, encoding='utf-8')
    print(json.dumps({'blocks': str(args.output_dir / 'blocks.json'),
                      'text': str(args.output_dir / 'fallback.txt')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
