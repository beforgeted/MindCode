"""read_artifact：Agent 版 JIT Retrieval 的第一环。

Context = Working Set，Storage = Full Evidence。历史里只留有界摘要 +
artifact 引用，模型需要细节时自己回读。这样就不用把所有东西永久塞在
Context Window 里。

后续 P3/P4 会在这条链上再加两级：memory_get(id) 与 evidence_get(event_id)，
形成 Index -> Memory -> Evidence 的渐进披露。
"""

from __future__ import annotations

from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult

_DEFAULT_MAX_BYTES = 200_000


class ReadArtifactTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    @property
    def name(self) -> str:
        return "read_artifact"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "回读被 offload 的完整工具输出。当历史里的工具结果显示"
                "「完整内容: artifact://...」而你需要更多细节时使用。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "形如 artifact://tool-results/art_xxx"},
                    "max_bytes": {"type": "integer", "description": f"默认 {_DEFAULT_MAX_BYTES}"},
                },
                "required": ["uri"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        uri = str(arguments.get("uri", "")).strip()
        if not uri.startswith("artifact://"):
            return ToolResult.error(call, "uri 必须形如 artifact://<kind>/<id>")
        max_bytes = max(1000, int(arguments.get("max_bytes", _DEFAULT_MAX_BYTES)))
        try:
            text = await ctx.artifact_store.load_text(uri, max_bytes=max_bytes)
        except FileNotFoundError as exc:
            return ToolResult.error(call, str(exc))
        return ToolResult.ok(call, f"{uri}\n{text}", metadata={"uri": uri})
