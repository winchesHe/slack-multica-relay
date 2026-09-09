#!/usr/bin/env python3
"""采集当前 Multica 运行快照，准备并一次发送最终 Slack 回复。"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


class FinalReplyError(Exception):
    pass


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as file:
        os.chmod(path, 0o600)
        json.dump(value, file, ensure_ascii=False, indent=2)


def query(command):
    try:
        result = subprocess.run(["rtk", "proxy", *command], capture_output=True,
                                text=True, timeout=30, check=True)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError):
        raise FinalReplyError("只读查询失败，未发送；请检查 CLI 认证和目标") from None


def scope(env):
    keys = ("MULTICA_SERVER_URL", "MULTICA_WORKSPACE_ID", "MULTICA_TASK_ID")
    if any(not env.get(key) for key in keys):
        raise FinalReplyError("缺少 Multica Runtime 配置")
    return {key: env[key] for key in keys}


def snapshot(issue, env):
    identity = scope(env)
    cli = ["multica", "--server-url", identity["MULTICA_SERVER_URL"],
           "--workspace-id", identity["MULTICA_WORKSPACE_ID"]]
    runs = query(cli + ["issue", "runs", issue, "--output", "json"])
    matches = [r for r in runs if isinstance(r, dict) and r.get("id") == identity["MULTICA_TASK_ID"]]
    if len(matches) != 1:
        raise FinalReplyError("当前 run 未唯一命中，禁止改选其他运行")
    run = matches[0]
    if run.get("issue_id") != issue or run.get("workspace_id") != identity["MULTICA_WORKSPACE_ID"]:
        raise FinalReplyError("运行归属不匹配")
    model = None
    agent_id = run.get("agent_id")
    if agent_id:
        try:
            agent = query(cli + ["agent", "get", agent_id, "--output", "json"])
            if isinstance(agent, dict) and agent.get("id") == agent_id and agent.get("workspace_id") == identity["MULTICA_WORKSPACE_ID"]:
                candidate = agent.get("model")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", candidate.strip()):
                    model = candidate.strip()
        except FinalReplyError:
            pass
    try:
        messages = query(cli + ["issue", "run-messages", run["id"], "--issue", issue, "--output", "json"])
    except FinalReplyError:
        messages = []
    captured = datetime.now(timezone.utc).isoformat()
    # 不保存发起人、邮箱、完整任务正文等与统计无关的 run 字段。
    selected = {key: run[key] for key in ("id", "issue_id", "workspace_id", "agent_id", "started_at", "status") if key in run}
    if model:
        selected.update(model=model, model_source="agent_config")
    return {"version": 1, "scope": identity, "captured_at": captured, "run": selected, "messages": messages}


def valid_messages(data):
    messages = data.get("messages")
    run = data["run"]
    if not isinstance(messages, list) or len(messages) > 10000:
        return []
    if any(not isinstance(m, dict) or m.get("seq") != index + 1
           or m.get("task_id") != run["id"] or m.get("issue_id") != run["issue_id"]
           or m.get("type") not in ("text", "tool_use", "tool_result", "error")
           for index, m in enumerate(messages)):
        return []
    return messages


def words(message):
    if message.get("tool") != "exec_command":
        return []
    value = message.get("input") or {}
    command = value.get("command", value.get("cmd", ""))
    try:
        parsed = shlex.split(command)
        if len(parsed) == 3 and Path(parsed[0]).name in ("zsh", "sh", "bash") and parsed[1] in ("-c", "-lc"):
            command = parsed[2]
            parsed = shlex.split(command)
        if any(char in command for char in (";", "|", "&", "`", "$", "\n", ">", "<")):
            return []
        if parsed and parsed[0] == "rtk":
            parsed = parsed[2:] if len(parsed) > 1 and parsed[1] == "proxy" else parsed[1:]
        return parsed
    except (ValueError, TypeError):
        return []


def statistics(data):
    parts = []
    try:
        seconds = round((datetime.fromisoformat(data["captured_at"]) - datetime.fromisoformat(data["run"]["started_at"].replace("Z", "+00:00"))).total_seconds())
        if seconds >= 0:
            minutes = f"{seconds // 60}m " if seconds >= 60 else ""
            parts.append(f":agent_time: {minutes}{seconds % 60}s")
    except (ValueError, KeyError, TypeError):
        pass
    model = data["run"].get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
        parts.append(f":agent_mdi_robot_outline_muted: {model}")
    messages = valid_messages(data)
    calls = [m for m in messages if m["type"] == "tool_use"]
    if calls:
        parts.append(f":agent_tool: {len(calls)} tools")
    names = set()
    known = True
    pending = []
    for message in messages:
        if message["type"] == "tool_use":
            pending.append(message)
            continue
        if message["type"] != "tool_result":
            continue
        call = pending[0] if len(pending) == 1 else None
        candidates = [p for p in pending if p.get("tool") == message.get("tool")]
        for candidate in candidates:
            if "SKILL.md" not in json.dumps(candidate.get("input", {})):
                continue
            parsed = words(candidate)
            if len(parsed) > 1 and parsed[1] == "--":
                parsed = [parsed[0], *parsed[2:]]
            paths = parsed[1:]
            is_read = bool(paths) and parsed[0] in ("cat", "/bin/cat") and all(
                re.fullmatch(r"(?:/|~/)[^*?\[\]]+/SKILL\.md", path) for path in paths)
            if not is_read or call != candidate or candidate["seq"] + 1 != message["seq"]:
                known = False
                continue
            output = message.get("output") or ""
            headers = re.findall(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", output, re.S | re.M)
            loaded = [re.findall(r"^name:\s*['\"]?([A-Za-z0-9_:/.-]+)['\"]?\s*$", header, re.M) for header in headers]
            # 批量 cat 仅在每个路径都返回一个完整且唯一的 frontmatter 时计数。
            if len(loaded) == len(paths) and all(len(name) == 1 for name in loaded):
                names.update(name[0] for name in loaded)
            elif not re.match(r"cat: .*: (No such file or directory|Permission denied)", output):
                known = False
        if candidates:
            pending.remove(candidates[0])
    if any("SKILL.md" in json.dumps(c.get("input", {})) for c in pending):
        known = False
    if known and names:
        parts.append(f":agent_skill: {len(names)} skills")
    return " · ".join(parts)


def artifacts(data, selection):
    messages = {m["seq"]: m for m in valid_messages(data)}
    if not isinstance(selection, list) or len(selection) > 10:
        raise FinalReplyError("PR 选择必须是最多 10 项的数组")
    result = []
    seen = set()
    for item in selection:
        url = item.get("url", "")
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)", url)
        if not match:
            raise FinalReplyError("PR URL 无效")
        evidence = messages.get(item.get("evidence_seq"), {})
        call = evidence if evidence.get("type") == "tool_use" else messages.get(evidence.get("seq", 0) - 1, {})
        command = words(call)
        # 只接受明确的 gh pr 操作；读取文档或一般讨论不能给 Footer 注入链接。
        if command[:2] != ["gh", "pr"] or len(command) < 4 or command[2] not in ("view", "diff", "checks", "review", "create", "edit", "merge"):
            raise FinalReplyError("PR 缺少当前运行中的直接工具证据")
        repo, number = match.groups()
        explicit_target = url in command or (number in command and any(command[i:i + 2] in (["--repo", repo], ["-R", repo]) for i in range(len(command))))
        output_urls = re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*(?![0-9])", evidence.get("output", ""))
        returned_url = evidence.get("type") == "tool_result" and evidence.get("tool") == call.get("tool") and url in output_urls
        if not explicit_target and not returned_url:
            raise FinalReplyError("PR 与证据目标不匹配")
        if url in seen:
            continue
        verified = query(["gh", "pr", "view", url, "--json", "url,headRefName,number"])
        if verified.get("url") != url or verified.get("number") != int(number) or not verified.get("headRefName"):
            raise FinalReplyError("GitHub 回读与 PR 目标不匹配")
        seen.add(url)
        result.append({"url": url, "repository": repo, "branch": verified["headRefName"], "number": int(number)})
    return result


def escape(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;").replace("|", "&#124;")


def prepare(data, selection, text, blocks, output):
    if not text.strip() or not isinstance(blocks, list) or not blocks:
        raise FinalReplyError("最终正文和 blocks 不能为空")
    stats = statistics(data)
    rows = artifacts(data, selection)
    lines = ([stats] if stats else []) + [f":agent_mdi_github: {escape(r['repository'].split('/')[-1])} · `{escape(r['branch'])}` · <{r['url']}|PR #{r['number']}>" for r in rows]
    if any(len(line) > 2000 for line in lines) or len(blocks) + len(lines) > 50:
        raise FinalReplyError("消息超出 Block Kit 限制，未发送")
    full_blocks = blocks + [{"type": "context", "elements": [{"type": "mrkdwn", "text": line}]} for line in lines]
    full_text = text + ("\n\n" + "\n".join(lines) if lines else "")
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    text_path, blocks_path = output / "text.txt", output / "blocks.json"
    text_path.write_text(full_text, encoding="utf-8")
    os.chmod(text_path, 0o600)
    write_new(blocks_path, full_blocks)
    bundle = {"version": 1, "scope": data["scope"], "issue": data["run"]["issue_id"],
              "text_sha256": hashlib.sha256(text_path.read_bytes()).hexdigest(),
              "blocks_sha256": hashlib.sha256(blocks_path.read_bytes()).hexdigest()}
    write_new(output / "bundle.json", bundle)
    return {"bundle": str((output / "bundle.json").resolve()), "statistics": stats, "artifacts": rows}


def send(path, channel, thread, dry_run, env):
    path = Path(path).resolve()
    bundle = read(path)
    if bundle["scope"] != scope(env):
        raise FinalReplyError("bundle 不属于当前运行")
    for name in ("text", "blocks"):
        file = path.parent / ("text.txt" if name == "text" else "blocks.json")
        if hashlib.sha256(file.read_bytes()).hexdigest() != bundle[name + "_sha256"]:
            raise FinalReplyError("准备后的消息内容已变化，未发送")
    helper = Path(env.get("RELAY_REPLY_SCRIPT", "")).expanduser()
    if not helper.is_absolute() or not helper.is_file():
        raise FinalReplyError("Runtime 最终回复入口不存在")
    spec = importlib.util.spec_from_file_location("relay_reply", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = argparse.Namespace(issue=bundle["issue"], channel=channel, thread_ts=thread,
        text_file=str(path.parent / "text.txt"), blocks_file=str(path.parent / "blocks.json"),
        format="markdown", dry_run=dry_run)
    try:
        return module.execute(args, env, register_footer=False)
    except module.ReplyError as error:
        raise FinalReplyError(str(error)) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--issue", required=True)
    snap.add_argument("--output", required=True)
    prep = sub.add_parser("prepare")
    for option in ("snapshot", "artifacts", "text-file", "blocks-file", "output-dir"):
        prep.add_argument("--" + option, required=True)
    post = sub.add_parser("send")
    for option in ("bundle", "channel", "thread-ts"):
        post.add_argument("--" + option, required=True)
    post.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            data = snapshot(args.issue, os.environ)
            write_new(args.output, data)
            result = {"snapshot": args.output, "messages": len(valid_messages(data)), "captured_at": data["captured_at"]}
        elif args.command == "prepare":
            result = prepare(read(args.snapshot), read(args.artifacts), Path(args.text_file).read_text(encoding="utf-8"), read(args.blocks_file), args.output_dir)
        else:
            result = send(args.bundle, args.channel, args.thread_ts, args.dry_run, os.environ)
    except Exception as error:
        print(json.dumps({"error": str(error) if isinstance(error, FinalReplyError) else "最终回复失败，请核对回执，禁止自动重发"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
