from __future__ import annotations

from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.memory.repository import MemoryRepository
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class MemoryGetTool(BaseTool):
    """Progressive Disclosure（记忆 V2 §19）：从索引项下钻到完整 Memory + evidence。"""

    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    def __init__(self, repository: MemoryRepository, project_id: str) -> None:
        self._repository = repository
        self._project_id = project_id

    @property
    def name(self) -> str:
        return "memory_get"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="按 memory_id 读取完整的长期记忆条目及其 evidence 引用。",
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 ID，如 mem_xxx"},
                },
                "required": ["memory_id"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        memory_id = str(arguments.get("memory_id", "")).strip()
        if not memory_id:
            return ToolResult.error(call, "缺少参数 memory_id")
        try:
            item = await self._repository.get(
                self._project_id, memory_id, include_deleted=True
            )
        except Exception as exc:
            return ToolResult.error(call, f"读取记忆失败: {type(exc).__name__}: {exc}")
        if item is None:
            return ToolResult.error(call, f"记忆不存在: {memory_id}")
        refs = "\n".join(f"- {ref}" for ref in item.evidence_refs) or "- none"
        body = (
            f"{item.id} [{item.type}] status={item.status} source={item.source}\n"
            f"scope={item.scope} confidence={item.confidence} importance={item.importance}\n"
            f"updated={item.updated_at.isoformat()}\n\n"
            f"{item.content}\n\nevidence:\n{refs}"
        )
        return ToolResult.ok(call, body, metadata={"memory_id": item.id})
