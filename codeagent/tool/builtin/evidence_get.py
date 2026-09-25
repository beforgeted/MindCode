from __future__ import annotations

from typing import Any

from codeagent.evidence.event_store import RawEventStore
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class EvidenceGetTool(BaseTool):
    """Progressive Disclosure（记忆 V2 §19）：按 event_id 回读原始事件证据。"""

    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    def __init__(self, event_store: RawEventStore) -> None:
        self._events = event_store

    @property
    def name(self) -> str:
        return "evidence_get"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="按 event_id 回读 RawEventStore 里的原始事件（记忆/工具结果的证据源）。",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "事件 ID"},
                    "session_id": {
                        "type": "string",
                        "description": "事件所属 session，缺省用当前 session",
                    },
                },
                "required": ["event_id"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        event_id = str(arguments.get("event_id", "")).strip()
        if not event_id:
            return ToolResult.error(call, "缺少参数 event_id")
        session_id = str(arguments.get("session_id", "")).strip() or ctx.session_id
        try:
            events = await self._events.query(session_id)
        except Exception as exc:
            return ToolResult.error(call, f"回读事件失败: {type(exc).__name__}: {exc}")
        for event in events:
            if event.event_id == event_id:
                payload = event.payload
                text = payload.get("text") or payload.get("content_preview") or str(payload)
                body = (
                    f"{event.event_id} [{event.type}] session={event.session_id}\n"
                    f"created={event.created_at.isoformat()}\n\n{text}"
                )
                return ToolResult.ok(call, body, metadata={"event_id": event_id})
        return ToolResult.error(call, f"事件不存在: {event_id}（session={session_id}）")
