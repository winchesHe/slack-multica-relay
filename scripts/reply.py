#!/usr/bin/env python3
"""复用 Slack Skill 发送最终回复，并自动登记运行与消息；重复执行只补登记。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


class ReplyError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReplyError("登记地址发生重定向，已停止")


def save(path, value):
    # 先落盘发送意图；发送成功后的登记失败不能导致下一次重复发送。
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def invoke(cli, args):
    result = subprocess.run(["rtk", "proxy", sys.executable, str(cli), *args],
                            text=True, capture_output=True)
    try:
        body = json.loads(result.stdout)
    except ValueError:
        raise ReplyError("Slack 返回无法解析；若已开始发送，禁止自动重发")
    if not isinstance(body, dict) or result.returncode != 0:
        raise ReplyError("Slack 调用未确认成功；请核对持久化回执，禁止自动重发")
    return body


def register(url, token, ref):
    request = urllib.request.Request(url, data=json.dumps(ref).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=50) as response:
        body = json.load(response)
    if body.get("action") != "registered" or body.get("messageTs") != ref["messageTs"]:
        raise ReplyError("登记尚未确认")


def execute(args, env, *, register_footer=True):
    required = ["MULTICA_TASK_ID", "MULTICA_WORKSPACE_ID", "SLACK_TEAM_ID", "RELAY_SLACK_CLI",
                "SLACK_REPLY_ACTOR"]
    if register_footer:
        required += ["RELAY_REPLY_REGISTER_URL", "RELAY_REPLY_TOKEN"]
    if any(not env.get(key, "").strip() for key in required):
        raise ReplyError("缺少 Runtime 回复配置，未发送")
    task = env["MULTICA_TASK_ID"]
    uuid = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
    if any(not re.fullmatch(uuid, value) for value in [task, args.issue, env["MULTICA_WORKSPACE_ID"]]):
        raise ReplyError("运行或工作区 ID 无效，未发送")
    if not re.fullmatch(r"[CGD][A-Z0-9]+", args.channel) or not re.fullmatch(r"\d+\.\d{6}", args.thread_ts):
        raise ReplyError("必须提供原频道 ID 和根 thread 时间戳，未发送")
    actor = env["SLACK_REPLY_ACTOR"]
    if actor not in ("user", "bot"):
        raise ReplyError("必须固定 User 或 Bot 身份，未发送")
    if register_footer:
        parsed = urllib.parse.urlparse(env["RELAY_REPLY_REGISTER_URL"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ReplyError("登记地址必须是固定 HTTPS 地址，未发送")
        if len(env["RELAY_REPLY_TOKEN"]) < 32:
            raise ReplyError("登记凭据无效，未发送")
    cli = Path(env["RELAY_SLACK_CLI"]).expanduser()
    if not cli.is_absolute() or not cli.is_file():
        raise ReplyError("Slack Skill 入口不存在，未发送")
    text = Path(args.text_file).read_text(encoding="utf-8")
    blocks = Path(args.blocks_file).read_text(encoding="utf-8") if args.blocks_file else None
    intent = {"issue": args.issue, "channel": args.channel, "thread": args.thread_ts, "team": env["SLACK_TEAM_ID"],
              "text": text, "blocks": blocks, "format": args.format, "actor": actor}
    if not register_footer:
        intent["delivery"] = "final-snapshot"
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    root = Path(env.get("RELAY_RECEIPT_DIR", "~/.local/state/slack-multica-relay")).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256((env["MULTICA_WORKSPACE_ID"] + ":" + task).encode()).hexdigest()
    path = root / (key + ".json")
    lock_fd = os.open(root / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(path.read_text()) if path.exists() else None
        if state:
            if state.get("fingerprint") != fingerprint:
                raise ReplyError("本次运行已有不同的最终回复记录，未发送")
            if state.get("phase") not in ("sent", "registered"):
                raise ReplyError("上次发送结果不明，必须人工核对，禁止自动重发")
            if state["phase"] == "registered" or not register_footer:
                return {"action": "duplicate", **state["ref"]}
        else:
            command = ["send", "--as", actor, "--channel", args.channel,
                       "--thread-ts", args.thread_ts, "--format", args.format,
                       "--text-file", args.text_file]
            if args.blocks_file:
                command += ["--blocks-file", args.blocks_file]
            preview = invoke(cli, [*command, "--dry-run"])
            preview_actor = preview.get("actor", {})
            if preview.get("status") != "preview" or not preview.get("preview_digest") or preview_actor.get("selected") != actor or preview_actor.get("team_id") != env["SLACK_TEAM_ID"]:
                raise ReplyError("Slack 预检身份或输出不符合契约，未发送")
            if args.dry_run:
                return {"action": "preview", "taskId": task, "preview_digest": preview["preview_digest"]}
            state = {"phase": "sending", "fingerprint": fingerprint}
            save(path, state)
            result = invoke(cli, [*command, "--confirm-preview", preview["preview_digest"]])
            message = result.get("message", {})
            if result.get("status") != "sent" or result.get("operation_status") != "succeeded" or result.get("actor", {}).get("selected") != actor:
                raise ReplyError("Slack 发送未确认，禁止重发")
            if message.get("channel_id") != args.channel or message.get("thread_ts") != args.thread_ts or not re.fullmatch(r"\d+\.\d{6}", str(message.get("ts", ""))):
                raise ReplyError("Slack 返回消息目标不匹配，禁止重发")
            state.update(phase="sent", ref={"version": 1, "issueId": args.issue, "taskId": task,
                "channelId": args.channel, "threadTs": args.thread_ts, "messageTs": message["ts"]})
            save(path, state)
        if not register_footer:
            return {"action": "sent", **state["ref"]}
        if args.dry_run:
            return {"action": "registration_pending", **state["ref"]}
        try:
            register(env["RELAY_REPLY_REGISTER_URL"], env["RELAY_REPLY_TOKEN"], state["ref"])
        except Exception:
            raise ReplyError("正文已发送，footer 登记失败；使用相同参数重试仅补登记，不重发正文")
        state["phase"] = "registered"
        save(path, state)
        return {"action": "registered", **state["ref"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread-ts", required=True)
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--blocks-file")
    parser.add_argument("--format", choices=["markdown"], default="markdown")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = execute(args, os.environ)
    except Exception as error:
        print(json.dumps({"error": str(error) if isinstance(error, ReplyError) else "回复处理失败，请核对回执后继续"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
