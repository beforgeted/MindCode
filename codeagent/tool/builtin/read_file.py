from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult

_MAX_LINE_CHARS = 2000


class ReadFileTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "读取 workspace 内的文本文件。返回带行号的内容。"
                "大文件请用 offset/limit 分段读，不要一次读完。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 workspace 根目录的路径"},
                    "offset": {"type": "integer", "description": "起始行号（1 起），默认 1"},
                    "limit": {"type": "integer", "description": "最多读取行数，默认 2000"},
                },
                "required": ["path"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult.error(call, "缺少参数 path")
        offset = max(1, int(arguments.get("offset", 1)))
        limit = max(1, int(arguments.get("limit", 2000)))

        try:
            path = ctx.workspace.resolve(raw_path)
        except PermissionError as exc:
            return ToolResult.error(call, str(exc))
        if not path.exists():
            return ToolResult.error(call, f"文件不存在: {raw_path}")
        if path.is_dir():
            return ToolResult.error(call, f"{raw_path} 是目录，不是文件")

        text, total = await asyncio.to_thread(_read_slice, path, offset, limit)
        header = f"{raw_path} (行 {offset}-{min(total, offset + limit - 1)} / 共 {total} 行)"
        return ToolResult.ok(
            call,
            f"{header}\n{text}",
            metadata={"path": str(path), "total_lines": total},
        )


def _read_slice(path: Path, offset: int, limit: int) -> tuple[str, int]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[offset - 1 : offset - 1 + limit]
    numbered = [
        f"{offset + i:>6}\t{line[:_MAX_LINE_CHARS]}" for i, line in enumerate(selected)
    ]
    return "\n".join(numbered), len(lines)
