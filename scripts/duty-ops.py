#!/usr/bin/env python3
"""Closed Loop B：GRM 每日 Bug 值守 v3 Agent 驱动闭环。

本模块把本地状态限制为租约、fencing token、启动 intent 和恢复索引；
业务结论必须通过 Multica/Slack 回读后才写入 v3 终态评论。
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo


_ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("duty_queue", _ROOT / "duty-queue.py")
queue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(queue)

PROTOCOL_VERSION = 3
POLICY = "lightweight_agent_driven_v3"
FAILURE_POLICY = "retry_2_then_skip_v1"
LANE_COUNT = 2
RETRY_LIMIT = 2
IN_FLIGHT = {"queued", "dispatched", "running", "waiting_local_directory"}
TERMINAL_ISSUE = {"done", "cancelled", "canceled"}
TERMINAL_RECEIPT = {"delivered", "blocked"}
CONCLUSIONS = {"confirmed", "duplicate", "cs_followed", "excluded", "suspended_external", "skipped_failure"}
SOURCE_NAMES = ("canny", "facebook", "intercom")
MANIFEST_MARKER = "daily-round-manifest:v3"
INVESTIGATION_MARKER = "daily-investigation:v3"
FINISH_MARKER = "daily-scan-finish:v3"
DELIVERY_MARKER = "daily-delivery:v3"
BLOCKED_MARKER = "daily-blocked:v3"
BOARD_MARKER = "duty-board:v1"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
PR_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/[0-9]+$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class OpsError(ValueError):
    """可预期的闭环错误；CLI 只输出摘要，不打印来源内容或凭据。"""


class AmbiguousWrite(OpsError):
    """写入结果未知，必须先回读，不能盲重试。"""


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(value=None):
    value = value or utc_now()
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, value):
    """在同一目录内 fsync 后替换，避免进程中断留下半份 JSON。"""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ops-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def file_lock(path, timeout=60):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OpsError("值守锁等待超时，保留现场后重试")
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def validate_id(value, name="ID"):
    if not isinstance(value, str) or not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise OpsError(f"{name} 无效")
    if any(token in value for token in ("/", "\\", "..")):
        raise OpsError(f"{name} 无效")
    return value


def json_from_file(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OpsError("JSON 文件不可读") from error
    if not isinstance(value, dict):
        raise OpsError("JSON 根节点必须是对象")
    return value


def comment_json(comment, marker):
    if not isinstance(comment, dict):
        return None
    content = comment.get("content") or ""
    if marker == BOARD_MARKER:
        if not content.startswith("<!-- " + marker + " -->"):
            return None
    elif not content.startswith("<!-- " + marker + " "):
        return None
    match = re.search(r"```json\n(.*?)\n```", comment.get("content") or "", re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def comment_text(marker, fields, suffix=""):
    header = "<!-- duty-board:v1 -->" if marker == BOARD_MARKER else "<!-- " + marker + suffix + " -->"
    return (header + "\n"
            + ("结构化回执，以下事实已完成回读。\n\n" if marker != BOARD_MARKER else "每日值守台账记录。\n\n")
            + "```json\n" + json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n```")


def candidate_hash(ids):
    return queue.candidate_set_hash(ids)


def recursive_value(value, names):
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] not in (None, ""):
                return value[name]
        for child in value.values():
            found = recursive_value(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = recursive_value(child, names)
            if found is not None:
                return found
    return None


def attachment_id(value):
    """优先从附件数组取真实附件 ID，避免把 comment UUID 当成附件。"""
    if isinstance(value, dict):
        attachments = value.get("attachments")
        if isinstance(attachments, list):
            for item in attachments:
                if isinstance(item, dict):
                    for key in ("id", "attachmentId", "attachment_id"):
                        found = item.get(key)
                        if isinstance(found, str) and queue.valid_attachment_id(found):
                            return found
        for key in ("attachmentId", "attachment_id"):
            found = value.get(key)
            if isinstance(found, str) and queue.valid_attachment_id(found):
                return found
        if not any(key in value for key in ("attachments", "content", "author_id")):
            found = value.get("id")
            if isinstance(found, str) and queue.valid_attachment_id(found):
                return found
    return None


def permalink(value):
    """只读取顶层或 message 层的 permalink 字段，避免误抓 blocks/attachments 中的业务 URL。"""
    if not isinstance(value, dict):
        return None
    candidates = [value]
    if isinstance(value.get("message"), dict):
        candidates.append(value["message"])
    for container in candidates:
        for key in ("permalink", "messageUrl", "message_url"):
            found = container.get(key)
            if isinstance(found, str) and found.startswith(("http://", "https://")):
                return found
    return None


def workspace_base_from_url(url):
    if not isinstance(url, str) or "/archives/" not in url:
        return None
    idx = url.find("/archives/")
    base = url[:idx].rstrip("/")
    return base if base.startswith(("http://", "https://")) else None


def compose_slack_permalink(workspace_base, channel_id, ts, thread_ts=None):
    if not workspace_base or not channel_id or not ts:
        return None
    ts_str = str(ts).strip()
    if "." not in ts_str:
        return None
    frag = ts_str.replace(".", "")
    base = workspace_base.rstrip("/")
    url = f"{base}/archives/{channel_id}/p{frag}"
    if thread_ts and str(thread_ts).strip() != ts_str:
        url = f"{url}?thread_ts={str(thread_ts).strip()}&cid={channel_id}"
    return url


def run_id_set(runs):
    return {run.get("id") for run in runs if isinstance(run, dict) and isinstance(run.get("id"), str)}


def valid_manifest(value, scan_id=None, target_date=None):
    if not isinstance(value, dict) or value.get("protocolVersion") != 3:
        raise OpsError("manifest protocolVersion 必须是 3")
    round_id = value.get("roundId")
    feedback_date = value.get("feedbackDate")
    if scan_id and round_id != scan_id:
        raise OpsError("manifest roundId 与扫描任务不一致")
    if not DATE_RE.fullmatch(feedback_date or "") or (target_date and feedback_date != target_date):
        raise OpsError("manifest feedbackDate 无效或与目标日不一致")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or value.get("candidateCount") != len(candidates):
        raise OpsError("manifest candidateCount 不一致")
    ids = []
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("candidateId"), str) or not item["candidateId"].strip():
            raise OpsError("manifest candidateId 无效")
        if item.get("feedbackDate") != feedback_date or not isinstance(item.get("summary"), str) or not item["summary"].strip():
            raise OpsError("manifest 候选缺少摘要或反馈日")
        if not isinstance(item.get("sourceUrl"), str) or not item["sourceUrl"].strip():
            raise OpsError("manifest 候选缺少 sourceUrl")
        if item["candidateId"] in ids:
            raise OpsError("manifest candidateId 重复")
        ids.append(item["candidateId"])
    if value.get("candidateSetSha256") != candidate_hash(ids):
        raise OpsError("manifest candidateSetSha256 不匹配")
    return value


# 与只读计划器共享协议校验，避免 v3 写入和看板读取出现两套口径。
metadata = queue.meta
parse_time = queue.timestamp
valid_confirmation = queue.valid_confirmation
valid_external_block = queue.valid_external_block
valid_failure_skip = queue.valid_failure_skip
source_tokens = queue.source_tokens


def valid_investigation(value, entry, lane_id=None, *, agent_record=True):
    required = {"schemaVersion": 1, "protocolVersion": 3, "mode": "investigate",
                "roundId": entry["_roundId"], "sourceScanId": entry["_roundId"],
                "candidateId": entry["candidateId"], "feedbackDate": entry["feedbackDate"]}
    if any(value.get(key) != expected for key, expected in required.items()):
        return False
    if lane_id is not None and value.get("laneIssueId") != lane_id:
        return False
    if not isinstance(value.get("sourceUrl"), str) or value["sourceUrl"] != entry.get("sourceUrl"):
        return False
    if not queue.valid_source_anchor(value, entry):
        return False
    conclusion = value.get("conclusion")
    if conclusion not in CONCLUSIONS:
        return False
    if agent_record and (type(value.get("fencingToken")) is not int or value["fencingToken"] < 1):
        return False
    if conclusion == "skipped_failure":
        return valid_failure_skip(value)
    report = value.get("report")
    if not isinstance(report, dict) or not queue.valid_attachment_id(report.get("attachmentId")):
        return False
    if not isinstance(report.get("sha256"), str) or not report["sha256"] or report.get("readbackVerified") is not True:
        return False
    evidence = value.get("evidenceRevision")
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    if conclusion == "confirmed" and not valid_confirmation(value):
        return False
    if conclusion == "suspended_external" and not valid_external_block(value):
        return False
    if conclusion in {"duplicate", "excluded", "cs_followed"} and not queue.text(value.get("conclusionRef")):
        return False
    return True


class Native:
    """Multica CLI 适配器。测试可传 runner；真实命令只在此处拼接。"""
    def __init__(self, server_url, workspace_id, runner=subprocess.run, timeout=30):
        self.server_url = server_url
        self.workspace_id = workspace_id
        self.runner = runner
        self.timeout = timeout

    @property
    def base(self):
        return ["multica", "--server-url", self.server_url, "--workspace-id", self.workspace_id]

    def call(self, args, *, ambiguous=False):
        command = self.base + list(args)
        if "--output" not in command:
            command += ["--output", "json"]
        try:
            result = self.runner(command, check=True, capture_output=True, text=True, timeout=self.timeout)
            data = json.loads(result.stdout or "null")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            if ambiguous:
                raise AmbiguousWrite("Multica 写入结果未知，已保留 intent") from error
            raise OpsError("Multica 操作失败") from error
        return data

    def issue(self, issue_id):
        data = self.call(["issue", "get", issue_id])
        if not isinstance(data, dict):
            raise OpsError("任务读取响应无效")
        return data

    def list_project(self, project_id):
        rows, offset, total = [], 0, None
        while True:
            page = self.call(["issue", "list", "--project", project_id, "--limit", "100",
                              "--offset", str(offset), "--sort", "created_at", "--direction", "asc"])
            batch = page.get("issues") if isinstance(page, dict) else None
            if not isinstance(batch, list) or type(page.get("has_more")) is not bool or type(page.get("total")) is not int:
                raise OpsError("项目任务分页响应无效")
            total = page["total"] if total is None else total
            if page["total"] != total or page.get("offset") != offset:
                raise OpsError("项目任务分页在读取期间发生变化")
            rows.extend(batch)
            if not page["has_more"]:
                if len(rows) != total or len({row.get("id") for row in rows}) != total:
                    raise OpsError("项目任务分页总数不闭合")
                return rows
            if not batch:
                raise OpsError("项目任务分页未推进")
            offset += len(batch)

    def comments(self, issue_id):
        data = self.call(["issue", "comment", "list", issue_id, "--full", "--compact"])
        rows = data if isinstance(data, list) else data.get("comments") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise OpsError("评论读取响应无效")
        return rows

    def runs(self, issue_id):
        data = self.call(["issue", "runs", issue_id])
        if not isinstance(data, list):
            raise OpsError("运行读取响应无效")
        return data

    def timeline(self, issue_id):
        data = self.call(["issue", "timeline", issue_id])
        return data if isinstance(data, list) else []

    def metadata_set(self, issue_id, key, value):
        self.call(["issue", "metadata", "set", issue_id, "--key", key,
                   "--value", json.dumps(value, ensure_ascii=False, separators=(",", ":"))], ambiguous=True)
        if metadata(self.issue(issue_id), key) != value:
            raise OpsError("metadata 写后回读不一致")

    def _comment_trigger_parent_id(self, issue_id, error):
        cause = getattr(error, "__cause__", None)
        if not isinstance(cause, subprocess.CalledProcessError):
            return None
        detail = "\n".join(part for part in (cause.stderr, cause.stdout) if isinstance(part, str))
        if "comment-triggered tasks cannot create top-level comments" not in detail:
            return None
        uuid_any = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        match = re.search(rf"set parent_id \(--parent\) to ({uuid_any})", detail)
        if match:
            return match.group(1)
        try:
            runs = self.runs(issue_id)
        except OpsError:
            return None
        for run in reversed(runs):
            if not isinstance(run, dict) or run.get("status") != "running" or run.get("kind") != "comment":
                continue
            for key in ("trigger_comment_id", "comment_id", "triggerCommentId", "commentId"):
                cid = run.get(key)
                if isinstance(cid, str) and re.fullmatch(uuid_any, cid):
                    return cid
        return None

    def comment_add(self, issue_id, content_file, attachment_file=None, parent_id=None):
        args = ["issue", "comment", "add", issue_id, "--content-file", str(content_file),
                "--allow-external-file"]
        if attachment_file:
            args += ["--attachment", str(attachment_file)]
        if parent_id:
            args += ["--parent", str(parent_id)]
        try:
            return self.call(args, ambiguous=True)
        except OpsError as error:
            if parent_id:
                raise
            detected = self._comment_trigger_parent_id(issue_id, error)
            if not detected:
                raise
            return self.call([*args, "--parent", detected], ambiguous=True)

    def attachment_download(self, attachment, directory):
        # multica attachment download 不支持 --output json 且输出纯文本状态。
        command = self.base + ["attachment", "download", attachment, "-o", str(directory)]
        try:
            self.runner(command, check=True, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise OpsError("Multica 附件下载失败") from error
        return {"ok": True}

    def assign(self, issue_id, agent_id):
        return self.call(["issue", "assign", issue_id, "--to-id", agent_id, "--no-start"], ambiguous=True)

    def rerun(self, issue_id):
        return self.call(["issue", "rerun", issue_id], ambiguous=True)

    def status(self, issue_id, value):
        return self.call(["issue", "status", issue_id, value, "--no-start"], ambiguous=True)

    def create(self, args):
        return self.call(["issue", "create", *args], ambiguous=True)

    def update(self, issue_id, args):
        return self.call(["issue", "update", issue_id, *args], ambiguous=True)


class Slack:
    """slack.py CLI 适配器；输出文件由适配器临时保存，不污染 ops 状态目录。"""
    def __init__(self, cli, runner=subprocess.run, timeout=30):
        self.cli, self.runner, self.timeout = cli, runner, timeout

    def call(self, args, *, dry_run=False, ambiguous=True):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "slack.json"
            command = [sys.executable, self.cli, *args]
            if dry_run:
                command.append("--dry-run")
            command += ["--as", "bot", "--output", str(output)]
            try:
                self.runner(command, check=True, capture_output=True, text=True, timeout=self.timeout)
                data = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                exit_code = getattr(error, "returncode", "unknown")
                safe_name = Path(self.cli).name or "slack"
                message = f"Slack {safe_name} 失败（退出码 {exit_code}）"
                if ambiguous:
                    raise AmbiguousWrite(message + "，已保留 intent") from error
                raise OpsError(message) from error
            return data

    def send(self, channel, text_file, blocks_file=None, thread_ts=None, dry_run=False):
        args = ["send", "--channel", channel, "--text-file", str(text_file)]
        if blocks_file:
            args += ["--blocks-file", str(blocks_file)]
        if thread_ts:
            args += ["--thread-ts", thread_ts]
        preview = self.call(args, dry_run=True, ambiguous=False)
        if dry_run:
            return preview
        digest = recursive_value(preview, ("preview_digest", "previewDigest"))
        if not digest:
            raise OpsError("Slack dry-run 未返回 preview_digest")
        return self.call([*args, "--confirm-preview", str(digest)], dry_run=False, ambiguous=True)

    def upload(self, channel, file_path, digest, message, thread_ts=None):
        args = ["files_upload", "--channel", channel]
        if thread_ts:
            args += ["--thread-ts", thread_ts]
        args += ["--file", str(file_path), "--sha256", digest, "--message", message]
        return self.call(args)

    def edit(self, url, text_file, blocks_file=None, dry_run=False):
        args = ["edit", "--url", url, "--text-file", str(text_file)]
        if blocks_file:
            args += ["--blocks-file", str(blocks_file)]
        preview = self.call(args, dry_run=True, ambiguous=False)
        if dry_run:
            return preview
        digest = recursive_value(preview, ("preview_digest", "previewDigest"))
        if not digest:
            raise OpsError("Slack edit dry-run 未返回 preview_digest")
        return self.call([*args, "--confirm-preview", str(digest)], dry_run=False, ambiguous=True)

    def get(self, url):
        return self.call(["get", "--url", url], ambiguous=False)

    def replies(self, url):
        return self.call(["replies", "--url", url], ambiguous=False)

    def history(self, channel, limit=200, thread_ts=None):
        args = ["history", "--channel", channel, "--limit", str(limit)]
        if thread_ts:
            args += ["--include-thread-replies"]
        return self.call(args, ambiguous=False)

    def resolve_permalink(self, channel_id, ts, thread_ts=None, workspace_base=None):
        """slack.py history 返回 permalink=None 时，通过已知 workspace_base 或 chat.getPermalink 还原链接。"""
        composed = compose_slack_permalink(workspace_base, channel_id, ts, thread_ts)
        if composed:
            return composed
        cli_dir = Path(self.cli).resolve().parent if self.cli else None
        if not cli_dir or not (cli_dir / "sk").is_dir() or not channel_id or not ts:
            return None
        script = (
            "import json, sys\n"
            "from sk.actor import discover_contexts\n"
            "from sk.config import Config\n"
            "cfg = Config()\n"
            "ctxs = discover_contexts(cfg, ('bot',))\n"
            "ctx = ctxs.get('bot')\n"
            "if not ctx:\n"
            "    sys.exit(2)\n"
            "res = ctx.read_client().call('chat.getPermalink', channel=sys.argv[1], message_ts=sys.argv[2])\n"
            "print(json.dumps({'permalink': res.get('permalink'), 'workspace_url': ctx.identity.workspace_url}))\n"
        )
        try:
            proc = self.runner(
                [sys.executable, "-c", script, str(channel_id), str(ts)],
                check=True, capture_output=True, text=True, timeout=self.timeout,
                cwd=str(cli_dir),
            )
            payload = json.loads(proc.stdout or "{}")
            link = payload.get("permalink")
            if isinstance(link, str) and link.startswith(("http://", "https://")):
                return link
            return compose_slack_permalink(payload.get("workspace_url"), channel_id, ts, thread_ts)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            return None


class Ops:
    def __init__(self, native, slack=None, state_dir=None, now=None, agent_id=None, runtime_id=None, workspace_id=None):
        self.native, self.slack = native, slack
        self.agent_id, self.runtime_id = agent_id, runtime_id
        self.now = now or utc_now()
        if runtime_id is not None:
            validate_id(runtime_id, "runtimeId")
        if workspace_id is not None:
            validate_id(workspace_id, "workspaceId")
        scope = workspace_id or getattr(native, "workspace_id", None) or "default"
        validate_id(scope, "workspaceId")
        self.state_dir = Path(state_dir or (Path.home() / ".config/moego/daily-bug-duty/ops" / scope))
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)

    def _validate_id(self, value, name="ID"):
        validate_id(value, name)
        path = self._state_path_unchecked(value)
        try:
            inside = path.resolve().is_relative_to(self.state_dir.resolve())
        except AttributeError:
            inside = self.state_dir.resolve() in path.resolve().parents
        if not inside:
            raise OpsError(f"{name} 无效")
        return value

    def _state_path_unchecked(self, round_id):
        return self.state_dir / f"{round_id}.json"

    def _lock_for_round(self, round_id, *, issue_id=None):
        # 每轮独立短锁只保护本轮状态；dispatch 无 round 时使用 issue 专属持久锁。
        if round_id is None:
            if issue_id:
                self._validate_id(issue_id, "issueId")
                return self.state_dir / "locks" / f"dispatch-{issue_id}.lock"
            return self.state_dir / "locks" / "dispatch.lock"
        self._validate_id(round_id, "roundId")
        return self.state_dir / "locks" / f"{round_id}.lock"

    def _repair_lock(self):
        # 全局修复槽单独加锁，避免不同轮次的调查互相阻塞。
        return self.state_dir / "locks" / "global-repair-slot.lock"

    def _state_path(self, round_id):
        self._validate_id(round_id, "roundId")
        return self._state_path_unchecked(round_id)

    def state(self, round_id):
        path = self._state_path(round_id)
        if not path.exists():
            return {"schemaVersion": 1, "roundId": round_id, "leases": {}, "nextFencingToken": 0,
                    "failedRuns": {}, "intents": {}, "records": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OpsError("本地闭环状态损坏，停止自动恢复") from error
        if not isinstance(value, dict) or value.get("roundId") != round_id:
            raise OpsError("本地闭环状态作用域不一致")
        return value

    def save_state(self, round_id, value):
        atomic_write(self._state_path(round_id), value)

    @contextlib.contextmanager
    def _write_content(self, value, suffix=".json"):
        fd, name = tempfile.mkstemp(prefix="ops-content-", suffix=suffix)
        os.close(fd)
        path = Path(name)
        try:
            if isinstance(value, bytes):
                path.write_bytes(value)
            else:
                path.write_text(str(value), encoding="utf-8")
            yield path
        finally:
            path.unlink(missing_ok=True)

    def _fresh_comments(self, issue_id):
        return self.native.comments(issue_id)

    def manifest(self, scan_id, agent_id=None):
        agent_id = agent_id or self.agent_id
        values = []
        for comment in self._fresh_comments(scan_id):
            item = comment_json(comment, MANIFEST_MARKER)
            if item is None:
                continue
            if comment.get("author_type") == "agent" and (not agent_id or comment.get("author_id") == agent_id):
                values.append(item)
        if not values:
            return None
        first = valid_manifest(values[0], scan_id)
        if (not queue.valid_attachment_id(first.get("attachmentId"))
                or not isinstance(first.get("sha256"), str)
                or first.get("readbackHashVerified") is not True):
            raise OpsError("manifest 附件回执不完整")
        signature = json.dumps(first.get("candidates"), sort_keys=True, ensure_ascii=False)
        for item in values[1:]:
            valid_manifest(item, scan_id)
            if (not queue.valid_attachment_id(item.get("attachmentId"))
                    or not isinstance(item.get("sha256"), str)
                    or item.get("readbackHashVerified") is not True):
                raise OpsError("manifest 附件回执不完整")
            if (item.get("candidateSetSha256"), json.dumps(item.get("candidates"), sort_keys=True, ensure_ascii=False)) != (first.get("candidateSetSha256"), signature):
                raise OpsError("同一扫描存在互相冲突的 manifest")
        return values[-1]

    def _readback_attachment(self, result, expected_hash):
        aid = attachment_id(result)
        if not aid:
            raise OpsError("上传未返回真实 attachmentId")
        with tempfile.TemporaryDirectory() as directory:
            self.native.attachment_download(aid, directory)
            candidates = [p for p in Path(directory).rglob("*") if p.is_file()]
            if not candidates:
                raise OpsError("附件下载回读为空")
            if not any(sha256_file(path) == expected_hash for path in candidates):
                raise OpsError("附件 SHA-256 回读不一致")
        return aid

    def _post_and_verify(self, issue_id, marker, payload, *, suffix="", attachment=None):
        with self._write_content(comment_text(marker, payload, suffix)) as content:
            result = self.native.comment_add(issue_id, content, attachment)
            comments = self._fresh_comments(issue_id)
            found = [comment_json(comment, marker) for comment in comments]
            if payload not in found:
                raise OpsError(f"{marker} 评论写后回读不一致")
            return recursive_value(result, ("commentId", "comment_id")) or self._comment_id(comments, marker, payload)

    @staticmethod
    def _comment_id(comments, marker, payload):
        for comment in reversed(comments):
            if comment_json(comment, marker) == payload:
                return comment.get("id") or "comment-readback"
        return "comment-readback"

    def _ensure_scan_metadata(self, scan_id, target_date, scheduler_revision, runtime_id, sources=None, collected=None):
        scan = self.native.issue(scan_id)
        legacy_keys = ("dailyRound", "dailyBatch", "dailyDispatch")
        if any(metadata(scan, key) for key in legacy_keys):
            raise OpsError("已有轮次不是可迁移的 v3 现场")
        execution = metadata(scan, "dailyExecution")
        expected = {"schemaVersion": 1, "protocolVersion": 3, "policy": POLICY, "laneCount": LANE_COUNT,
                    "repairConcurrency": 1, "investigationFailurePolicy": FAILURE_POLICY,
                    "schedulerRevision": scheduler_revision, "runtimeId": runtime_id}
        if execution and execution != expected:
            if execution.get("protocolVersion") != 3 or any(key in execution for key in ("dailyRound", "dailyBatch", "dailyDispatch")):
                raise OpsError("已有轮次不是可迁移的 v3 现场")
            raise OpsError("dailyExecution 已存在且与本次 Runtime/版本冲突")
        if execution != expected:
            self.native.metadata_set(scan_id, "dailyExecution", expected)
        scan_data = metadata(scan, "dailyScan")
        if scan_data and scan_data.get("targetDate") != target_date:
            raise OpsError("dailyScan.targetDate 已冻结，不能覆盖")
        daily_scan = dict(scan_data)
        daily_scan.update({"targetDate": target_date, "coverageComplete": bool(scan_data.get("coverageComplete", False)),
                           "sources": scan_data.get("sources") or (sources or {})})
        if scan_data.get("collected") is not None:
            daily_scan["collected"] = scan_data["collected"]
        elif collected is not None:
            daily_scan["collected"] = collected
        else:
            daily_scan.pop("collected", None)
        if daily_scan != scan_data:
            self.native.metadata_set(scan_id, "dailyScan", daily_scan)
        return expected, daily_scan

    def _ensure_lanes(self, scan, project_id, target_date):
        issues = self.native.list_project(project_id) if hasattr(self.native, "list_project") else []
        lanes = [row for row in issues if (
            metadata(row, "dailyLane").get("roundId") == scan["id"]
            or (row.get("parent_issue_id") == scan["id"]
                and "mode=investigate_lane" in (row.get("description") or "")
                and re.search(rf"工位\s+[1-{LANE_COUNT}]/{LANE_COUNT}", row.get("title") or "")))]

        lanes.sort(key=lambda row: metadata(row, "dailyLane").get("lane", 0))
        if len(lanes) > LANE_COUNT:
            raise OpsError(f"v3 调查工位超过 {LANE_COUNT} 张")
        for row in lanes:
            lane_data = metadata(row, "dailyLane")
            if any(key in lane_data for key in ("state", "currentCandidateId", "failureHandoffCandidateId")):
                raise OpsError("v3 调查工位包含 legacy 状态字段")
        def lane_number(row):
            value = metadata(row, "dailyLane").get("lane")
            if value in range(1, LANE_COUNT + 1):
                return value
            match = re.search(rf"工位\s+([1-{LANE_COUNT}])/{LANE_COUNT}", row.get("title") or "")
            return int(match.group(1)) if match else None
        for number in range(1, LANE_COUNT + 1):
            existing_lane = next((row for row in lanes if lane_number(row) == number), None)
            expected_lane = {"schemaVersion": 1, "protocolVersion": 3,
                             "mode": "investigate_lane", "roundId": scan["id"], "lane": number, "laneCount": LANE_COUNT}
            if existing_lane:
                current_lane = metadata(existing_lane, "dailyLane")
                if any(current_lane.get(key) != val for key, val in expected_lane.items()):
                    merged_lane = {**current_lane, **expected_lane}
                    self.native.metadata_set(existing_lane["id"], "dailyLane", merged_lane)
                    existing_lane.setdefault("metadata", {})["dailyLane"] = merged_lane
                continue
            if not hasattr(self.native, "create"):
                raise OpsError("无法创建调查工位")
            args = ["--project", project_id, "--title", f"反馈调查｜{target_date[5:]} 反馈｜工位 {number}/{LANE_COUNT}",
                    "--description", f"mode=investigate_lane\nroundId={scan['id']}\n", "--parent", scan["id"]]
            result = self.native.create(args)
            lane_id = recursive_value(result, ("id", "issueId", "issue_id"))
            if not lane_id:
                raise OpsError("创建调查工位未返回 issue id")
            lane = self.native.issue(lane_id)
            self.native.metadata_set(lane_id, "dailyLane", expected_lane)
            lane.setdefault("metadata", {})["dailyLane"] = expected_lane
            lanes.append(lane)
        return lanes

    def init_scan_manifest(self, scan_id, manifest_file, *, target_date=None, scheduler_revision=None,
                           runtime_id=None, project_id=None, agent_id=None, sources=None, collected=None):
        raw = json_from_file(manifest_file)
        target_date = target_date or raw.get("feedbackDate")
        valid_manifest(raw, scan_id, target_date)
        runtime_id = runtime_id or self.runtime_id
        if not runtime_id or not UUID_RE.fullmatch(runtime_id):
            raise OpsError("runtimeId 必须是 UUID")
        if not scheduler_revision or not re.fullmatch(r"[0-9a-fA-F]{40}", scheduler_revision):
            raise OpsError("schedulerRevision 必须是 40 位 SHA")
        with file_lock(self._lock_for_round(scan_id)):
            existing = self.manifest(scan_id, agent_id)
            current_hash = sha256_file(manifest_file)
            if existing:
                compare_existing = {key: value for key, value in existing.items()
                                    if key not in {"attachmentId", "sha256", "readbackHashVerified"}}
                compare_raw = {key: value for key, value in raw.items()
                               if key not in {"attachmentId", "sha256", "readbackHashVerified"}}
                if compare_existing != compare_raw or existing.get("sha256") != current_hash:
                    raise OpsError("已登记的 manifest 与输入冲突")
                if existing.get("readbackHashVerified") is not True:
                    raise OpsError("已登记的 manifest 未通过附件回读")
                self._ensure_scan_metadata(scan_id, target_date, scheduler_revision, runtime_id, sources, collected)
                if project_id:
                    self._ensure_lanes(self.native.issue(scan_id), project_id, target_date)
                return {"status": "reused", "manifest": existing}
            self._ensure_scan_metadata(scan_id, target_date, scheduler_revision, runtime_id, sources, collected)
            if project_id:
                self._ensure_lanes(self.native.issue(scan_id), project_id, target_date)
            state = self.state(scan_id)
            intent = state.setdefault("intent", {"kind": "manifest", "sha256": current_hash, "attempted": True})
            if intent.get("sha256") != current_hash:
                raise OpsError("manifest intent 与当前文件冲突")
            aid = intent.get("attachmentId")
            if aid:
                self._readback_attachment({"attachmentId": aid}, current_hash)
            else:
                self.save_state(scan_id, state)
                with self._write_content("准备登记清单") as content:
                    result = self.native.comment_add(scan_id, content, manifest_file)
                aid = self._readback_attachment(result, current_hash)
                intent["attachmentId"] = aid
                intent["readbackHashVerified"] = True
                self.save_state(scan_id, state)
            raw = {**raw, "attachmentId": aid, "sha256": current_hash, "readbackHashVerified": True}
            with self._write_content(comment_text(MANIFEST_MARKER, raw, f" round={scan_id}")) as content:
                self.native.comment_add(scan_id, content)
            if not any(comment_json(c, MANIFEST_MARKER) == raw for c in self._fresh_comments(scan_id)):
                raise OpsError("manifest 评论写后回读失败")
            state["manifest"] = raw
            state.pop("intent", None)
            self.save_state(scan_id, state)
            return {"status": "created", "manifest": raw}

    def update_scan_status(self, scan_id, *, target_date=None, source=None, source_url=None, source_status=None,
                           collected=None, counts=None, coverage_complete=None):
        """阶段 1 采集事实统一走写后回读，避免其它 CLI 绕过 v3 状态边界。"""
        with file_lock(self._lock_for_round(scan_id)):
            scan = self.native.issue(scan_id)
            data = metadata(scan, "dailyScan")
            if target_date is not None:
                if not DATE_RE.fullmatch(target_date):
                    raise OpsError("targetDate 必须是 YYYY-MM-DD")
                if data.get("targetDate") and data.get("targetDate") != target_date:
                    raise OpsError("dailyScan.targetDate 已冻结，不能覆盖")
                data["targetDate"] = target_date
            if source:
                if source not in SOURCE_NAMES:
                    raise OpsError("source 无效")
                sources = dict(data.get("sources") or {})
                source_data = dict(sources.get(source) or {})
                if source_url:
                    source_data["sourceUrl"] = source_url
                if source_status is not None:
                    if source_status not in {"success", "failed", "error"}:
                        raise OpsError("sourceStatus 无效")
                    source_data["status"] = source_status
                sources[source] = source_data
                data["sources"] = sources
            elif source_status is not None or source_url is not None:
                raise OpsError("设置 source-url 或 source-status 时必须指定 --source")
            if collected is not None:
                if not isinstance(collected, dict) or any(type(value) is not int or value < 0 for value in collected.values()):
                    raise OpsError("collected 必须是非负整数对象")
                # 按来源分次更新时保留此前已记录的来源计数，避免后写入覆盖先写入。
                data["collected"] = {**(data.get("collected") or {}), **collected}
            if counts is not None:
                if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
                    raise OpsError("counts 必须是非负整数对象")
                data["counts"] = {**(data.get("counts") or {}), **counts}
            if coverage_complete is not None:
                data["coverageComplete"] = bool(coverage_complete)
            self.native.metadata_set(scan_id, "dailyScan", data)
            saved = metadata(self.native.issue(scan_id), "dailyScan")
            if saved != data:
                raise OpsError("dailyScan 写后回读不一致")
            return saved

    def _legacy_scan(self, scan):
        # legacy 现场只读隔离，防止 v3 恢复误写旧 dailyRound/dailyBatch/dailyDispatch。
        execution = metadata(scan, "dailyExecution")
        return (any(metadata(scan, key) for key in ("dailyRound", "dailyBatch", "dailyDispatch"))
                or execution.get("protocolVersion") != 3 or execution.get("policy") != POLICY)

    def _round_issues(self, round_id, project_id=None):
        if hasattr(self.native, "list_project"):
            if not project_id:
                project_id = self.native.issue(round_id).get("project_id")
            if project_id:
                return [row for row in self.native.list_project(project_id)
                        if row.get("id") == round_id or row.get("parent_issue_id") == round_id
                        or metadata(row, "dailyLane").get("roundId") == round_id
                        or metadata(row, "dailyBug").get("sourceScanId") == round_id]
        return [self.native.issue(round_id)]

    def _records(self, scan_id, manifest, lanes, agent_id):
        entries = {item["candidateId"]: {**item, "_roundId": scan_id} for item in manifest["candidates"]}
        by_candidate = {}
        utc_min = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        for issue in lanes + [self.native.issue(scan_id)]:
            comments = issue.get("_comments") if "_comments" in issue else self._fresh_comments(issue["id"])
            for index, comment in enumerate(comments):
                item = comment_json(comment, INVESTIGATION_MARKER)
                if item is None:
                    continue
                is_agent = comment.get("author_type") == "agent" and (not agent_id or comment.get("author_id") == agent_id)
                is_override = comment.get("author_type") == "member" and item.get("manualOverride") is True
                if not (is_agent or is_override):
                    continue
                entry = entries.get(item.get("candidateId"))
                if not entry or not valid_investigation(item, entry, agent_record=is_agent):
                    continue
                try:
                    created_ts = parse_time(comment.get("created_at"))
                except (OpsError, queue.QueueError):
                    created_ts = utc_min
                group = by_candidate.setdefault(item["candidateId"], {"manual": [], "agent": []})
                group["manual" if is_override else "agent"].append((created_ts, index, item))
        selected = {}
        for cid, group in by_candidate.items():
            if group["manual"]:
                manual = max(group["manual"], key=lambda pair: (pair[0], pair[1]))
                latest_agent = max(group["agent"], key=lambda pair: (pair[0], pair[1]), default=None)
                if latest_agent is None or manual[0] >= latest_agent[0]:
                    selected[cid] = manual[2]
                    continue
            if group["agent"]:
                selected[cid] = max(group["agent"], key=lambda pair: (pair[2].get("fencingToken"), pair[0], pair[1]))[2]
        attachments, intercom = set(), set()
        for item in selected.values():
            if item.get("conclusion") == "skipped_failure":
                continue
            aid = item["report"]["attachmentId"]
            if aid in attachments:
                raise OpsError("调查报告附件被多个候选复用")
            attachments.add(aid)
            if item.get("conclusion") == "confirmed":
                tokens = {token for source in (item.get("sourceUrl"), item.get("evidenceRevision"))
                          for token in source_tokens(source) if token.startswith("intercom:")}

                if tokens & intercom:
                    raise OpsError("confirmed 候选复用了 Intercom 会话证据")
                intercom |= tokens
        return entries, selected

    def _lease_expired(self, lease):
        return parse_time(lease["expiresAt"]) <= self.now

    def _skip_candidate(self, scan_id, lane_id, entry, state, run_ids, reason):
        state["nextFencingToken"] = int(state.get("nextFencingToken", 0)) + 1
        token = state["nextFencingToken"]
        skipped = {"schemaVersion": 1, "protocolVersion": 3, "mode": "investigate", "roundId": scan_id,
                   "sourceScanId": scan_id, "candidateId": entry["candidateId"], "feedbackDate": entry["feedbackDate"],
                   "sourceUrl": entry.get("sourceUrl"), "laneIssueId": lane_id, "fencingToken": token,
                   "conclusion": "skipped_failure", "nextAction": "依赖恢复后在原候选继续调查",
                   "failureSkip": {"retryLimit": RETRY_LIMIT, "runIds": run_ids[-3:], "reason": reason,
                                   "resumeCondition": "依赖恢复后由 Agent 调用 recover", "skippedAt": iso(self.now)}}
        self._post_and_verify(lane_id, INVESTIGATION_MARKER, skipped,
                              suffix=f" round={scan_id} candidate={entry['candidateId']}")
        state["records"][entry["candidateId"]] = skipped
        return skipped

    def claim(self, scan_id, lane_id, *, worker_id=None, ttl_seconds=3600, agent_id=None):
        agent_id = agent_id or self.agent_id or self.native.issue(scan_id).get("assignee_id")
        worker_id = worker_id or agent_id or "worker"
        if ttl_seconds <= 0:
            raise OpsError("租约 TTL 必须为正数")
        with file_lock(self._lock_for_round(scan_id)):
            manifest = self.manifest(scan_id, agent_id)
            if not manifest:
                raise OpsError("尚未登记 manifest")
            lanes = [row for row in self._round_issues(scan_id) if metadata(row, "dailyLane").get("roundId") == scan_id]
            lane = next((row for row in lanes if row["id"] == lane_id), None)
            if not lane:
                raise OpsError("lane 不属于该扫描")
            state = self.state(scan_id)
            entries, records = self._records(scan_id, manifest, lanes, agent_id)
            for cid, lease in list(state.setdefault("leases", {}).items()):
                lane_runs = self.native.runs(lease.get("laneIssueId", lane_id))
                tracked_ids = set(lease.get("runIds", []))
                tracked = [run for run in lane_runs if run.get("id") in tracked_ids]
                exited_without_conclusion = bool(tracked) and all(run.get("status") not in IN_FLIGHT for run in tracked)
                if self._lease_expired(lease) or exited_without_conclusion:
                    failed = state.setdefault("failedRuns", {}).setdefault(cid, [])
                    observed = [run.get("id") for run in tracked if run.get("id")]
                    observed = observed or [run_id for run_id in lease.get("runIds", []) if run_id]
                    observed = observed or [lease.get("attemptId")]
                    for run_id in observed:
                        if run_id and run_id not in failed:
                            failed.append(run_id)
                    state["leases"].pop(cid, None)
                    if len(failed) >= RETRY_LIMIT + 1 and cid not in records:
                        skipped = self._skip_candidate(scan_id, lease.get("laneIssueId", lane_id), entries[cid], state, failed,
                                                        "调查运行结束或租约过期，三次运行均未形成结论")
                        records[cid] = skipped
            if any(lease.get("laneIssueId") == lane_id and not self._lease_expired(lease)
                   for lease in state["leases"].values()):
                raise OpsError("同一 lane 已持有未过期租约")
            occupied = {lease.get("candidateId"): lease for lease in state["leases"].values()
                        if not self._lease_expired(lease)}
            for cid, item in records.items():
                if item.get("conclusion") in CONCLUSIONS:
                    occupied[cid] = {"conclusion": True}
            available = next((entry for entry in manifest["candidates"] if entry["candidateId"] not in occupied), None)
            if available is None:
                self.save_state(scan_id, state)
                if len(records) == len(manifest["candidates"]):
                    return {"status": "all_concluded", "roundId": scan_id}
                return {"status": "no_available_candidate", "roundId": scan_id}
            # fencing token 单调递增，旧 lease 即使迟到也不能覆盖新 worker。
            token = int(state.get("nextFencingToken", 0)) + 1
            state["nextFencingToken"] = token
            run_history = self.native.runs(lane_id)
            active = sorted((run for run in run_history if run.get("status") in IN_FLIGHT),
                            key=lambda run: run.get("status") != "running")
            lease = {"candidateId": available["candidateId"], "laneIssueId": lane_id, "workerId": worker_id,
                     "fencingToken": token, "issuedAt": iso(self.now),
                     "expiresAt": iso(self.now + dt.timedelta(seconds=ttl_seconds)),
                     "runIds": [run.get("id") for run in active if run.get("id")],
                     "attemptId": f"lease-{token}"}
            state["leases"][available["candidateId"]] = lease
            self.save_state(scan_id, state)
            return {"status": "claimed", "roundId": scan_id, "laneIssueId": lane_id,
                    "candidateId": available["candidateId"], "fencingToken": token, "expiresAt": lease["expiresAt"],
                    "feedbackDate": available["feedbackDate"], "sourceUrl": available.get("sourceUrl")}

    def submit_investigation(self, scan_id, lane_id, candidate_id, fencing_token, record_file=None, report_file=None,
                             *, agent_id=None):
        agent_id = agent_id or self.agent_id or self.native.issue(scan_id).get("assignee_id")
        record = json_from_file(record_file) if record_file else {}
        manifest = self.manifest(scan_id, agent_id)
        if not manifest:
            raise OpsError("manifest 不存在")
        entry = next((item for item in manifest["candidates"] if item.get("candidateId") == candidate_id), None)
        if not entry:
            raise OpsError("candidateId 不在 manifest")
        conclusion = record.get("conclusion")
        if conclusion not in CONCLUSIONS:
            raise OpsError("调查 conclusion 无效")
        if conclusion != "skipped_failure" and (not isinstance(record.get("evidenceRevision"), str)
                                                   or not record["evidenceRevision"].strip()):
            raise OpsError("调查报告上传前缺少 evidenceRevision")
        if conclusion == "confirmed" and not valid_confirmation(record):
            raise OpsError("调查报告上传前 confirmed 证据不完整")
        if conclusion in {"duplicate", "excluded", "cs_followed"} and not queue.text(record.get("conclusionRef")):
            raise OpsError("调查报告上传前缺少 conclusionRef")
        if conclusion == "suspended_external" and not valid_external_block(record):
            raise OpsError("调查报告上传前 external blocker 不完整")
        report_hash = None
        if record.get("conclusion") != "skipped_failure":
            if not report_file:
                report_file = record.get("reportFile") or report_file
            if not report_file or not Path(report_file).is_file():
                raise OpsError("非 skipped_failure 必须提供 report-file")
            report_hash = sha256_file(report_file)
            with file_lock(self._lock_for_round(scan_id)):
                state = self.state(scan_id)
                lease = state.get("leases", {}).get(candidate_id)
                if not lease or lease.get("laneIssueId") != lane_id or lease.get("fencingToken") != fencing_token or self._lease_expired(lease):
                    raise OpsError("fencingToken 无效、租约已过期或已被更高 token 替换")
                with self._write_content("调查报告附件") as content:
                    uploaded = self.native.comment_add(lane_id, content, report_file)
                aid = self._readback_attachment(uploaded, report_hash)
                record["report"] = {"attachmentId": aid, "sha256": report_hash, "readbackVerified": True}
                record.update(schemaVersion=1, protocolVersion=3, mode="investigate", roundId=scan_id,
                              sourceScanId=scan_id, candidateId=candidate_id, feedbackDate=entry["feedbackDate"],
                              sourceUrl=entry.get("sourceUrl"), laneIssueId=lane_id, fencingToken=fencing_token)
                if not valid_investigation(record, {**entry, "_roundId": scan_id}, lane_id):
                    raise OpsError("调查结论不符合 v3 证据门槛")
                self._post_and_verify(lane_id, INVESTIGATION_MARKER, record,
                                      suffix=f" round={scan_id} candidate={candidate_id}")
                state["records"][candidate_id] = record
                state["leases"].pop(candidate_id, None)
                self.save_state(scan_id, state)
                return record
        with file_lock(self._lock_for_round(scan_id)):
            state = self.state(scan_id)
            lease = state.get("leases", {}).get(candidate_id)
            if not lease or lease.get("laneIssueId") != lane_id or lease.get("fencingToken") != fencing_token or self._lease_expired(lease):
                raise OpsError("fencingToken 无效、租约已过期或已被更高 token 替换")
            if not record.get("nextAction"):
                record["nextAction"] = (record.get("failureSkip") or {}).get("resumeCondition")
            record.update(schemaVersion=1, protocolVersion=3, mode="investigate", roundId=scan_id,
                          sourceScanId=scan_id, candidateId=candidate_id, feedbackDate=entry["feedbackDate"],
                          sourceUrl=entry.get("sourceUrl"), laneIssueId=lane_id, fencingToken=fencing_token,
                          conclusion="skipped_failure")
            if not valid_investigation(record, {**entry, "_roundId": scan_id}, lane_id):
                raise OpsError("skipped_failure 记录不符合 v3 失败跳过门槛")
            self._post_and_verify(lane_id, INVESTIGATION_MARKER, record,
                                  suffix=f" round={scan_id} candidate={candidate_id}")
            state["records"][candidate_id] = record
            state["leases"].pop(candidate_id, None)
            self.save_state(scan_id, state)
            return record

    def _agent_closed_done(self, issue_id, agent_id):
        events = self.native.timeline(issue_id)
        rows = [event for event in events if event.get("action") == "status_changed"]
        if not rows:
            return False
        latest = sorted(rows, key=lambda row: row.get("created_at") or "")[-1]
        return latest.get("details", {}).get("to") == "done" and latest.get("actor_type") == "agent" and latest.get("actor_id") == agent_id

    def _clear_launch_intents(self, round_id, issue_id, *keys):
        self._validate_id(issue_id, "issueId")
        intent_path = None if round_id else self.state_dir / f"dispatch-{issue_id}.json"
        if intent_path:
            if not intent_path.exists():
                return
            state = json_from_file(intent_path)
        else:
            if not self._state_path(round_id).exists():
                return
            state = self.state(round_id)
        intents = state.get("intents")
        if not isinstance(intents, dict):
            return
        changed = False
        for key in keys:
            if key in intents:
                intents.pop(key, None)
                changed = True
        if changed:
            self._save_intent_state(round_id, intent_path, state)

    def _launch_if_idle_unlocked(self, issue_id, agent_id, *, allow_successor=False, round_id=None):
        issue = self.native.issue(issue_id)
        agent_id = agent_id or self.agent_id or issue.get("assignee_id")
        if not agent_id:
            raise OpsError("缺少 agent-id 且任务没有 assignee_id")
        runs = self.native.runs(issue_id)
        active = [run for run in runs if run.get("status") in IN_FLIGHT]
        if any(run.get("agent_id") not in (None, agent_id) for run in active):
            raise OpsError("任务存在其它 Agent 的活动运行")
        queued = [run for run in active if run.get("status") in {"queued", "dispatched", "waiting_local_directory"}]
        if queued:
            keys = [f"dispatch:{issue_id}"] + ([f"parent-rerun:{round_id}"] if round_id == issue_id else [])
            self._clear_launch_intents(round_id, issue_id, *keys)
            return {"status": "reused", "runId": queued[0].get("id"), "issueId": issue_id}
        running = [run for run in active if run.get("status") == "running"]
        if running and not allow_successor:
            self._clear_launch_intents(round_id, issue_id, f"dispatch:{issue_id}")
            return {"status": "active", "issueId": issue_id, "runId": running[0].get("id")}
        if issue.get("status") in {"cancelled", "canceled"}:
            raise OpsError("任务已被取消，不自动恢复")
        if issue.get("status") == "done" and self._agent_closed_done(issue_id, agent_id):
            self.native.status(issue_id, "in_progress")
            issue = self.native.issue(issue_id)
        elif issue.get("status") == "done":
            raise OpsError("真人关闭的 done 任务不自动重开")
        prepare = None
        if issue.get("assignee_id") in (None, agent_id):
            if issue.get("assignee_id") is None:
                def prepare_assignment():
                    self.native.assign(issue_id, agent_id)
                    if self.native.issue(issue_id).get("assignee_id") != agent_id:
                        raise OpsError("任务指派写后回读不一致")
                prepare = prepare_assignment
        else:
            raise OpsError("任务已转交其它执行者")
        before = run_id_set(runs)
        key = f"dispatch:{issue_id}"
        return self._launch_with_intent(issue_id, agent_id, before, key, allow_successor=allow_successor,
                                        round_id=round_id, prepare=prepare)

    def _launch_with_intent(self, issue_id, agent_id, before, key, *, allow_successor=False,
                            round_id=None, prepare=None):
        """用持久 intent 对 assign/rerun 的未知结果做差集核对，允许终态 intent 清理后重新派发。"""
        self._validate_id(issue_id, "issueId")
        intent_path = None if round_id else self.state_dir / f"dispatch-{issue_id}.json"
        state = self.state(round_id) if round_id else (
            json_from_file(intent_path) if intent_path.exists() else {"intents": {}})
        intents = state.setdefault("intents", {})
        existing = intents.get(key)
        if existing:
            current = self.native.runs(issue_id)
            found = [run for run in current if run.get("id") not in existing.get("beforeRunIds", [])]
            if len(found) > 1:
                in_flight_found = [run for run in found if run.get("status") in IN_FLIGHT]
                if len(in_flight_found) > 1:
                    raise OpsError("intent 对应多个新运行")
                found = in_flight_found
            if len(found) == 1:
                run = found[0]
                run_id = run.get("id")
                intents.pop(key, None)
                self._save_intent_state(round_id, intent_path, state)
                if allow_successor:
                    # 后继请求只接受尚未开始且不在本次 before 快照中的新排队运行，避免把当前 running 当成后继而漏唤醒。
                    if run.get("status") in {"queued", "dispatched", "waiting_local_directory"} and run_id not in before:
                        return {"status": "reconciled", "runId": run_id, "issueId": issue_id}
                elif run.get("status") in IN_FLIGHT:
                    return {"status": "reconciled", "runId": run_id, "issueId": issue_id}
                # 原 run 已完成或属于当前正在运行的实例，继续走下方新 intent。
            else:
                if prepare and existing.get("prepared") is not True:
                    prepare()
                    existing["prepared"] = True
                existing["beforeRunIds"] = sorted(before)
                self._save_intent_state(round_id, intent_path, state)
        intent = {"beforeRunIds": sorted(before), "createdAt": iso(self.now), "allowSuccessor": allow_successor}
        intents[key] = intent
        self._save_intent_state(round_id, intent_path, state)
        if prepare:
            prepare()
            intent["prepared"] = True
            self._save_intent_state(round_id, intent_path, state)
        try:
            self.native.rerun(issue_id)
        except AmbiguousWrite:
            pass
        after = self.native.runs(issue_id)
        found = [run for run in after if run.get("id") not in intent["beforeRunIds"]]
        if len(found) != 1:
            raise AmbiguousWrite("rerun 结果不明，保留 beforeRunIds")
        run_id = found[0].get("id")
        intents.pop(key, None)
        self._save_intent_state(round_id, intent_path, state)
        return {"status": "started", "runId": run_id, "issueId": issue_id}

    def _save_intent_state(self, round_id, intent_path, state):
        if intent_path:
            atomic_write(intent_path, state)
        elif round_id:
            self.save_state(round_id, state)

    def dispatch_if_issue_idle(self, issue_id, *, agent_id=None, round_id=None):
        issue = self.native.issue(issue_id)
        agent_id = agent_id or self.agent_id or issue.get("assignee_id")
        if not agent_id:
            raise OpsError("缺少 agent-id 且任务没有 assignee_id")
        if round_id is None:
            round_id = (metadata(issue, "dailyBug").get("sourceScanId")
                        or metadata(issue, "dailyLane").get("roundId")
                        or (issue.get("id") if metadata(issue, "dailyExecution").get("protocolVersion") == 3 else None))
        with file_lock(self._lock_for_round(round_id, issue_id=issue_id)):
            result = self._launch_if_idle_unlocked(issue_id, agent_id, round_id=round_id)
            return result

    def _rerun_parent_if_needed_unlocked(self, round_id, agent_id):
        issue = self.native.issue(round_id)
        agent_id = agent_id or self.agent_id or issue.get("assignee_id")
        if not agent_id:
            raise OpsError("缺少 agent-id 且任务没有 assignee_id")
        runs = self.native.runs(round_id)
        active = [run for run in runs if run.get("status") in IN_FLIGHT]
        queued = [run for run in active if run.get("status") in {"queued", "dispatched", "waiting_local_directory"}]
        if queued:
            self._clear_launch_intents(round_id, round_id, f"parent-rerun:{round_id}", f"dispatch:{round_id}")
            return {"status": "reused", "runId": queued[0].get("id"), "issueId": round_id}
        running = [run for run in active if run.get("status") == "running"]
        if len(running) == 1:
            issue = self.native.issue(round_id)
            run = running[0]
            if (issue.get("status") in TERMINAL_ISSUE or issue.get("assignee_id") not in (None, agent_id)
                    or run.get("agent_id") not in (None, agent_id)):
                self._clear_launch_intents(round_id, round_id, f"parent-rerun:{round_id}")
                return {"status": "active", "issueId": round_id, "activeRunIds": sorted(run_id_set(active))}
            before = run_id_set(runs)
            key = f"parent-rerun:{round_id}"
            # 父任务只补一个可回读后继，避免 child.attention 与 lane 收口并发漏唤醒。
            return self._launch_with_intent(round_id, agent_id, before, key, allow_successor=True, round_id=round_id)
        if not active:
            return self._launch_if_idle_unlocked(round_id, agent_id, round_id=round_id)
        self._clear_launch_intents(round_id, round_id, f"parent-rerun:{round_id}")
        return {"status": "active", "issueId": round_id, "activeRunIds": sorted(run_id_set(active))}

    def rerun_parent_if_needed(self, round_id, *, agent_id=None):
        agent_id = agent_id or self.agent_id
        with file_lock(self._lock_for_round(round_id)):
            return self._rerun_parent_if_needed_unlocked(round_id, agent_id)

    def _round_terminal(self, round_id):
        scan = self.native.issue(round_id)
        if scan.get("status") in TERMINAL_ISSUE:
            return True
        if hasattr(self.native, "list_project") and scan.get("project_id"):
            bugs = [row for row in self.native.list_project(scan.get("project_id"))
                    if row.get("parent_issue_id") == round_id and metadata(row, "dailyBug").get("protocolVersion") == 3]
            if bugs:
                for bug in bugs:
                    if not self._has_receipt(bug["id"], round_id):
                        return False
                return True
        return scan.get("status") == "in_review"

    def _has_receipt(self, bug_id, round_id):
        bug = self.native.issue(bug_id)
        agent_id = self.agent_id or bug.get("assignee_id")
        scan = self.native.issue(round_id)
        return bool(self._receipt_history(bug_id, scan, agent_id))

    def acquire_global_repair_slot(self, round_id):
        # 修复槽按 FIFO 转移；holder 未达到终态前不能抢占。
        path = self.state_dir / "global-repair-slot.json"
        with file_lock(self._repair_lock()):
            slot = {"holder": None, "waitQueue": []}
            if path.exists():
                try:
                    slot.update(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError) as error:
                    raise OpsError("全局修复槽状态损坏") from error
            holder = slot.get("holder")
            if holder and holder.get("roundId") == round_id:
                return {"status": "acquired", "roundId": round_id, "reused": True}
            if holder and not self._round_terminal(holder.get("roundId")):
                queue_ids = list(dict.fromkeys([*slot.get("waitQueue", []), round_id]))
                slot["waitQueue"] = queue_ids
                atomic_write(path, slot)
                return {"status": "busy", "holder": holder, "roundId": round_id, "waitQueue": queue_ids}
            slot["holder"] = {"roundId": round_id, "acquiredAt": iso(self.now)}
            slot["waitQueue"] = [key for key in slot.get("waitQueue", []) if key != round_id]
            atomic_write(path, slot)
            return {"status": "acquired", "roundId": round_id, "reused": False}

    def release_global_repair_slot(self, round_id, *, agent_id=None):
        agent_id = agent_id or self.agent_id
        path = self.state_dir / "global-repair-slot.json"
        next_id = None
        next_agent = None
        with file_lock(self._repair_lock()):
            if not path.exists():
                return {"status": "idle", "roundId": round_id}
            try:
                slot = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise OpsError("全局修复槽状态损坏") from error
            if (slot.get("holder") or {}).get("roundId") != round_id:
                return {"status": "not_holder", "roundId": round_id}
            remaining = []
            for candidate in slot.get("waitQueue", []):
                candidate_issue = self.native.issue(candidate)
                candidate_agent = agent_id or candidate_issue.get("assignee_id")
                if next_id is None and not self._round_terminal(candidate):
                    if not candidate_agent or (candidate_issue.get("assignee_id") and agent_id
                                               and candidate_issue.get("assignee_id") != agent_id):
                        raise OpsError("修复槽下一个 round 的 agent 身份未核对，未修改槽")
                    next_id, next_agent = candidate, candidate_agent
                elif candidate != next_id:
                    remaining.append(candidate)
            slot["holder"] = {"roundId": next_id, "acquiredAt": iso(self.now)} if next_id else None
            slot["waitQueue"] = remaining
            atomic_write(path, slot)
        wake = None
        if next_id:
            # 锁序约定：先持久化并释放 global-repair-slot.lock，再获取 next_id 的 round 锁唤醒下一轮，禁止双锁嵌套死锁。
            with file_lock(self._lock_for_round(next_id)):
                wake = self._launch_if_idle_unlocked(next_id, next_agent, round_id=next_id)
        return {"status": "released", "roundId": round_id, "wokenRoundId": next_id, "wake": wake}

    def _ledger(self, ledger_issue, item):
        # 台账写入可能返回 AmbiguousWrite；先回读同一 payload，避免重复登记。
        for comment in self._fresh_comments(ledger_issue):
            if comment_json(comment, BOARD_MARKER) == item:
                return comment.get("id") or "comment-readback"
        return self._post_and_verify(ledger_issue, BOARD_MARKER, item)

    @staticmethod
    def _slack_message_time(msg_ts):
        if msg_ts is None or msg_ts == "":
            return None
        raw = str(msg_ts).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return dt.datetime.fromtimestamp(float(raw), tz=dt.timezone.utc)
        return parse_time(raw)

    def _slack_recover(self, channel, identifier, url, version=None, *, prs=None, thread_ts=None, created_at=None,
                       require_identifier=True, workspace_base=None):
        if not self.slack:
            return None
        data = self.slack.history(channel, thread_ts=thread_ts) if thread_ts else self.slack.history(channel)
        if isinstance(data, dict) and not workspace_base:
            workspace_base = data.get("workspace_url") or data.get("workspace_base")
        messages = data.get("messages", data) if isinstance(data, dict) else data
        since = parse_time(created_at) if created_at else None
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(str(identifier))}(?![A-Za-z0-9_])")
        prs = list(prs or [])
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            author = message.get("author") or message.get("user") or {}
            if author.get("is_bot") is False or author.get("bot") is False or message.get("bot") is False:
                continue
            msg_ts = message.get("ts") or message.get("timestamp") or message.get("created_at")
            if since and msg_ts:
                try:
                    parsed_ts = self._slack_message_time(msg_ts)
                    if parsed_ts is not None and parsed_ts < since - dt.timedelta(seconds=2):
                        continue
                except (OpsError, queue.QueueError, ValueError, OSError):
                    pass
            if thread_ts and message.get("thread_ts") not in (None, thread_ts) and message.get("ts") != thread_ts:
                continue
            text = " ".join(str(message.get(key, "")) for key in ("text", "text_rendered", "text_raw", "message", "fallback"))
            if require_identifier and not pattern.search(text):
                continue
            if (url or prs) and not ((url and url in text) or any(pr in text for pr in prs)):
                continue
            if version is not None and not re.search(rf"(?<!\d){re.escape(str(version))}(?!\d)", text):
                continue
            link = permalink(message)
            if not link:
                channel_id = message.get("channel_id") or message.get("channel") or channel
                if hasattr(self.slack, "resolve_permalink"):
                    link = self.slack.resolve_permalink(channel_id, message.get("ts"), message.get("thread_ts"),
                                                        workspace_base=workspace_base)
                else:
                    link = compose_slack_permalink(workspace_base, channel_id, message.get("ts"), message.get("thread_ts"))
            if not link:
                raise OpsError("Slack history 已发现已发送消息但缺少 permalink，已阻止重复发送")
            return link
        return None

    def _send_slack_once(self, channel, text_file, blocks_file, thread_ts, identifier, url, version=None,
                         *, prs=None, dry_run=False, recover_needed=False, created_at=None, require_identifier=True,
                         workspace_base=None):
        if recover_needed:
            recovered = self._slack_recover(channel, identifier, url, version, prs=prs, thread_ts=thread_ts,
                                            created_at=created_at, require_identifier=require_identifier,
                                            workspace_base=workspace_base)
            if recovered:
                return recovered, True
        result = self.slack.send(channel, text_file, blocks_file, thread_ts, dry_run=dry_run)
        link = permalink(result)
        if not link:
            raise AmbiguousWrite("Slack 发送结果没有 permalink，已保留 slackSendAttempted")
        checked = self.slack.get(link)
        if isinstance(checked, dict) and checked.get("ok") is False:
            raise OpsError("Slack 消息回读失败")
        return link, False

    def finish_scan(self, scan_id, evidence_file, *, ledger_issue, channel, text_file=None, target_status=None,
                    agent_id=None, dry_run=False, scheduler_counts=None):
        agent_id = agent_id or self.agent_id or self.native.issue(scan_id).get("assignee_id")
        with file_lock(self._lock_for_round(scan_id)):
            scan = self.native.issue(scan_id)
            scan_data = metadata(scan, "dailyScan")
            execution = metadata(scan, "dailyExecution")
            if execution.get("protocolVersion") != 3 or execution.get("policy") != POLICY:
                raise OpsError("不是 v3 scan，不能 finish")
            existing_finish = [comment_json(comment, FINISH_MARKER) for comment in self._fresh_comments(scan_id)
                              if comment.get("author_type") == "agent"
                              and (not agent_id or comment.get("author_id") == agent_id)]
            existing_finish = [item for item in existing_finish if item]
            if existing_finish:
                return existing_finish[-1]
            manifest = self.manifest(scan_id, agent_id)
            if not manifest:
                raise OpsError("manifest 未完成回读")
            # 完整调查事实来自实时评论；没有 project list 能力时由调用方在 finish 前完成注入。
            issues = self._round_issues(scan_id, scan.get("project_id"))
            lanes = [row for row in issues if metadata(row, "dailyLane").get("roundId") == scan_id]
            entries, records = self._records(scan_id, manifest, lanes, agent_id)
            if len(records) != len(manifest["candidates"]):
                raise OpsError("仍有候选没有终态调查结论")
            if scan_data.get("coverageComplete") is not True:
                raise OpsError("三源 coverageComplete 未完成")
            evidence_hash = sha256_file(evidence_file)
            confirmed = sum(item.get("conclusion") == "confirmed" for item in records.values())
            inv_cs_followed = sum(item.get("conclusion") == "cs_followed" for item in records.values())
            skipped_failure = sum(item.get("conclusion") == "skipped_failure" for item in records.values())
            counts = {**(scan_data.get("counts") or {}), **(scheduler_counts or {})}
            cs_followed_total = int(counts.get("csFollowed", 0)) + inv_cs_followed
            suspected_count = len(manifest["candidates"]) - inv_cs_followed
            scan_ident = scan.get("identifier") or scan_id
            collected_map = scan_data.get("collected") or {}
            collected_total = sum(collected_map.values()) if isinstance(collected_map, dict) else 0
            default_summary = (
                f"{scan_ident} 每日 Bug 值守｜{scan_data.get('targetDate')} 反馈扫描完成\n"
                f"采集 {collected_total} 条｜疑似候选 {suspected_count} 个｜确认可修 {confirmed} 个。"
            )
            if dry_run:
                if not self.slack or not channel:
                    raise OpsError("finish-scan 需要 Slack adapter 和 channel")
                if text_file:
                    preview = self.slack.send(channel, text_file, None, None, dry_run=True)
                else:
                    with self._write_content(default_summary) as generated:
                        preview = self.slack.send(channel, generated, None, None, dry_run=True)
                return {"status": "dry_run", "roundId": scan_id, "slackPreview": preview}
            if hasattr(self.native, "list_project") and scan.get("project_id"):
                for candidate in manifest["candidates"]:
                    record = records.get(candidate["candidateId"])
                    if record and record.get("conclusion") == "confirmed":
                        self._create_bug_if_needed(scan_id, candidate, project_id=scan.get("project_id"))
            state = self.state(scan_id)
            intent = state.setdefault("intents", {}).setdefault("finish", {"evidenceSha256": evidence_hash})
            if intent.get("evidenceSha256") != evidence_hash:
                raise OpsError("finish intent 与当前证据冲突")
            if not intent.get("evidenceAttachmentId"):
                with self._write_content("扫描证据索引") as content:
                    result = self.native.comment_add(scan_id, content, evidence_file)
                intent["evidenceAttachmentId"] = self._readback_attachment(result, evidence_hash)
                intent["evidenceReadbackVerified"] = True
                self.save_state(scan_id, state)
            outcomes = {"confirmed": confirmed,
                        "duplicate": sum(item.get("conclusion") == "duplicate" for item in records.values()),
                        "verificationBlocked": sum(item.get("conclusion") == "suspended_external" for item in records.values()),
                        "excludedAfterInvestigation": sum(item.get("conclusion") == "excluded" for item in records.values())}
            if inv_cs_followed:
                outcomes["csFollowed"] = inv_cs_followed
            if skipped_failure:
                outcomes["skippedFailure"] = skipped_failure
            scan_date = dt.datetime.fromisoformat((scan.get("created_at") or iso(self.now)).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            ledger_item = {"issue": scan_ident, "kind": "scan", "feedbackDate": scan_data.get("targetDate"),
                           "scanDate": scan_date, "collected": scan_data.get("collected"),
                           "coverageComplete": True, "suspected": suspected_count,
                           "nonBug": int(counts.get("nonBug", 0)), "productDecision": int(counts.get("productDecision", 0)),
                           "csFollowed": cs_followed_total, "outcomes": outcomes}
            ledger_id = intent.get("ledgerCommentId")
            if not ledger_id:
                ledger_id = self._ledger(ledger_issue, ledger_item)
                intent["ledgerCommentId"] = ledger_id
                intent["ledgerReadbackVerified"] = True
                self.save_state(scan_id, state)
            if not intent.get("slackPermalink"):
                if not self.slack or not channel:
                    raise OpsError("finish-scan 需要 Slack adapter 和 channel")
                # 先读取历史发送标记再置 slackSendAttempted=True：首次发送不扫 history 防误匹配，失败/结果不明重试时才从 createdAt 窗口回查防重发。
                recover_needed = bool(intent.get("slackSendAttempted") and not intent.get("slackPermalink"))
                intent["slackSendAttempted"] = True
                intent["createdAt"] = intent.get("createdAt") or iso(self.now)
                self.save_state(scan_id, state)
                if text_file:
                    link, _ = self._send_slack_once(channel, text_file, None, None, scan_ident,
                                                     None, dry_run=False, recover_needed=recover_needed,
                                                     created_at=intent.get("createdAt"), require_identifier=True)
                else:
                    with self._write_content(default_summary) as generated:
                        link, _ = self._send_slack_once(channel, generated, None, None, scan_ident,
                                                         None, dry_run=False, recover_needed=recover_needed,
                                                         created_at=intent.get("createdAt"), require_identifier=True)
                intent["slackPermalink"] = link
                intent["slackReadbackVerified"] = True
                self.save_state(scan_id, state)
            finish = {"protocolVersion": 3, "roundId": scan_id, "collected": scan_data.get("collected") or {},
                      "nonBug": int(counts.get("nonBug", 0)), "productDecision": int(counts.get("productDecision", 0)),
                      "csFollowed": cs_followed_total,
                      "counts": {"nonBug": int(counts.get("nonBug", 0)), "productDecision": int(counts.get("productDecision", 0)),
                                 "csFollowed": cs_followed_total}, "suspected": suspected_count,
                      "outcomes": outcomes, "evidenceIndex": {"attachmentId": intent["evidenceAttachmentId"],
                      "sha256": evidence_hash, "readbackVerified": True},
                      "ledger": {"issue": ledger_issue, "commentId": ledger_id, "readbackVerified": True},
                      "slack": {"permalink": intent["slackPermalink"], "readbackVerified": True}}
            self._post_and_verify(scan_id, FINISH_MARKER, finish, suffix=f" round={scan_id}")
            state["finish"] = finish
            state["intents"].pop("finish", None)
            self.save_state(scan_id, state)
            if target_status:
                self.native.status(scan_id, target_status)
            return finish

    def _bug_metadata_valid(self, bug, scan):
        item = metadata(bug, "dailyBug")
        required = {"schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": scan["id"],
                    "sourceScanId": scan["id"], "feedbackDate": metadata(scan, "dailyScan").get("targetDate")}
        if any(item.get(key) != expected for key, expected in required.items()):
            raise OpsError("dailyBug metadata 不符合 v3")
        if not isinstance(item.get("candidateId"), str) or not item["candidateId"].strip():
            raise OpsError("dailyBug candidateId 不能为空")
        if item.get("investigationRef") != f"{scan['id']}:{item['candidateId']}":
            raise OpsError("dailyBug investigationRef 不符合 v3")
        if any(key in item for key in ("state", "index", "total", "completeDelivery", "deliveryVersion", "reviewState")):
            raise OpsError("v3 dailyBug 禁止旧状态字段")
        return item

    def _create_bug_if_needed(self, scan_id, entry, *, project_id=None):
        if not hasattr(self.native, "list_project") or not project_id:
            return None
        rows = [row for row in self.native.list_project(project_id)
                if metadata(row, "dailyBug").get("candidateId") == entry["candidateId"] and row.get("parent_issue_id") == scan_id]
        if rows:
            return rows[0]
        desc_lines = [
            "mode=bug",
            f"roundId={scan_id}",
            f"candidateId={entry['candidateId']}",
        ]
        if entry.get("sourceUrl"):
            desc_lines.append(f"sourceUrl={entry['sourceUrl']}")
        summary = entry.get("summary") or ""
        if summary:
            desc_lines.extend(["", summary])
        result = self.native.create(["--project", project_id, "--title", "修复｜" + entry.get("summary", entry["candidateId"]),
                                     "--description", "\n".join(desc_lines), "--parent", scan_id])
        bug_id = recursive_value(result, ("id", "issueId", "issue_id"))
        if not bug_id:
            raise OpsError("创建 Bug 子任务未返回 issue id")
        data = {"schemaVersion": 1, "protocolVersion": 3, "mode": "bug", "batchId": scan_id,
                "sourceScanId": scan_id, "candidateId": entry["candidateId"],
                "investigationRef": f"{scan_id}:{entry['candidateId']}",
                "feedbackDate": entry["feedbackDate"], "source": entry.get("source"), "sourceUrl": entry.get("sourceUrl")}
        self.native.metadata_set(bug_id, "dailyBug", data)
        return self.native.issue(bug_id)

    def _receipt_history(self, bug_id, scan, agent_id=None):
        """按评论时间合并 delivery/blocked，只有完整 v3 回执才参与状态机。"""
        agent_id = agent_id or self.agent_id
        bug = self.native.issue(bug_id)
        bug_data = metadata(bug, "dailyBug")
        values = []
        for index, comment in enumerate(self._fresh_comments(bug_id)):
            if comment.get("author_type") != "agent" or (agent_id and comment.get("author_id") != agent_id):
                continue
            for marker, state in ((DELIVERY_MARKER, "delivered"), (BLOCKED_MARKER, "blocked")):
                item = comment_json(comment, marker)
                if not isinstance(item, dict) or item.get("protocolVersion") != 3:
                    continue
                if (item.get("issueId") != bug_id or item.get("candidateId") != bug_data.get("candidateId")
                        or item.get("roundId") != scan.get("id")):
                    continue
                slack, ledger = item.get("slack"), item.get("ledger")
                common = (isinstance(slack, dict) and slack.get("readbackVerified") is True
                          and isinstance(slack.get("permalink"), str) and slack["permalink"]
                          and isinstance(ledger, dict) and ledger.get("readbackVerified") is True
                          and ledger.get("issue") and ledger.get("commentId"))
                if state == "delivered":
                    prs = item.get("prs")
                    valid = (common and type(item.get("deliveryVersion")) is int and item["deliveryVersion"] >= 1
                             and isinstance(prs, list) and bool(prs) and len(prs) == len(item.get("prHeads") or [])
                             and all(isinstance(url, str) and PR_RE.fullmatch(url) for url in prs)
                             and all(isinstance(head, str) and SHA_RE.fullmatch(head) for head in item["prHeads"])
                             and isinstance(item.get("report"), dict)
                             and queue.valid_attachment_id(item["report"].get("attachmentId"))
                             and item["report"].get("sha256") and item["report"].get("readbackVerified") is True)
                else:
                    blocker = item.get("blocker")
                    valid = common and isinstance(blocker, dict) and all(
                        isinstance(blocker.get(key), str) and blocker[key].strip()
                        for key in ("kind", "reason", "resumeCondition"))
                if valid:
                    values.append((parse_time(comment.get("created_at") or iso(self.now)), index, state, item))
        return sorted(values, key=lambda row: (row[0], row[1]))

    def _bug_receipt(self, bug_id, marker=None, agent_id=None):
        scan_id = metadata(self.native.issue(bug_id), "dailyBug").get("sourceScanId")
        if not scan_id:
            return None
        scan = self.native.issue(scan_id)
        values = self._receipt_history(bug_id, scan, agent_id)
        if marker:
            values = [row for row in values if (marker == DELIVERY_MARKER and row[2] == "delivered")
                      or (marker == BLOCKED_MARKER and row[2] == "blocked")]
        return values[-1][3] if values else None

    @staticmethod
    def _upload_thread_ts(link, explicit, slack):
        if explicit:
            return explicit
        match = re.search(r"p(\d{10})(\d{6})(?:\D|$)", link or "")
        if match:
            return f"{match.group(1)}.{match.group(2)}"
        checked = slack.get(link) if slack else {}
        return checked.get("thread_ts") or checked.get("ts") if isinstance(checked, dict) else None

    @staticmethod
    def _uploaded_file_ids(upload_result):
        ids = set()
        if not isinstance(upload_result, dict):
            return ids
        for key in ("uploaded", "files"):
            items = upload_result.get(key)
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict):
                        for id_key in ("id", "file_id", "fileId"):
                            if isinstance(entry.get(id_key), str) and entry[id_key]:
                                ids.add(entry[id_key])
        if isinstance(upload_result.get("file"), dict):
            for id_key in ("id", "file_id", "fileId"):
                if isinstance(upload_result["file"].get(id_key), str) and upload_result["file"][id_key]:
                    ids.add(upload_result["file"][id_key])
        for id_key in ("file_id", "fileId"):
            if isinstance(upload_result.get(id_key), str) and upload_result[id_key]:
                ids.add(upload_result[id_key])
        if not ids and "channel" not in upload_result and isinstance(upload_result.get("id"), str) and upload_result["id"]:
            ids.add(upload_result["id"])
        return ids

    @classmethod
    def _attachment_in_replies(cls, value, digest, upload_result=None):
        upload_ids = cls._uploaded_file_ids(upload_result)
        def walk(item, in_file_list=False):
            if isinstance(item, dict):
                if item.get("sha256") == digest or item.get("sha256Hash") == digest:
                    return True
                if upload_ids and (in_file_list or any(k in item for k in ("file_id", "fileId", "url_private", "mimetype"))):
                    if any(item.get(key) in upload_ids for key in ("id", "file_id", "fileId")):
                        return True
                for list_key in ("files", "attachments"):
                    if isinstance(item.get(list_key), list) and walk(item[list_key], True):
                        return True
                return any(walk(child, False) for key, child in item.items() if key not in ("files", "attachments", "channel"))
            if isinstance(item, list):
                return any(walk(child, in_file_list) for child in item)
            return False
        return walk(value)

    def _delivery_common(self, bug_id, report_file, *, blocked=False, ledger_issue=None, channel=None,
                         text_file=None, blocks_file=None, thread_ts=None, blocker=None, prs=None, pr_heads=None,
                         agent_id=None, dry_run=False, edit_blocked_url=None, edit_blocked_text_file=None):
        bug = self.native.issue(bug_id)
        agent_id = agent_id or self.agent_id or bug.get("assignee_id")
        daily_bug = metadata(bug, "dailyBug")
        scan_id = daily_bug.get("sourceScanId") or daily_bug.get("batchId")
        if not scan_id:
            raise OpsError("Bug 缺少 sourceScanId")
        scan = self.native.issue(scan_id)
        self._bug_metadata_valid(bug, scan)
        candidate_id = daily_bug.get("candidateId")
        if blocked:
            if not isinstance(blocker, dict) or not all(isinstance(blocker.get(key), str) and blocker[key].strip()
                                                         for key in ("kind", "reason", "resumeCondition")):
                raise OpsError("blocker 字段不完整")
            prs, pr_heads, report_hash = prs or [], pr_heads or [], None
        else:
            if not report_file or not Path(report_file).is_file():
                raise OpsError("deliver-bug 必须提供 HTML report-file")
            prs = prs or []
            pr_heads = pr_heads or []
            if not prs or len(prs) != len(pr_heads) or any(not PR_RE.fullmatch(url) for url in prs):
                raise OpsError("delivery receipt 的 PR 列表无效")
            if any(not SHA_RE.fullmatch(head) for head in pr_heads):
                raise OpsError("delivery receipt 的 PR head 无效")
            report_hash = sha256_file(report_file)
        with file_lock(self._lock_for_round(scan_id)):
            # delivery/blocked 是同一条按时间排序的状态机；新版本必须使用新 intent key。
            history = self._receipt_history(bug_id, scan, agent_id)
            latest = history[-1] if history else None
            currently_blocked = bool(latest and latest[2] == "blocked")
            max_version = max((row[3].get("deliveryVersion", 0) for row in history if row[2] == "delivered"), default=0)
            block_version = sum(1 for row in history if row[2] == "blocked") + 1
            if blocked and latest and latest[2] == "blocked":
                old = latest[3]
                if old.get("blocker") == blocker and old.get("ledger", {}).get("issue") == ledger_issue:
                    if dry_run:
                        return {"status": "dry_run", "issueId": bug_id, "roundId": scan_id, "idempotent": True}
                    self.native.status(bug_id, "blocked")
                    self._rerun_parent_if_needed_unlocked(scan_id, agent_id)
                    return old
            if not blocked and latest and latest[2] == "delivered":
                old = latest[3]
                if (old.get("prs") == prs and old.get("prHeads") == pr_heads
                        and old.get("report", {}).get("sha256") == report_hash
                        and old.get("ledger", {}).get("issue") == ledger_issue):
                    if dry_run:
                        return {"status": "dry_run", "issueId": bug_id, "roundId": scan_id, "idempotent": True}
                    self.native.status(bug_id, "in_review")
                    self._rerun_parent_if_needed_unlocked(scan_id, agent_id)
                    return old
            version = max_version + 1
            if dry_run:
                if not self.slack or not channel or not text_file:
                    raise OpsError("交付需要 Slack channel 和 text-file")
                edit_preview = None
                if not blocked and (currently_blocked or edit_blocked_url):
                    blocked_url = edit_blocked_url or latest[3].get("slack", {}).get("permalink")
                    if blocked_url:
                        edit_text = edit_blocked_text_file or text_file
                        edit_blocks = None if edit_blocked_text_file else blocks_file
                        try:
                            edit_preview = self.slack.edit(blocked_url, edit_text, edit_blocks, dry_run=True)
                        except TypeError:
                            edit_preview = self.slack.edit(blocked_url, edit_text, edit_blocks)
                send_preview = self.slack.send(channel, text_file, blocks_file, thread_ts, dry_run=True)
                return {"status": "dry_run", "issueId": bug_id, "roundId": scan_id, "version": version,
                        "slackPreview": send_preview, **({"editPreview": edit_preview} if edit_preview else {})}
            key = f"blocked:{bug_id}:v{block_version}" if blocked else f"delivery:{bug_id}:v{version}"
            state = self.state(scan_id)
            intent = state.setdefault("intents", {}).setdefault(key, {"issueId": bug_id, "roundId": scan_id,
                                                                        "version": version})
            if not blocked and intent.get("reportSha256") and intent["reportSha256"] != report_hash:
                raise OpsError("delivery intent 与当前报告冲突")
            if not blocked and not intent.get("reportAttachmentId"):
                # 先上传并回读报告附件；重入复用同一 reportAttachmentId 与 reportSha256，避免重复上传。
                with self._write_content("交付 HTML 报告") as content:
                    result = self.native.comment_add(bug_id, content, report_file)
                intent["reportAttachmentId"] = self._readback_attachment(result, report_hash)
                intent["reportSha256"] = report_hash
                intent["reportReadbackVerified"] = True
                self.save_state(scan_id, state)
            if not blocked and (currently_blocked or edit_blocked_url):
                blocked_url = edit_blocked_url or latest[3].get("slack", {}).get("permalink")
                if blocked_url and not intent.get("blockedEdited"):
                    # 每个 intent 最多编辑一次原受阻消息，随后再发新的交付通知，保证断点续作幂等。
                    edit_text = edit_blocked_text_file or text_file
                    edit_blocks = None if edit_blocked_text_file else blocks_file
                    if not self.slack or not edit_text or not hasattr(self.slack, "edit"):
                        raise OpsError("受阻恢复缺少 Slack 编辑材料")
                    try:
                        self.slack.edit(blocked_url, edit_text, edit_blocks, dry_run=False)
                    except TypeError:
                        self.slack.edit(blocked_url, edit_text, edit_blocks)
                    checked = self.slack.get(blocked_url)
                    if isinstance(checked, dict) and checked.get("ok") is False:
                        raise OpsError("原受阻 Slack 顶层消息编辑回读失败")
                    intent["blockedEdited"] = True
                    self.save_state(scan_id, state)
            if not intent.get("slackPermalink"):
                if not self.slack or not channel or not text_file:
                    raise OpsError("交付需要 Slack channel 和 text-file")
                recover_needed = bool(intent.get("slackSendAttempted") and not intent.get("slackPermalink"))
                intent["slackSendAttempted"] = True
                intent["createdAt"] = intent.get("createdAt") or iso(self.now)
                self.save_state(scan_id, state)
                known_base = workspace_base_from_url(
                    edit_blocked_url or (latest[3].get("slack", {}).get("permalink") if latest else None)
                )
                link, _ = self._send_slack_once(
                    channel, text_file, blocks_file, thread_ts, bug.get("identifier") or bug_id,
                    daily_bug.get("sourceUrl"), version if (not blocked and version > 1) else None,
                    prs=prs, dry_run=False, recover_needed=recover_needed,
                    created_at=intent.get("createdAt"), workspace_base=known_base)
                intent["slackPermalink"] = link
                intent["slackReadbackVerified"] = True
                # 先保存 permalink，再上传附件；附件失败时恢复不会重发顶层消息。
                self.save_state(scan_id, state)
            # 附件上传留在 slackPermalink 持久化之外，允许 Slack 发送成功但附件上传失败时在同一线程续传。
            if not blocked and report_file and not intent.get("slackAttachmentReadbackVerified"):
                link = intent["slackPermalink"]
                upload_thread_ts = self._upload_thread_ts(link, thread_ts, self.slack)
                upload = self.slack.upload(channel, report_file, intent["reportSha256"], "完整 HTML 交付报告",
                                           thread_ts=upload_thread_ts)
                replies = self.slack.replies(link)
                if not self._attachment_in_replies(replies, intent["reportSha256"], upload):
                    raise OpsError("Slack 交付附件未在 thread 回读")
                intent["slackAttachmentReadbackVerified"] = True
                self.save_state(scan_id, state)
            if blocked:
                ledger_item = {"issue": bug.get("identifier") or bug_id, "kind": "bug", "state": "blocked",
                               "feedbackDate": daily_bug.get("feedbackDate"), "source": daily_bug.get("source"),
                               "sourceUrl": daily_bug.get("sourceUrl"), "candidateId": candidate_id, "prs": prs,
                               "delivery": {"permalink": intent["slackPermalink"]}, "blocker": blocker}
            else:
                ledger_item = {"issue": bug.get("identifier") or bug_id, "kind": "bug", "feedbackDate": daily_bug.get("feedbackDate"),
                               "source": daily_bug.get("source"), "sourceUrl": daily_bug.get("sourceUrl"),
                               "candidateId": candidate_id, "prs": prs, "delivery": {"permalink": intent["slackPermalink"]}}
            ledger_id = intent.get("ledgerCommentId")
            if not ledger_id:
                ledger_id = self._ledger(ledger_issue, ledger_item)
                intent["ledgerCommentId"] = ledger_id
                intent["ledgerReadbackVerified"] = True
                self.save_state(scan_id, state)
            if blocked:
                receipt = {"protocolVersion": 3, "issueId": bug_id, "roundId": scan_id, "candidateId": candidate_id,
                           "blocker": blocker, "slack": {"permalink": intent["slackPermalink"], "readbackVerified": True},
                           "ledger": {"issue": ledger_issue, "commentId": ledger_id, "readbackVerified": True}}
                marker, suffix = BLOCKED_MARKER, f" round={scan_id} issue={bug_id}"
            else:
                receipt = {"protocolVersion": 3, "issueId": bug_id, "roundId": scan_id, "candidateId": candidate_id,
                           "deliveryVersion": version, "prs": prs, "prHeads": pr_heads,
                           "report": {"attachmentId": intent["reportAttachmentId"], "sha256": intent["reportSha256"], "readbackVerified": True},
                           "slack": {"permalink": intent["slackPermalink"], "readbackVerified": True},
                           "ledger": {"issue": ledger_issue, "commentId": ledger_id, "readbackVerified": True}}
                marker, suffix = DELIVERY_MARKER, f" round={scan_id} issue={bug_id} version={version}"
            self._post_and_verify(bug_id, marker, receipt, suffix=suffix)
            state["intents"].pop(key, None)
            state.setdefault("receipts", {})[bug_id] = receipt
            self.save_state(scan_id, state)
            self.native.status(bug_id, "blocked" if blocked else "in_review")
            self._rerun_parent_if_needed_unlocked(scan_id, agent_id)
            return receipt

    def deliver_bug(self, **kwargs):
        return self._delivery_common(kwargs.pop("bug_id"), kwargs.pop("report_file"), blocked=False, **kwargs)

    def block_bug(self, **kwargs):
        return self._delivery_common(kwargs.pop("bug_id"), None, blocked=True, **kwargs)

    def inspect_round(self, round_id, *, project_id=None, agent_id=None):
        agent_id = agent_id or self.agent_id or self.native.issue(round_id).get("assignee_id")
        scan = self.native.issue(round_id)
        if self._legacy_scan(scan):
            return {"status": "legacy_round_delegate", "roundId": round_id, "mutated": False}
        manifest = self.manifest(round_id, agent_id)
        state = self.state(round_id)
        issues = self._round_issues(round_id, project_id)
        lanes = [row for row in issues if metadata(row, "dailyLane").get("roundId") == round_id]
        bugs = [row for row in issues if metadata(row, "dailyBug").get("sourceScanId") == round_id]
        records = {}
        if manifest:
            _, records = self._records(round_id, manifest, lanes, agent_id)
        has_receipt_by_bug = {bug["id"]: bool(self._receipt_history(bug["id"], scan, agent_id)) for bug in bugs}
        missing_receipts = [
            bug.get("id") for bug in bugs
            if bug.get("status") in {"in_review", "done", "cancelled", "canceled"}
            and not has_receipt_by_bug[bug["id"]]
        ]
        runs_by_issue = {row["id"]: self.native.runs(row["id"]) for row in [scan, *lanes, *bugs]}
        def is_active(row_id):
            return any(
                run.get("issue_id") == row_id
                and run.get("status") in IN_FLIGHT
                and (not agent_id or run.get("agent_id") == agent_id)
                for run in runs_by_issue[row_id]
            )
        child_active = any(is_active(row["id"]) for row in [*lanes, *bugs])
        investigation_open = manifest is None or len(records) < len(manifest["candidates"])
        confirmed_count = sum(item.get("conclusion") == "confirmed" for item in records.values())
        repair_open = len(bugs) < confirmed_count or any(not has_receipt_by_bug[bug["id"]] for bug in bugs)
        scan_data = metadata(scan, "dailyScan")
        sources = scan_data.get("sources") or {}
        source_failed = isinstance(sources, dict) and any(
            isinstance(value, dict) and value.get("status") in {"failed", "error"}
            for value in sources.values()
        )
        has_finish = any(
            comment_json(comment, FINISH_MARKER)
            and comment.get("author_type") == "agent"
            and (not agent_id or comment.get("author_id") == agent_id)
            for comment in self._fresh_comments(round_id)
        )
        # 与 duty-progress 保持同一恢复口径：只恢复确有未完成工作、未结案工位或无回执 Bug 的 in_progress 任务。
        scan_known_work = (
            scan_data.get("coverageComplete") is not True
            or source_failed
            or investigation_open
            or not has_finish
            or repair_open
        )
        needs = []
        if scan.get("status") == "in_progress" and not child_active and not is_active(round_id) and scan_known_work:
            needs.append(round_id)
        if manifest is not None and len(records) < len(manifest["candidates"]):
            for lane in lanes:
                if lane.get("status") == "in_progress" and not is_active(lane["id"]):
                    needs.append(lane["id"])
        for bug in bugs:
            if bug.get("status") == "in_progress" and not has_receipt_by_bug[bug["id"]] and not is_active(bug["id"]):
                needs.append(bug["id"])
        slot_path = self.state_dir / "global-repair-slot.json"
        if slot_path.exists():
            try:
                slot = json.loads(slot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise OpsError("全局修复槽状态损坏") from error
        else:
            slot = {"holder": None, "waitQueue": []}
        return {"status": "ok", "roundId": round_id, "manifest": manifest,
                "candidateCount": len(manifest["candidates"]) if manifest else None, "concluded": len(records),
                "lanes": [{"id": row["id"], "metadata": metadata(row, "dailyLane")} for row in lanes],
                "bugs": [{"id": row["id"], "status": row.get("status"), "hasReceipt": has_receipt_by_bug[row["id"]]} for row in bugs],
                "missingReceipt": missing_receipts, "needsRecovery": needs, "state": state, "globalRepairSlot": slot,
                "advisoryNextStep": "recover" if needs or missing_receipts else "继续当前阶段"}

    def recover(self, round_id, *, project_id=None, apply=False, agent_id=None):
        agent_id = agent_id or self.agent_id or self.native.issue(round_id).get("assignee_id")
        scan = self.native.issue(round_id)
        if self._legacy_scan(scan):
            return {"status": "legacy_round_delegate", "roundId": round_id, "mutated": False}
        if not apply:
            report = self.inspect_round(round_id, project_id=project_id, agent_id=agent_id)
            report["mutated"] = False
            return report
        with file_lock(self._lock_for_round(round_id)):
            # apply 前重新读取，避免锁外快照把已恢复的 receipt/run 当成缺口。
            report = self.inspect_round(round_id, project_id=project_id, agent_id=agent_id)
            report["mutated"] = False
            for bug_id in report["missingReceipt"]:
                bug = self.native.issue(bug_id)
                if bug.get("status") in {"in_review", "done", "cancelled", "canceled"}:
                    report.setdefault("warnings", []).append(f"{bug_id} 终态缺少 v3 回执，保留待人工核对")
            for issue_id in report["needsRecovery"]:
                if issue_id == round_id or issue_id in {lane["id"] for lane in report["lanes"]} or issue_id in {bug["id"] for bug in report["bugs"]}:
                    try:
                        self._launch_if_idle_unlocked(issue_id, agent_id, round_id=round_id)
                        report["mutated"] = True
                    except OpsError as error:
                        report.setdefault("errors", []).append({"issueId": issue_id, "reason": str(error)})
            return report


def _parse_bool_arg(value):
    if value is None or isinstance(value, bool):
        return value
    norm = str(value).strip().lower()
    if norm in {"true", "1", "yes"}:
        return True
    if norm in {"false", "0", "no"}:
        return False
    raise OpsError("--coverage-complete 必须是 true 或 false")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-scan-manifest", "claim", "submit-investigation",
                                             "dispatch-if-issue-idle", "rerun-parent-if-needed",
                                             "acquire-global-repair-slot", "release-global-repair-slot",
                                             "finish-scan", "update-scan-status", "deliver-bug", "block-bug", "inspect-round", "recover"))
    parser.add_argument("--server-url", default="https://multica.devops.moego.dev")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--runtime-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--project-id")
    parser.add_argument("--scan-id", "--round-id", dest="round_id")
    parser.add_argument("--issue-id")
    parser.add_argument("--bug-id")
    parser.add_argument("--lane-issue-id")
    parser.add_argument("--worker-id")
    parser.add_argument("--candidate-id")
    parser.add_argument("--fencing-token", type=int)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    parser.add_argument("--manifest-file")
    parser.add_argument("--report-file")
    parser.add_argument("--record-file")
    parser.add_argument("--evidence-file")
    parser.add_argument("--scheduler-revision")
    parser.add_argument("--target-date")
    parser.add_argument("--ledger-issue", default="GRM-664")
    parser.add_argument("--channel")
    parser.add_argument("--text-file")
    parser.add_argument("--blocks-file")
    parser.add_argument("--thread-ts")
    parser.add_argument("--slack-cli", default=os.path.expanduser("~/.agents/skills/slack/scripts/slack.py"))
    parser.add_argument("--target-status")
    parser.add_argument("--source")
    parser.add_argument("--source-url")
    parser.add_argument("--source-status")
    parser.add_argument("--blocker-file")
    parser.add_argument("--prs", nargs="*", default=[])
    parser.add_argument("--pr-heads", nargs="*", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--edit-blocked-url")
    parser.add_argument("--edit-blocked-text-file")
    parser.add_argument("--collected")
    parser.add_argument("--counts")
    parser.add_argument("--coverage-complete", nargs="?", const="true", default=None)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    runtime_id = args.runtime_id
    try:
        for value, name in ((args.workspace_id, "workspaceId"), (args.round_id, "roundId"),
                            (args.runtime_id, "runtimeId"), (args.issue_id, "issueId"),
                            (args.bug_id, "bugId"), (args.lane_issue_id, "laneIssueId")):
            if value:
                validate_id(value, name)
        native = Native(args.server_url, args.workspace_id)
        slack = Slack(args.slack_cli) if args.command in {"finish-scan", "deliver-bug", "block-bug"} else None
        ops = Ops(native, slack, args.state_dir, agent_id=args.agent_id,
                  runtime_id=runtime_id, workspace_id=args.workspace_id)
        for value, name in ((args.round_id, "roundId"), (args.runtime_id, "runtimeId"),
                            (args.issue_id, "issueId"), (args.bug_id, "bugId"),
                            (args.lane_issue_id, "laneIssueId")):
            if value:
                ops._validate_id(value, name)
        if args.command == "init-scan-manifest":
            result = ops.init_scan_manifest(args.round_id, args.manifest_file, target_date=args.target_date,
                scheduler_revision=args.scheduler_revision, runtime_id=runtime_id, project_id=args.project_id, agent_id=args.agent_id)
        elif args.command == "claim":
            result = ops.claim(args.round_id, args.lane_issue_id, worker_id=args.worker_id,
                                ttl_seconds=args.ttl_seconds, agent_id=args.agent_id)
        elif args.command == "submit-investigation":
            if not args.candidate_id:
                raise OpsError("submit-investigation 必须显式提供 --candidate-id")
            result = ops.submit_investigation(args.round_id, args.lane_issue_id, args.candidate_id,
                                              args.fencing_token, args.record_file, args.report_file,
                                              agent_id=args.agent_id)
        elif args.command == "dispatch-if-issue-idle":
            result = ops.dispatch_if_issue_idle(args.issue_id, agent_id=args.agent_id, round_id=args.round_id)
        elif args.command == "rerun-parent-if-needed":
            result = ops.rerun_parent_if_needed(args.round_id, agent_id=args.agent_id)
        elif args.command == "acquire-global-repair-slot":
            result = ops.acquire_global_repair_slot(args.round_id)
        elif args.command == "release-global-repair-slot":
            result = ops.release_global_repair_slot(args.round_id, agent_id=args.agent_id)
        elif args.command == "update-scan-status":
            result = ops.update_scan_status(
                args.round_id, target_date=args.target_date, source=args.source, source_url=args.source_url,
                source_status=args.source_status,
                collected=json.loads(args.collected) if args.collected else None,
                counts=json.loads(args.counts) if args.counts else None,
                coverage_complete=_parse_bool_arg(args.coverage_complete))
        elif args.command == "finish-scan":
            result = ops.finish_scan(args.round_id, args.evidence_file, ledger_issue=args.ledger_issue,
                                     channel=args.channel, text_file=args.text_file, target_status=args.target_status,
                                     dry_run=args.dry_run, agent_id=args.agent_id)
        elif args.command == "deliver-bug":
            result = ops.deliver_bug(bug_id=args.bug_id, report_file=args.report_file, ledger_issue=args.ledger_issue,
                                     channel=args.channel, text_file=args.text_file, blocks_file=args.blocks_file,
                                     thread_ts=args.thread_ts, prs=args.prs, pr_heads=args.pr_heads,
                                     agent_id=args.agent_id, dry_run=args.dry_run,
                                     edit_blocked_url=args.edit_blocked_url, edit_blocked_text_file=args.edit_blocked_text_file)
        elif args.command == "block-bug":
            blocker = json_from_file(args.blocker_file) if args.blocker_file else None
            result = ops.block_bug(bug_id=args.bug_id, blocker=blocker, ledger_issue=args.ledger_issue,
                                   channel=args.channel, text_file=args.text_file, blocks_file=args.blocks_file,
                                   thread_ts=args.thread_ts, agent_id=args.agent_id, dry_run=args.dry_run)
        elif args.command == "inspect-round":
            result = ops.inspect_round(args.round_id, project_id=args.project_id, agent_id=args.agent_id)
        else:
            result = ops.recover(args.round_id, project_id=args.project_id, apply=args.apply, agent_id=args.agent_id)
        print(json.dumps({"protocolVersion": PROTOCOL_VERSION, **result}, ensure_ascii=False, sort_keys=True))
        return 0
    except OSError:
        print("duty-ops 未完成：本地文件或命令操作失败", file=sys.stderr)
        return 1
    except (OpsError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"duty-ops 未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
