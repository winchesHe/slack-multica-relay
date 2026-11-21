#!/usr/bin/env python3
"""GRM 每日 Bug 值守 v3 轻量自愈巡检脚本（纯本地 CLI，零 LLM 调用）。

每轮由 launchd 周期触发：
- 先通过一次分页的 `multica issue list --status in_progress` 查询当前仍处于 `in_progress` 的 v3 轮次；
- 仅筛选 `dailyExecution.protocolVersion == 3` / `dailyLane.protocolVersion == 3` / `dailyBug.protocolVersion == 3` 的轮次，严格忽略历史 v1/v2 轮次及 `blocked`/`done`/`cancelled`/`in_review` 任务；
- 若不存在活跃 v3 `in_progress` 轮次，直接清理已完结轮次巡检计数并在 <1s 内以 `status=idle` 退出；
- 对存在 `needsRecovery` 的活跃轮次执行 `recover --apply` 续推，同时提供无进展连续重启熔断（`--max-no-progress-restarts`）并回收已达终态却阻塞等待队列的全局修复槽持有者。
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

_DEFAULT_OPS_PATH = Path(__file__).with_name("duty-ops.py")


def load_ops_module(ops_script=None):
    target = Path(ops_script) if ops_script else _DEFAULT_OPS_PATH
    spec = importlib.util.spec_from_file_location("duty_ops_watchdog_target", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def list_in_progress_issues(native, project_id):
    """分页读取项目下所有 status=in_progress 的任务，仅取必要轻量字段。"""
    rows, offset, total = [], 0, None
    fields = "id,identifier,status,assignee_id,parent_issue_id,metadata,created_at"
    while True:
        page = native.call([
            "issue", "list",
            "--project", project_id,
            "--status", "in_progress",
            "--fields", fields,
            "--limit", "100",
            "--offset", str(offset),
            "--sort", "created_at",
            "--direction", "asc",
        ])
        batch = page.get("issues") if isinstance(page, dict) else None
        if not isinstance(batch, list) or type(page.get("has_more")) is not bool or type(page.get("total")) is not int:
            raise RuntimeError("项目 in_progress 任务分页响应无效")
        total = page["total"] if total is None else total
        if page["total"] != total or page.get("offset") != offset:
            raise RuntimeError("项目 in_progress 任务分页在读取期间发生变化")
        rows.extend(batch)
        if not page["has_more"]:
            if len(rows) != total or len({row.get("id") for row in rows}) != total:
                raise RuntimeError("项目 in_progress 任务分页总数不闭合")
            return rows
        if not batch:
            raise RuntimeError("项目 in_progress 任务分页未推进")
        offset += len(batch)


def extract_v3_active_rounds(issues, ops_mod, agent_id, policy):
    """仅从 v3 in_progress 任务中提取 roundId，绝不触碰 legacy v1/v2 或已移交他人任务。"""
    rounds = {}
    for row in issues:
        if not isinstance(row, dict) or row.get("status") != "in_progress":
            continue
        if row.get("assignee_id") not in (None, agent_id):
            continue
        execution = ops_mod.metadata(row, "dailyExecution")
        lane = ops_mod.metadata(row, "dailyLane")
        bug = ops_mod.metadata(row, "dailyBug")
        legacy = any(ops_mod.metadata(row, key) for key in ("dailyRound", "dailyBatch", "dailyDispatch"))
        if legacy:
            continue
        round_id = None
        if execution.get("protocolVersion") == 3 and execution.get("policy") == policy:
            round_id = row.get("id")
        elif lane.get("protocolVersion") == 3 and isinstance(lane.get("roundId"), str):
            round_id = lane["roundId"]
        elif bug.get("protocolVersion") == 3 and isinstance(bug.get("sourceScanId"), str):
            round_id = bug["sourceScanId"]
        if round_id and round_id not in rounds:
            rounds[round_id] = row.get("created_at") or ""
    return [rid for rid, _ in sorted(rounds.items(), key=lambda pair: (pair[1], pair[0]))]


def _load_watchdog_state(path):
    if not path.exists():
        return {"schemaVersion": 1, "rounds": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rounds"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"schemaVersion": 1, "rounds": {}}


def _progress_signature(report):
    concluded = report.get("concluded")
    bugs = report.get("bugs") or []
    receipts = sum(1 for b in bugs if b.get("hasReceipt"))
    needs = ",".join(sorted(report.get("needsRecovery") or []))
    return f"{concluded}:{len(bugs)}:{receipts}:{needs}"


def run_watchdog(ops_mod, ops, *, project_id, agent_id, max_no_progress_restarts=5, dry_run=False):
    if type(max_no_progress_restarts) is not int or max_no_progress_restarts < 1:
        raise ops_mod.OpsError("max-no-progress-restarts 必须是正整数")
    state_dir = Path(ops.state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / "watchdog.lock"
    state_path = state_dir / "watchdog-state.json"

    with ops_mod.file_lock(lock_path):
        wd_state = _load_watchdog_state(state_path)
        rounds_state = wd_state.setdefault("rounds", {})

        # 回收已终态却仍占着修复槽阻塞 waitQueue 的持有人。
        slot_released = None
        slot_path = state_dir / "global-repair-slot.json"
        if not dry_run and slot_path.exists():
            try:
                slot = json.loads(slot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                slot = {}
            holder = slot.get("holder") if isinstance(slot, dict) else None
            holder_id = holder.get("roundId") if isinstance(holder, dict) else (holder if isinstance(holder, str) else None)
            wait_q = slot.get("waitQueue") if isinstance(slot, dict) else None
            if holder_id and wait_q and ops._round_terminal(holder_id):
                slot_released = ops.release_global_repair_slot(holder_id, agent_id=agent_id)

        issues = list_in_progress_issues(ops.native, project_id)
        active_rounds = extract_v3_active_rounds(issues, ops_mod, agent_id, ops_mod.POLICY)

        # 清理已不在 in_progress 的历史巡检计数，避免状态文件膨胀。
        stale_keys = [rid for rid in list(rounds_state) if rid not in set(active_rounds)]
        for rid in stale_keys:
            rounds_state.pop(rid, None)

        if not active_rounds:
            if not dry_run and (stale_keys or state_path.exists()):
                ops_mod.atomic_write(state_path, wd_state)
            result = {"status": "idle", "activeRounds": [], "recovered": [], "circuitOpen": []}
            if slot_released is not None:
                result["slotReleased"] = slot_released
            return result

        recovered = []
        circuit_open = []
        inspected = []
        for round_id in active_rounds:
            report = ops.inspect_round(round_id, project_id=project_id, agent_id=agent_id)
            if report.get("status") == "legacy_round_delegate":
                rounds_state.pop(round_id, None)
                continue
            needs = report.get("needsRecovery") or []
            sig = _progress_signature(report)
            inspected.append({
                "roundId": round_id,
                "concluded": report.get("concluded"),
                "candidateCount": report.get("candidateCount"),
                "needsRecovery": needs,
                "missingReceipt": report.get("missingReceipt") or [],
            })
            if not needs:
                rounds_state.pop(round_id, None)
                continue

            prev = rounds_state.get(round_id) or {}
            consecutive = (int(prev.get("consecutiveRestarts", 0)) + 1) if prev.get("lastSignature") == sig else 1
            if consecutive > max_no_progress_restarts:
                circuit_open.append({
                    "roundId": round_id,
                    "needsRecovery": needs,
                    "consecutiveRestarts": consecutive - 1,
                    "reason": "连续多次恢复均无进展，已触发熔断等待人工核对",
                })
                continue

            if dry_run:
                recovered.append({"roundId": round_id, "dryRun": True, "wouldRecover": needs})
                continue

            rec = ops.recover(round_id, project_id=project_id, apply=True, agent_id=agent_id)
            rounds_state[round_id] = {
                "lastSignature": sig,
                "consecutiveRestarts": consecutive,
                "lastRunAt": ops_mod.iso(ops.now),
            }
            recovered.append({
                "roundId": round_id,
                "mutated": bool(rec.get("mutated")),
                "needsRecovery": needs,
                "consecutiveRestarts": consecutive,
                "errors": rec.get("errors") or [],
            })

        if not dry_run:
            ops_mod.atomic_write(state_path, wd_state)

        status = "dry_run" if dry_run else ("circuit_open" if circuit_open and not recovered else "ok")
        result = {
            "status": status,
            "activeRounds": active_rounds,
            "inspected": inspected,
            "recovered": recovered,
            "circuitOpen": circuit_open,
        }
        if slot_released is not None:
            result["slotReleased"] = slot_released
        return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default="https://multica.devops.moego.dev")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--ops-script")
    parser.add_argument("--max-no-progress-restarts", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        ops_mod = load_ops_module(args.ops_script)
        ops_mod.validate_id(args.workspace_id, "workspaceId")
        ops_mod.validate_id(args.project_id, "projectId")
        ops_mod.validate_id(args.agent_id, "agentId")
        native = ops_mod.Native(args.server_url, args.workspace_id)
        ops = ops_mod.Ops(native, None, args.state_dir, agent_id=args.agent_id, workspace_id=args.workspace_id)
        result = run_watchdog(
            ops_mod, ops,
            project_id=args.project_id,
            agent_id=args.agent_id,
            max_no_progress_restarts=args.max_no_progress_restarts,
            dry_run=args.dry_run,
        )
        print(json.dumps({"protocolVersion": ops_mod.PROTOCOL_VERSION, **result}, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
