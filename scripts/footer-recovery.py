#!/usr/bin/env python3
"""查询或重放单个已登记运行；凭据只从运维环境读取。"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from reply import NoRedirect


def request(url, token, ref, operation):
    body = json.dumps({**ref, "operation": operation}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "authorization": "Bearer " + token,
        "content-type": "application/json",
    })
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=55) as response:
        result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["inspect", "retry"])
    parser.add_argument("--issue", required=True, type=uuid.UUID)
    parser.add_argument("--task", required=True, type=uuid.UUID)
    args = parser.parse_args()
    url = os.environ.get("RELAY_RECOVERY_URL", "")
    parsed = urllib.parse.urlsplit(url)
    token = os.environ.get("CRON_SECRET", "")
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment or
            parsed.path != "/api/footer/recovery" or len(token) < 32):
        parser.error("需要准确的 HTTPS RELAY_RECOVERY_URL 和至少 32 字符的 CRON_SECRET")
    ref = {"version": 1, "issueId": str(args.issue), "taskId": str(args.task)}
    try:
        before = request(url, token, ref, "inspect")
        if args.operation == "inspect":
            print(json.dumps(before, ensure_ascii=False, indent=2))
            return 0
        if not before.get("messageTs"):
            print("没有已登记回复，不能重放。", file=sys.stderr)
            return 1
        if before.get("done"):
            print("该运行已经完成 footer 更新，无需重放。")
            return 0
        result = request(url, token, ref, "retry")
        after = request(url, token, ref, "inspect")
        print(json.dumps({"result": result, "state": after}, ensure_ascii=False, indent=2))
        return 0
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        # 响应不明时先查状态，不输出认证信息或服务端原始错误正文。
        print("请求失败或结果不明，请先用 inspect 核对同一运行，勿盲目重复 retry。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
