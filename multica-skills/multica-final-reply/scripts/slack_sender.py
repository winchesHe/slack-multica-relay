#!/usr/bin/env python3
"""通过 Slack Skill 一次发送最终消息，并持久化运行回执。"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class ReplyError(Exception):
    pass


def save(path, value):
    # 发送前先落盘意图；崩溃或响应丢失后停止重发，交由人工核对。
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


def execute(args, env):
    required = ["MULTICA_TASK_ID", "MULTICA_WORKSPACE_ID", "SLACK_TEAM_ID", "RELAY_SLACK_CLI",
                "SLACK_REPLY_ACTOR"]
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
    cli = Path(env["RELAY_SLACK_CLI"]).expanduser()
    if not cli.is_absolute() or not cli.is_file():
        raise ReplyError("Slack Skill 入口不存在，未发送")
    text = Path(args.text_file).read_text(encoding="utf-8")
    blocks = Path(args.blocks_file).read_text(encoding="utf-8") if args.blocks_file else None
    intent = {"issue": args.issue, "channel": args.channel, "thread": args.thread_ts, "team": env["SLACK_TEAM_ID"],
              "text": text, "blocks": blocks, "format": args.format, "actor": actor}
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
            if state.get("phase") != "sent":
                raise ReplyError("上次发送结果不明，必须人工核对，禁止自动重发")
            return {"action": "duplicate", **state["ref"]}
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
        return {"action": "sent", **state["ref"]}
