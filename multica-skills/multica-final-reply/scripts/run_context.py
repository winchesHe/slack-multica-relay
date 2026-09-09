#!/usr/bin/env python3
"""只读采集 Multica 当前运行，提取统计和 PR／分支工具证据。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


class RunContextError(Exception):
    pass


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
        raise RunContextError("只读查询失败；请检查 CLI 认证和目标") from None


def scope(env):
    keys = ("MULTICA_SERVER_URL", "MULTICA_WORKSPACE_ID", "MULTICA_TASK_ID")
    if any(not env.get(key) for key in keys):
        raise RunContextError("缺少 Multica Runtime 配置")
    return {key: env[key] for key in keys}


def snapshot(issue, env):
    identity = scope(env)
    cli = ["multica", "--server-url", identity["MULTICA_SERVER_URL"],
           "--workspace-id", identity["MULTICA_WORKSPACE_ID"]]
    runs = query(cli + ["issue", "runs", issue, "--output", "json"])
    matches = [r for r in runs if isinstance(r, dict) and r.get("id") == identity["MULTICA_TASK_ID"]]
    if len(matches) != 1:
        raise RunContextError("当前 run 未唯一命中，禁止改选其他运行")
    run = matches[0]
    if run.get("issue_id") != issue or run.get("workspace_id") != identity["MULTICA_WORKSPACE_ID"]:
        raise RunContextError("运行归属不匹配")
    model = None
    agent_id = run.get("agent_id")
    if agent_id:
        try:
            agent = query(cli + ["agent", "get", agent_id, "--output", "json"])
            if isinstance(agent, dict) and agent.get("id") == agent_id and agent.get("workspace_id") == identity["MULTICA_WORKSPACE_ID"]:
                candidate = agent.get("model")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", candidate.strip()):
                    model = candidate.strip()
        except RunContextError:
            pass
    try:
        messages = query(cli + ["issue", "run-messages", run["id"], "--issue", issue, "--output", "json"])
    except RunContextError:
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
    stats = {}
    try:
        seconds = round((datetime.fromisoformat(data["captured_at"]) - datetime.fromisoformat(data["run"]["started_at"].replace("Z", "+00:00"))).total_seconds())
        if seconds >= 0:
            stats["duration_seconds"] = seconds
    except (ValueError, KeyError, TypeError):
        pass
    model = data["run"].get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
        stats.update(model=model, model_source="agent_config")
    messages = valid_messages(data)
    calls = [m for m in messages if m["type"] == "tool_use"]
    if calls:
        stats["tools"] = len(calls)
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
        stats.update(skills=len(names), skill_names=sorted(names))
    return stats


def code_evidence(messages):
    evidence = []
    pending = []
    selected = {}
    for message in messages:
        if message["type"] == "tool_use":
            pending.append(message)
            command = words(message)
            is_pr = command[:2] == ["gh", "pr"]
            git_args = command[3:] if len(command) > 3 and command[1] == "-C" else command[1:]
            is_git = bool(git_args) and command[0] == "git" and git_args[0] in (
                "branch", "status", "remote", "rev-parse")
            if is_pr or is_git:
                row = {"call_seq": message["seq"], "command": shlex.join(command)}
                evidence.append(row)
                selected[message["seq"]] = row
        elif message["type"] == "tool_result":
            candidates = [call for call in pending if call.get("tool") == message.get("tool")]
            # API 没有 call_id；并发或不相邻时保留调用证据，不猜结果归属。
            if len(pending) == 1 and candidates and candidates[0]["seq"] + 1 == message["seq"]:
                row = selected.get(candidates[0]["seq"])
                output = message.get("output")
                if row is not None and isinstance(output, str):
                    row.update(result_seq=message["seq"], output=output[:4000], output_truncated=len(output) > 4000)
            if candidates:
                pending.remove(candidates[0])
    return evidence


def summarize(data):
    messages = valid_messages(data)
    return {"version": 1, "run_id": data["run"]["id"], "issue_id": data["run"]["issue_id"],
            "captured_at": data["captured_at"], "last_message_seq": messages[-1]["seq"] if messages else None,
            "statistics": statistics(data), "code_evidence": code_evidence(messages)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        summary = summarize(snapshot(args.issue, os.environ))
        write_new(args.output, summary)
    except Exception as error:
        print(json.dumps({"error": str(error) if isinstance(error, RunContextError) else "运行资料整理失败，请检查 CLI 返回和输出路径"}, ensure_ascii=False))
        return 1
    print(json.dumps({"output": args.output, "statistics": summary["statistics"],
                      "evidence_count": len(summary["code_evidence"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
