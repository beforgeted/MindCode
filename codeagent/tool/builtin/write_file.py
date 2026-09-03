from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class WriteFileTool(BaseTool):
    """EXCLUSIVE_RESOURCE 的示范。

    `resource_keys()` 返回被写的文件路径，ToolExecutionManager 会按排序后的
    key 加锁，因此同一批里两个写同一文件的调用不会并发。
    """

    concurrency_mode = ToolConcurrencyMode.EXCLUSIVE_RESOURCE

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="写入（覆盖）workspace 内的文本文件。父目录不存在会自动创建。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        )

    def resource_keys(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        path = str(arguments.get("path", "")).strip()
        return (f"file:{path}",) if path else ()

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult.error(call, "缺少参数 path")
        content = arguments.get("content")
        if content is None:
            return ToolResult.error(call, "缺少参数 content")

        try:
            path = ctx.workspace.resolve(raw_path)
        except PermissionError as exc:
            return ToolResult.error(call, str(exc))

        existed = path.exists()
        await asyncio.to_thread(_write, path, str(content))
        action = "覆盖" if existed else "新建"
        return ToolResult.ok(
            call,
            f"{action} {raw_path}（{len(str(content))} 字符）",
            metadata={"path": str(path), "existed": existed},
        )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
