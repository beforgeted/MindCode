from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".pytest_cache", ".ruff_cache", "target", ".idea", ".codeagent",
}
_MAX_FILE_BYTES = 2 * 1024 * 1024


class GrepTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    @property
    def name(self) -> str:
        return "grep"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="在 workspace 内按正则搜索文件内容，返回匹配的文件与行。",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python 正则"},
                    "glob": {"type": "string", "description": "文件名通配，如 *.py"},
                    "path": {"type": "string", "description": "搜索子目录，默认整个 workspace"},
                    "max_results": {"type": "integer", "description": "默认 200"},
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            return ToolResult.error(call, "缺少参数 pattern")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult.error(call, f"正则无效: {exc}")

        try:
            root = ctx.workspace.resolve(str(arguments.get("path") or "."))
        except PermissionError as exc:
            return ToolResult.error(call, str(exc))

        glob = str(arguments.get("glob") or "*")
        max_results = max(1, int(arguments.get("max_results", 200)))

        # 正则匹配是 CPU 活，且要遍历大量文件 —— 必须离开事件循环，
        # 否则会卡住所有并发 AgentRun。
        hits, scanned = await asyncio.to_thread(
            _search, root, ctx.workspace.root, regex, glob, max_results
        )

        if not hits:
            return ToolResult.ok(call, f"没有匹配。已扫描 {scanned} 个文件。")
        body = "\n".join(hits)
        suffix = f"\n[已达上限 {max_results}，结果可能不完整]" if len(hits) >= max_results else ""
        return ToolResult.ok(
            call,
            f"共 {len(hits)} 条匹配（扫描 {scanned} 个文件）:\n{body}{suffix}",
            metadata={"hits": len(hits), "scanned": scanned},
        )


def _search(
    root: Path, workspace_root: Path, regex: re.Pattern[str], glob: str, max_results: int
) -> tuple[list[str], int]:
    hits: list[str] = []
    scanned = 0
    for path in sorted(root.rglob(glob)):
        if len(hits) >= max_results:
            break
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        try:
            rel = path.relative_to(workspace_root)
        except ValueError:
            rel = path
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                if len(hits) >= max_results:
                    break
    return hits, scanned
