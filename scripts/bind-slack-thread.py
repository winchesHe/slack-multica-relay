#!/usr/bin/env python3
"""确定性渲染或预绑定 Slack thread；仅使用 Python 3 标准库与 multica CLI。"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

PAYLOAD_START = "<!-- relay-payload:v1 -->"
PAYLOAD_END = "<!-- /relay-payload -->"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialize(value, pretty=False):
    text = json.dumps(value, ensure_ascii=False, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":"))
    return text.replace("@", r"\u0040").replace("<", r"\u003c").replace(">", r"\u003e")


def quote_message(text):
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"([\\`*_\[\]{}()!#|~])", r"\\\1", text)
    return "\n".join("> " + line for line in re.split(r"\r?\n", text))


def validate_body(body):
    # 原正文原样保留；遇到保留协议标记直接拒绝，不静默转义或删除 Jira 内容。
    if any(marker in body for marker in ("<!-- relay-thread:", PAYLOAD_START, PAYLOAD_END)):
        raise ValueError("原正文包含 Relay 保留标记，拒绝拼接")


def render(args, body=""):
    validate_body(body)
    scope_id = args.scope_id
    if scope_id is None:
        scope_id = args.assignee_id if args.assignee_type == "agent" else "squad:" + args.assignee_id
    scope = digest(f"{args.workspace_id}:{args.project_id}:{scope_id}")
    thread_digest = digest(f"{args.team_id}:{args.channel_id}:{args.root_ts}")
    marker = f"<!-- relay-thread:{scope}:{thread_digest} -->"
    event = {
        "teamId": args.team_id, "channelId": args.channel_id,
        "messageTs": args.root_ts, "threadTs": args.root_ts,
        "senderUserId": args.sender_user_id, "text": args.root_text,
        "mention": {"type": "user", "id": args.mention_user_id},
    }
    envelope = {"eventPayload": event}
    compact = serialize(envelope)
    if len(compact.encode("utf-8")) > 48 * 1024:
        raise ValueError("relay_payload_too_large")
    pretty = serialize(envelope, pretty=True)
    fence = "`" * max([3] + [len(run) + 1 for run in re.findall(r"`+", pretty)])
    clipped = args.root_text.encode("utf-8")[:4096].decode("utf-8", errors="ignore")
    quote = quote_message(clipped)
    if len(args.root_text.encode("utf-8")) > 4096:
        quote += "\n> （展示已截断，完整请求见数据区）"
    try:
        timestamp = datetime.fromtimestamp(float(args.root_ts), ZoneInfo("Asia/Shanghai"))
        time = timestamp.strftime("%Y-%m-%d %H:%M:%S") + "（Asia/Shanghai）"
    except (ValueError, OverflowError, OSError):
        time = "未知"
    channel_url = "https://slack.com/app_redirect?" + urlencode({"team": args.team_id, "channel": args.channel_id})

    def description(value):
        parts = [marker, "## Slack 原始消息", quote, "## 来源",
                 f"- Slack 频道：[打开频道]({channel_url})", f"- 触发时间：{time}",
                 "- 附件：0 个", "## 事件上下文", PAYLOAD_START,
                 f"{fence}json\n{value}\n{fence}", PAYLOAD_END]
        result = "\n\n".join(parts).replace(marker + "\n\n", marker + "\n", 1)
        return result + ("\n\n" + body if body else "")

    result = description(pretty)
    if len(result.encode("utf-8")) > 64 * 1024:
        result = description(compact)
    if len(result.encode("utf-8")) > 64 * 1024:
        raise ValueError("task_presentation_too_large")
    return {"scope": scope, "threadDigest": thread_digest, "marker": marker, "description": result}


def read_binding(description, rendered, args):
    marker = rendered["marker"]
    if not description.startswith(marker + "\n") or description.count("<!-- relay-thread:") != 1:
        raise ValueError("Issue 已绑定不同 thread，或 marker 不在首行")
    if description.count(PAYLOAD_START) != 1 or description.count(PAYLOAD_END) != 1:
        raise ValueError("Issue payload 标记不唯一或缺失")
    lines = description.replace("\r\n", "\n").split("\n")
    start, end = lines.index(PAYLOAD_START), lines.index(PAYLOAD_END)
    block = "\n".join(lines[start + 1:end]).strip()
    match = re.fullmatch(r"(`{3,})json\n([\s\S]*)\n\1", block)
    if end < start or not match:
        raise ValueError("Issue payload 围栏损坏")
    payload = json.loads(match[2])["eventPayload"]
    expected = {"teamId": args.team_id, "channelId": args.channel_id,
                "threadTs": args.root_ts, "messageTs": args.root_ts}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("Issue payload 与目标 thread 不一致")


def apply(args, runner=subprocess.run):
    command = ["multica", "--server-url", args.server_url, "--workspace-id", args.workspace_id]

    def call(arguments):
        result = runner(command + arguments, check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)

    def get():
        issue = call(["issue", "get", args.issue_id, "--output", "json"])
        if (issue["id"] != args.issue_id or issue["workspace_id"] != args.workspace_id
                or issue["project_id"] != args.project_id):
            raise ValueError("Issue 身份、工作区或项目不匹配")
        description = issue["description"]
        if description is None:
            return ""
        if not isinstance(description, str):
            raise ValueError("Issue description 类型错误")
        return description

    current = get()
    rendered = render(args)
    if "<!-- relay-thread:" in current:
        read_binding(current, rendered, args)
        return {"issueId": args.issue_id, "marker": rendered["marker"], "action": "unchanged"}
    rendered = render(args, current)
    if get() != current:
        raise ValueError("Issue 描述已变化，停止写入；重新读取后再执行")
    # 参数数组保持正文原样，不经 shell；不更改归属、状态或启动运行。
    call(["issue", "update", args.issue_id, "--description", rendered["description"],
          "--no-start", "--output", "json"])
    saved = get()
    read_binding(saved, rendered, args)
    if saved != rendered["description"]:
        raise ValueError("写后回读与候选描述不一致")
    return {"issueId": args.issue_id, "marker": rendered["marker"], "action": "bound"}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render-only", action="store_true")
    mode.add_argument("--apply", action="store_true")
    for name in ("workspace-id", "project-id", "assignee-id", "team-id", "channel-id",
                 "root-ts", "sender-user-id", "mention-user-id", "root-text"):
        result.add_argument("--" + name, required=True)
    result.add_argument("--assignee-type", choices=("agent", "squad"), default="agent")
    result.add_argument("--scope-id")
    result.add_argument("--body-file", type=Path)
    result.add_argument("--issue-id")
    result.add_argument("--server-url", default="https://multica.devops.moego.dev")
    return result


def main(argv=None, runner=subprocess.run):
    arguments = parser()
    args = arguments.parse_args(argv)
    for name in ("workspace_id", "project_id", "assignee_id", "scope_id"):
        value = getattr(args, name)
        if value is not None and (not value.strip() or value != value.strip()):
            arguments.error(f"{name} 必须是非空且无首尾空白的字符串")
    for name in ("team_id", "channel_id", "sender_user_id", "mention_user_id"):
        if not re.fullmatch(r"[A-Z][A-Z0-9]+", getattr(args, name)):
            arguments.error(f"{name} 不是合法 Slack ID")
    if not re.fullmatch(r"[0-9]+\.[0-9]{1,6}", args.root_ts):
        arguments.error("root-ts 必须保留 Slack 原始时间戳精度")
    if args.apply:
        if not args.issue_id or args.body_file:
            arguments.error("--apply 必须指定 --issue-id，正文直接从 Issue 读取，不接受 --body-file")
        try:
            if str(UUID(args.issue_id)) != args.issue_id:
                raise ValueError()
            server = urlsplit(args.server_url)
            if server.scheme != "https" or not server.hostname or server.username or server.password or server.query or server.fragment:
                raise ValueError()
        except ValueError:
            arguments.error("issue-id 必须是规范 UUID，server-url 必须是无凭据的 HTTPS 地址")
    elif args.issue_id:
        arguments.error("--issue-id 只用于 --apply")
    try:
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else ""
        result = apply(args, runner) if args.apply else render(args, body)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        # CLI 的异常可包含完整命令、私有正文或响应；失败只输出稳定诊断。
        print("预绑定失败：检查 Issue 归属、保留标记、CLI 状态与写后回读；结果未知先 get，勿盲目重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
