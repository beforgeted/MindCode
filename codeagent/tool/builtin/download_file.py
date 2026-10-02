"""Explicit, bounded network read into the active sandbox, never host shell/paths."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from codeagent.execution.snapshot import _parts
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class DownloadFileTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.SERIAL
    effect_kind = EffectKind.WORKSPACE_WRITE
    retry_policy = RetryPolicy.SAFE

    @property
    def name(self) -> str:
        return 'download_file'

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                '下载操作者白名单内的HTTPS文件到当前沙箱相对路径。必须给出可靠的SHA256；'
                '交互需审批，非交互默认拒绝。只支持443、无查询/凭据URL，不能覆盖不同内容已有文件。'
                '下载成功仅表示容器内数据已验证，项目发布仍需独立验收；此工具不安装或执行文件。'
            ),
            input_schema={'type': 'object', 'properties': {
                'url': {'type': 'string'}, 'sha256': {'type': 'string'},
                'path': {'type': 'string', 'description': '容器内规范的相对文件路径'},
            }, 'required': ['url', 'sha256', 'path'], 'additionalProperties': False},
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        if not isinstance(ctx.command_executor, SandboxExecutor) or ctx.downloader is None:
            return ToolResult.error(call, '受控下载需要当前Podman执行域和控制面下载配置')
        if set(arguments) != {'url', 'sha256', 'path'} or not all(
            isinstance(arguments[k], str) for k in arguments
        ):
            return ToolResult.error(call, 'download_file仅接受字符串url、sha256、path')
        path = arguments['path']
        _parts(path)
        executor = ctx.command_executor
        maximum = min(
            ctx.downloader.policy.max_bytes, executor.manager.snapshot_limits.max_file_bytes,
        )
        # Verify the active capability before authorizing any outgoing request.
        executor.manager._lock(executor.handle)
        executor.manager._active(executor.handle)
        data = await ctx.downloader.fetch(
            arguments['url'], arguments['sha256'], ctx.cancellation,
            session_id=ctx.session_id, agent_run_id=ctx.agent_run_id, tool_run_id=ctx.tool_run_id,
            max_bytes=maximum,
        )
        ctx.cancellation.raise_if_cancelled()
        source = Path(__file__).parents[1].joinpath('sandbox_download_helper.py').read_text(
            encoding='utf-8',
        )
        output = await executor.manager.execute_python(
            executor.handle, source, json.dumps({
                'path': path, 'data': base64.b64encode(data).decode('ascii'),
                'sha256': arguments['sha256'], 'max_bytes': maximum,
            }).encode(), cancellation=ctx.cancellation,
        )
        if output.returncode:
            return ToolResult.error(call, '下载数据未写入：目标路径不可用或内容冲突')
        return ToolResult.ok(
            call, f'已校验并写入当前沙箱 {path}（{len(data)}字节，SHA256={arguments["sha256"]}）；'
            '项目发布仍取决于独立验收。',
            metadata={'path': path, 'bytes': len(data), 'sha256': arguments['sha256']},
        )
