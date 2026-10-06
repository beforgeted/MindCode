from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from codeagent.infra.cancellation import CancelledByUser
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.executor import SandboxExecutor, _kill_tree, _new_group_kwargs, filtered_env
from codeagent.tool.mcp import bridge, project_server
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class McpProjectTool(BaseTool):
    # Processes/sessions belong to each call; this descriptor has no mutable run state.
    concurrency_mode = ToolConcurrencyMode.SERIAL

    def __init__(self, remote_name: str):
        descriptor = next((t for t in project_server.CATALOG if t['name'] == remote_name), None)
        if descriptor is None:
            raise ValueError('unsupported project MCP tool')
        self.remote_name = remote_name
        self._descriptor = descriptor
        # Capture controller-owned helpers at registration, before model workspace writes.
        self._server_source = Path(project_server.__file__).read_text(encoding='utf-8')
        self._bridge_source = Path(bridge.__file__).read_text(encoding='utf-8')

    @property
    def name(self) -> str:
        return 'mcp_project_' + self.remote_name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self._descriptor['description'], self._descriptor['inputSchema'])

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        ctx.cancellation.raise_if_cancelled()
        try:
            project_server.validate_arguments(self.remote_name, arguments)
        except ValueError as exc:
            return ToolResult.error(call, str(exc))
        payload = json.dumps({
            'source': self._server_source,
            'catalog': project_server.CATALOG, 'tool': self.remote_name, 'arguments': arguments,
            'max_response_bytes': min(ctx.max_output_bytes, 1024 * 1024),
            'timeout_seconds': ctx.timeout_seconds,
        }).encode()
        source = self._bridge_source
        executor = ctx.command_executor
        # Envelope escaping may expand each Unicode codepoint; raw content has its own cap.
        envelope_limit = 5 * 1024 * 1024
        if isinstance(executor, SandboxExecutor):
            if ctx.workspace.root != executor.root:
                return ToolResult.error(call, 'MCP execution domain mismatch')
            output = await executor.manager.execute_python(
                executor.handle, source, payload, cancellation=ctx.cancellation,
                max_output_bytes=envelope_limit,
            )
            raw, returncode = output.stdout, output.returncode
        else:
            raw, returncode = await _local_exchange(ctx, source, payload, envelope_limit)
        result = bridge.load_json(raw)
        if returncode or 'bridge_error' in result:
            kind = result.get('bridge_error', 'ProtocolError')
            if kind == 'TimeoutError':
                raise TimeoutError('MCP request deadline')
            return ToolResult.error(call, f'MCP service failed: {kind}')
        body = result['content'][0]['text']
        status = ToolResult.error if result.get('isError') else ToolResult.ok
        return status(call, body, raw_bytes=len(body.encode('utf-8')),
                      metadata={'mcp_server': 'mindcode-project', 'mcp_tool': self.remote_name})


async def _local_exchange(ctx, source, payload, limit):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-I', '-u', '-c', source, cwd=ctx.workspace.root,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=filtered_env(), **_new_group_kwargs(),
    )
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout

    async def exchange():
        stdin.write(payload)
        await stdin.drain()
        stdin.close()
        data = bytearray()
        while chunk := await stdout.read(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError('MCP bridge output exceeds limit')
        return bytes(data), await proc.wait()

    task = asyncio.create_task(exchange())
    cancel = asyncio.create_task(ctx.cancellation.wait())
    try:
        async with asyncio.timeout(ctx.timeout_seconds):
            done, _ = await asyncio.wait((task, cancel), return_when=asyncio.FIRST_COMPLETED)
            if cancel in done:
                raise CancelledByUser('MCP cancelled')
            return await task
    finally:
        _kill_tree(proc)
        task.cancel()
        cancel.cancel()
        await asyncio.gather(task, cancel, return_exceptions=True)
        await proc.wait()
