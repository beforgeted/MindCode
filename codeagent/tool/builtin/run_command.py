"""run_command：流式落 artifact + 有界返回。

这是 P1 的核心实现细节。**必须**边读子进程输出边往 artifact 写，
不能先在内存里攒完整个 buffer 再交给 Normalizer —— 否则"单条 300K token
输出"在进 Normalizer 之前就已经把内存吃掉了。

内存里只保留 head/tail 两段有界预览，完整输出在 artifact 里，
需要时用 read_artifact 回读（JIT 取证）。

安全说明：这个工具执行的是模型给出的任意 shell 命令，是整个 Agent 最大的
风险面。当前只挡掉了几个明显灾难性的命令，**没有**权限确认机制 ——
真正的 gating（用户确认 / 命令白名单 / 沙箱）属于 CLI 层，尚未实现。
在不受信任的环境里跑之前必须先补上。
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

from codeagent.infra.cancellation import CancelledByUser
from codeagent.infra.text import TRUNCATION_MARKER
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult

_CHUNK = 64 * 1024
_PREVIEW_BYTES = 128 * 1024

# 极小的黑名单，不是安全边界，只是防手滑。
_DENY = re.compile(
    r"(?:^|[\s;&|])(?:rm\s+-rf\s+/(?:\s|$)|mkfs|dd\s+if=.*of=/dev/|:\(\)\{.*\};:)",
    re.IGNORECASE,
)


class RunCommandTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.SERIAL

    @property
    def name(self) -> str:
        return "run_command"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "在 workspace 根目录执行 shell 命令（构建、测试、git 等）。"
                "输出完整落盘，返回值只包含有界摘要与 artifact 引用。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "相对 workspace 的子目录，可选"},
                },
                "required": ["command"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult.error(call, "缺少参数 command")
        if _DENY.search(command):
            return ToolResult.error(call, f"命令被拒绝执行（疑似破坏性操作）: {command}")

        try:
            cwd = ctx.workspace.resolve(str(arguments.get("cwd") or "."))
        except PermissionError as exc:
            return ToolResult.error(call, str(exc))

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )

        head = bytearray()
        tail: deque[bytes] = deque()
        tail_bytes = 0
        total = 0
        capped = False

        metadata = {"command": command, "cwd": str(cwd)}
        try:
            async with ctx.artifact_store.open_writer("tool-results", metadata=metadata) as writer:
                assert proc.stdout is not None
                while True:
                    if ctx.cancellation.cancelled:
                        _kill(proc)
                        raise CancelledByUser("run_command cancelled")
                    chunk = await proc.stdout.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total <= ctx.max_output_bytes:
                        await writer.write(chunk)
                    else:
                        capped = True
                    if len(head) < _PREVIEW_BYTES:
                        head.extend(chunk[: _PREVIEW_BYTES - len(head)])
                    tail.append(chunk)
                    tail_bytes += len(chunk)
                    while tail_bytes - len(tail[0]) >= _PREVIEW_BYTES:
                        tail_bytes -= len(tail.popleft())
                artifact = writer.ref
            exit_code = await proc.wait()
        except CancelledByUser:
            _kill(proc)
            raise
        except asyncio.CancelledError:
            _kill(proc)
            raise

        content = _preview(bytes(head), b"".join(tail), total, capped, ctx.max_output_bytes)
        status_line = f"$ {command}\nexitCode: {exit_code}  输出 {total} 字节"
        result_text = f"{status_line}\n{content}" if content else status_line

        factory = ToolResult.ok if exit_code == 0 else ToolResult.error
        return factory(
            call,
            result_text,
            exit_code=exit_code,
            artifact=artifact,
            raw_bytes=total,
            truncated=total > len(head) + len(b"".join(tail)),
            metadata=metadata,
        )


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _preview(head: bytes, tail: bytes, total: int, capped: bool, cap: int) -> str:
    notes = []
    if capped:
        notes.append(f"[输出超过 {cap} 字节上限，artifact 只保存了前 {cap} 字节]")
    if total <= len(head):
        body = head.decode("utf-8", errors="replace")
    else:
        omitted = total - len(head) - len(tail)
        body = (
            head.decode("utf-8", errors="replace")
            + f"\n{TRUNCATION_MARKER.format(omitted=max(0, omitted))}\n"
            + tail.decode("utf-8", errors="replace")
        )
    return ("\n".join(notes) + "\n" + body) if notes else body
