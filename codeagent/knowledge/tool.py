from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from codeagent.execution.process import run_bounded
from codeagent.knowledge import index
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.executor import LocalExecutor, SandboxExecutor
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class KnowledgeTool(BaseTool):
    # Prevents writes in the same batch from interleaving with versioned reads.
    concurrency_mode = ToolConcurrencyMode.SERIAL

    def __init__(self, action: str):
        if action not in ('search', 'get'):
            raise ValueError('unsupported knowledge action')
        self.action = action
        self._source = Path(index.__file__).read_text(encoding='utf-8')

    @property
    def name(self) -> str:
        return 'knowledge_' + self.action

    @property
    def spec(self) -> ToolSpec:
        if self.action == 'search':
            properties = {'query': {'type': 'string', 'minLength': 1, 'maxLength': 256},
                          'kind': {'type': 'string', 'enum': ['all', 'path', 'symbol', 'document']},
                          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                          'expected_version': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}
            required = ['query']
            description = '查询当前项目路径、Python符号与文档段落；返回版本摘要和来源行号。'
        else:
            properties = {'path': {'type': 'string'},
                          'start_line': {'type': 'integer', 'minimum': 1},
                          'end_line': {'type': 'integer', 'minimum': 1},
                          'file_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                          'index_version': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}
            required = list(properties)
            description = '按Knowledge引用读取当前源码；版本、内容摘要或行号失效则拒绝。'
        return ToolSpec(self.name, description, {
            'type': 'object', 'properties': properties,
            'required': required, 'additionalProperties': False,
        })

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        ctx.cancellation.raise_if_cancelled()
        try:
            index.validate_arguments(self.action, arguments)
        except ValueError as exc:
            return ToolResult.error(call, str(exc))
        executor = ctx.command_executor
        if (not isinstance(executor, (LocalExecutor, SandboxExecutor))
                or type(executor) not in (LocalExecutor, SandboxExecutor)):
            return ToolResult.error(call, 'Knowledge requires a supported execution domain')
        if isinstance(executor, SandboxExecutor) and executor.root != ctx.workspace.root:
            return ToolResult.error(call, 'Knowledge execution domain mismatch')
        state = executor.knowledge
        async with asyncio.timeout(ctx.timeout_seconds), state.lock:
            scope = str(ctx.workspace.root)
            cache = state.cache if state.cache.get('scope') == scope else {}
            payload = json.dumps({'action': self.action, 'arguments': arguments,
                                  'cache': cache}, ensure_ascii=False).encode()
            if len(payload) > index.MAX_ENVELOPE:
                state.cache = {}
                return ToolResult.error(call, 'Knowledge cache exceeds request limit')
            if isinstance(executor, SandboxExecutor):
                output = await executor.manager.execute_python(
                    executor.handle, self._source, payload, cancellation=ctx.cancellation,
                    max_output_bytes=index.MAX_ENVELOPE,
                )
            else:
                output = await run_bounded(
                    [sys.executable, '-I', '-c', self._source, str(ctx.workspace.root)],
                    data=payload, timeout_seconds=ctx.timeout_seconds,
                    cancellation=ctx.cancellation, max_bytes=index.MAX_ENVELOPE,
                )
            try:
                data = json.loads(output.stdout)
                if output.returncode or 'error' in data:
                    state.cache = {}
                    if data.get('error') == 'StaleIndex':
                        return ToolResult.error(call, 'Knowledge引用已失效，请重新搜索')
                    return ToolResult.error(call, 'Knowledge index unavailable; use read_file/grep')
                body = json.dumps({k: data[k] for k in ('index_version', 'result', 'stats')},
                                  ensure_ascii=False)
                state.cache = {**data['cache'], 'scope': scope}
            except (ValueError, KeyError, TypeError):
                state.cache = {}
                return ToolResult.error(call, 'Knowledge index protocol rejected')
            if len(body.encode()) > min(ctx.max_output_bytes, 128 * 1024):
                return ToolResult.error(call, 'Knowledge result exceeds limit; narrow query')
            return ToolResult.ok(call, body, raw_bytes=len(body.encode()),
                                 metadata={'knowledge': self.action,
                                           'index_version': data['index_version']})
