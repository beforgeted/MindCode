"""O1 执行轨迹：读取两个权威存储，生成可重建的 JSON 投影。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from codeagent.evidence.event_store import RawEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.trace import current_trace
from codeagent.llm.pricing import usd
from codeagent.orchestration.run_store import RunStore, SqliteRunStore


class TrajectoryExporter(Protocol):
    async def export(
        self, master_run_id: str, *, session_snapshot: dict | None = None,
    ) -> Path: ...


def summarize_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempts": len(calls), "calls": 0, "errors": 0, "cancelled": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "elapsed_ms": 0.0, "unknown_usage_calls": 0,
        "cost": None, "cost_status": "pricing_not_configured",
    }
    for call in calls:
        status = call["status"]
        result[{"success": "calls", "error": "errors", "cancelled": "cancelled"}[status]] += 1
        result["elapsed_ms"] += call["elapsed_ms"]
        usage = call.get("usage")
        if usage is None:
            result["unknown_usage_calls"] += 1
        else:
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                result[key] += usage.get(key, 0)
    priced = [c for c in calls if c.get('cost_pico_usd') is not None]
    configured = any(c.get('cost_status') in ('known', 'unknown') for c in calls)
    if configured:
        known = sum(int(c['cost_pico_usd']) for c in priced)
        result['known_cost_usd'] = usd(known)
        result['unknown_cost_calls'] = len(calls) - len(priced)
        result['cost_status'] = 'known' if len(priced) == len(calls) else 'partial'
        result['cost'] = usd(known) if len(priced) == len(calls) else None
        result['currency'] = 'USD'
    return result


def _group(calls: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        value = call.get(key) if key in ("model", "role") else call.get("trace", {}).get(key)
        if key == "model" and call.get("provider"):
            value = f"{call['provider']}:{value}"
        label = str(value) if value is not None else "unattributed"
        groups.setdefault(label, []).append(call)
    return {label: summarize_calls(items) for label, items in groups.items()}


def _transition_durations(transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in transitions]
    previous: dict[int, dict[str, Any]] = {}
    for row in result:
        row["duration_ms"] = None
        before = previous.get(row["attempt_no"])
        if before is not None:
            before["duration_ms"] = max(0.0, (
                datetime.fromisoformat(row["created_at"])
                - datetime.fromisoformat(before["created_at"])
            ).total_seconds() * 1000)
        previous[row["attempt_no"]] = row
    return result


class JsonTrajectoryExporter:
    def __init__(self, root: Path, store: RunStore, events: RawEventStore) -> None:
        self._root, self._store, self._events = root, store, events

    async def build(self, master_run_id: str) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", master_run_id) is None:
            raise ValueError("无效的 master_run_id")
        record = await self._store.load_run(master_run_id)
        observations = await self._store.load_observations(master_run_id)
        durable = (await self._store.load_costs(master_run_id)
                   if isinstance(self._store, SqliteRunStore) else [])
        origin = (await self._store.load_cost_origin(master_run_id)
                  if isinstance(self._store, SqliteRunStore) else None)
        await self._events.flush()
        selected: list[AgentEvent] = []
        # resume 可能发生在新 Session，必须跨 Session 关联；不能只看创建 run 的会话。
        for session_id in await self._events.list_session_ids():
            selected.extend(event for event in await self._events.query(session_id)
                            if event.payload.get("trace", {}).get("master_run_id") == master_run_id)
        selected.sort(key=lambda event: (event.created_at, event.event_id))
        calls = [dict(event.payload) for event in selected if event.type == EventType.LLM_CALL]
        tools = [{
            "event_id": e.event_id, "type": e.type, "created_at": e.created_at.isoformat(),
            "agent_run_id": e.agent_run_id, "tool_run_id": e.tool_run_id,
            "trace": e.payload.get("trace", {}),
            "name": e.payload.get("name"), "status": e.payload.get("status"),
            "duration_ms": e.payload.get("duration_ms"),
            "artifact_uri": e.payload.get("artifact_uri"),
        } for e in selected
            if e.tool_run_id is not None and e.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT)]
        workers = [{
            "event_id": e.event_id, "type": e.type, "created_at": e.created_at.isoformat(),
            "agent_run_id": e.agent_run_id, "trace": e.payload.get("trace", {}),
            "status": e.payload.get("status"), "agent": e.payload.get("agent"),
        } for e in selected
            if e.type in (EventType.AGENT_RUN_STARTED, EventType.AGENT_RUN_FINISHED)]
        deferred = []
        if record is not None:
            attempt_numbers = {a.attempt_no for a in record.attempts}
            attempt_numbers.update(s["attempt_no"] for s in observations["steps"])
            for attempt_no in sorted(attempt_numbers or {1}):
                deferred.extend({"attempt_no": attempt_no, **asdict(item)}
                                for item in await self._store.load_deferred(
                                    master_run_id, attempt_no,
                                ))
        return {
            "schema_version": 1, "master_run_id": master_run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": record.status if record else "not_persisted",
            "task": record.task if record else None,
            "promoted_sha": record.promoted_sha if record else None,
            "attempts": [asdict(a) for a in record.attempts] if record else [],
            "transitions": _transition_durations(observations["transitions"]),
            "steps": observations["steps"],
            "legacy_step_outcomes": {k: asdict(v) for k, v in record.outcomes.items()}
                                    if record and not observations["steps"] else {},
            "deferred_actions": deferred,
            "llm_calls": calls, "tools": tools, "worker_events": workers,
            "durable_cost": {
                'known_cost_usd': usd(sum(int(row['pico_usd']) for row in durable
                                          if row['pico_usd'] is not None)),
                'unknown_cost_calls': sum(row['pico_usd'] is None for row in durable)
                                      + int(origin is False),
                'attempts': len(durable), 'source': 'RunStore',
                'prior_coverage': origin,
            },
            "budget_routes": [dict(e.payload) for e in selected
                              if e.type == EventType.MODEL_BUDGET_ROUTE],
            "capability_routes": [dict(e.payload) for e in selected
                                  if e.type == EventType.MODEL_CAPABILITY_ROUTE],
            "model_fallbacks": [dict(e.payload) for e in selected
                                if e.type == EventType.MODEL_FALLBACK],
            "totals": summarize_calls(calls),
            "by_model": _group(calls, "model"), "by_role": _group(calls, "role"),
            "by_attempt": _group(calls, "attempt_no"), "by_step": _group(calls, "step_id"),
            "by_worker": _group(calls, "agent_run_id"),
            "by_invocation": _group(calls, "invocation_id"),
            "coverage": {
                "run_persisted": record is not None,
                "attempt_history_available": bool(observations["transitions"]),
                "llm_events_available": bool(calls),
                "note": "只汇总已落盘且带关联 ID 的观测；旧记录和崩溃时未落盘事件无法补回。"
                        "规划调用可能没有 Attempt；末尾状态耗时未知；各调用耗时之和不是墙钟耗时。",
            },
        }

    async def export(
        self, master_run_id: str, *, session_snapshot: dict | None = None,
    ) -> Path:
        report = await self.build(master_run_id)
        if session_snapshot is not None:
            report["session_cumulative_snapshot"] = session_snapshot
            report["snapshot_session_id"] = current_trace().get("session_id")
            report["export_invocation_id"] = current_trace().get("invocation_id")
        path = self._root / "metrics" / f"{master_run_id}.json"
        await asyncio.to_thread(_write_json, path, report)
        return path


def _write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_trajectory(report: dict[str, Any]) -> str:
    total = report["totals"]
    lines = [f"任务 {report['master_run_id']} | {report['status']}",
             f"LLM 成功 {total['calls']} / 失败 {total['errors']} / 取消 {total['cancelled']}",
             f"Token: 输入 {total['input_tokens']} / 输出 {total['output_tokens']} / "
             f"缓存读 {total['cache_read_tokens']} / 缓存写 {total['cache_write_tokens']}",
             (f"金额成本 USD：{total['cost']}" if total['cost_status'] == 'known' else
              f"已知成本 USD：{total['known_cost_usd']}；"
              f"未知调用 {total['unknown_cost_calls']}" if total['cost_status'] == 'partial' else
              "金额成本：未配置定价"), "角色用量："]
    for role, values in report["by_role"].items():
        lines.append(f"  {role}: {values['calls']} 次成功，"
                     f"输入 {values['input_tokens']} / 输出 {values['output_tokens']}")
    lines.append(report["coverage"]["note"])
    return "\n".join(lines)
