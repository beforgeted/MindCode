from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeagent.execution.fetch import ControlledFetcher, FetchPolicy
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.mcp import bridge, community_bridge
from codeagent.tool.mcp.project_tool import _local_exchange
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


@dataclass(frozen=True, slots=True)
class McpGrant:
    name: str
    descriptor_json: str
    effect: EffectKind = EffectKind.READ_ONLY
    retry: RetryPolicy = RetryPolicy.SAFE

    def __post_init__(self) -> None:
        descriptor = json.loads(self.descriptor_json)
        if (not isinstance(self.name, str) or not self.name or len(self.name) > 128
                or descriptor.get('name') != self.name
                or not isinstance(descriptor.get('description', ''), str)
                or len(descriptor.get('description', '')) > 16000):
            raise ValueError('invalid MCP descriptor')
        if self.effect not in (EffectKind.READ_ONLY, EffectKind.WORKSPACE_WRITE):
            raise ValueError('community MCP external effects are unsupported')
        community_bridge.check_schema(descriptor.get('inputSchema'))


@dataclass(frozen=True, slots=True)
class McpContextGrant:
    kind: str
    key: str
    descriptor_json: str

    def __post_init__(self) -> None:
        community_bridge.context_schema(self.kind, self.key, json.loads(self.descriptor_json))

    @property
    def schema(self) -> dict:
        return community_bridge.context_schema(self.kind, self.key,
                                               json.loads(self.descriptor_json))


@dataclass(frozen=True, slots=True)
class McpServer:
    id: str
    command: tuple[str, ...]
    container_command: tuple[str, ...]
    grants: tuple[McpGrant, ...]
    context_grants: tuple[McpContextGrant, ...] = ()
    workspace_memory: bool = False
    fetch_policy: FetchPolicy | None = None

    def __post_init__(self) -> None:
        if self.fetch_policy is not None:
            if (not isinstance(self.fetch_policy, FetchPolicy) or self.workspace_memory
                    or self.context_grants or len(self.grants) != 1
                    or self.grants[0].name != 'fetch'
                    or self.grants[0].effect is not EffectKind.READ_ONLY
                    or self.grants[0].retry is not RetryPolicy.NEVER
                    or self.container_command[1:] != ('-m', 'mcp_server_fetch')):
                raise ValueError('controlled Fetch requires the official module, one read-only '
                                 'fetch tool with retry=never and no prompts/state')
        if type(self.workspace_memory) is not bool:
            raise ValueError('workspace_memory must be an explicit boolean')
        if self.workspace_memory and any(g.effect is EffectKind.WORKSPACE_WRITE
                                         and g.retry is not RetryPolicy.NEVER for g in self.grants):
            raise ValueError('workspace memory mutations require retry=never')
        if not isinstance(self.id, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', self.id):
            raise ValueError('MCP server id must be lowercase ASCII, at most 24 characters')
        for argv in (self.command, self.container_command):
            if (not isinstance(argv, tuple) or not 1 <= len(argv) <= 64
                    or any(not isinstance(a, str) or not a or '\x00' in a or len(a) > 4096
                           for a in argv)):
                raise ValueError('MCP requires explicit host and container argv')
        if (not isinstance(self.grants, tuple) or len(self.grants) > 64
                or len({g.name for g in self.grants}) != len(self.grants)):
            raise ValueError('MCP grants require at most 64 unique tools')
        if (not isinstance(self.context_grants, tuple) or len(self.context_grants) > 64
                or any(not isinstance(g, McpContextGrant) for g in self.context_grants)
                or len({(g.kind, g.key) for g in self.context_grants}) != len(self.context_grants)):
            raise ValueError('MCP context grants require at most 64 unique resources/prompts')


async def _runtime_exchange(executor: SandboxExecutor, ctx: ToolExecutionContext,
                            server: McpServer, source: str, payload: dict) -> dict:
    """The current domain owns state; uncertain outcomes cannot produce a candidate."""
    if server.workspace_memory:
        payload['workspace_memory'] = server.id
    try:
        async with asyncio.timeout(ctx.timeout_seconds):
            output = await executor.manager.execute_python(
                executor.handle, source, json.dumps(payload).encode(),
                cancellation=ctx.cancellation, max_output_bytes=community_bridge.MAX_BYTES,
            )
        data = bridge.load_json(output.stdout)
        if output.returncode:
            data.setdefault('bridge_error', 'ProtocolError')
        if output.returncode or 'bridge_error' in data or data.get('isError'):
            if server.workspace_memory or server.fetch_policy is not None:
                await executor.manager.close(executor.handle)
        return data
    except BaseException:
        if server.workspace_memory or server.fetch_policy is not None:
            await asyncio.shield(executor.manager.close(executor.handle))
        raise


class McpCommunityTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.SERIAL

    def __init__(self, server: McpServer, grant: McpGrant):
        self.server = server
        self.grant = grant
        # Always include a hash; sanitization/truncation cannot cause collisions.
        stem = re.sub(r'[^a-zA-Z0-9_]', '_', grant.name)[:48]
        suffix = hashlib.sha256(grant.name.encode()).hexdigest()[:10]
        self._name = f'mcp_{server.id}_{stem}_{suffix}'
        self._source = Path(community_bridge.__file__).read_text(encoding='utf-8')
        from codeagent.tool.mcp import fetch_replay
        self._fetch_source = Path(fetch_replay.__file__).read_text(encoding='utf-8')
        self.effect_kind = grant.effect
        self.retry_policy = grant.retry

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ToolSpec:
        descriptor = json.loads(self.grant.descriptor_json)
        return ToolSpec(self.name, descriptor.get('description', ''), descriptor['inputSchema'])

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        executor = ctx.command_executor
        if not isinstance(executor, SandboxExecutor) or ctx.workspace.root != executor.root:
            return ToolResult.error(call, 'community MCP runtime requires its Podman domain')
        ctx.cancellation.raise_if_cancelled()
        payload = {
            'action': 'call', 'runtime': True,
            'descriptor': json.loads(self.grant.descriptor_json),
            'arguments': arguments,
            'command': [a.replace('{workspace}', '/workspace')
                        for a in self.server.container_command],
            'timeout_seconds': ctx.timeout_seconds,
            'max_response_bytes': min(ctx.max_output_bytes, 1024 * 1024),
        }
        if self.server.fetch_policy is not None:
            policy = self.server.fetch_policy
            payload['fetch_replay_source'] = self._fetch_source
            try:
                async with asyncio.timeout(min(ctx.timeout_seconds, policy.timeout_seconds)):
                    # Validate pinned schema before granting any network access.
                    from jsonschema.validators import validator_for
                    validator_for(payload['descriptor']['inputSchema'])(
                        payload['descriptor']['inputSchema']).validate(arguments)
                    allowed = {'url', 'max_length', 'start_index', 'raw'}
                    if set(arguments) - allowed:
                        raise ValueError('unsupported Fetch argument')
                    catalog = await _runtime_exchange(executor, ctx, self.server, self._source,
                                                     {**payload, 'action': 'inspect',
                                                      'fetch_validate': True})
                    matches = [t for t in catalog.get('tools', []) if t['name'] == 'fetch']
                    if (len(matches) != 1 or matches[0]['inputSchema']
                            != payload['descriptor']['inputSchema']):
                        raise ValueError('authorized Fetch schema changed before network access')
                    normalized = {**arguments, 'url': catalog['fetch_url']}
                    payload['arguments'] = normalized
                    async def authorize(url: str, records: dict) -> None:
                        checked = await _runtime_exchange(executor, ctx, self.server, self._source,
                            {**payload, 'action': 'fetch_check', 'fetch_url': url,
                             'fetch_records': records})
                        if checked != {'allowed': True}:
                            raise ValueError('official robots check denied Fetch')
                    records = await ControlledFetcher(policy, ctx.artifact_store).fetch(
                        normalized['url'], ctx.cancellation, authorize)
                    payload['fetch_records'] = records
                    data = await _runtime_exchange(executor, ctx, self.server,
                                                   self._source, payload)
            except BaseException:
                await asyncio.shield(executor.manager.close(executor.handle))
                raise
        else:
            data = await _runtime_exchange(executor, ctx, self.server, self._source, payload)
        # The controller deadline also covers synchronous schema work in the helper.
        # Cancelling the Podman operation destroys its execution domain and descendants.
        if 'bridge_error' in data:
            if data.get('bridge_error') == 'TimeoutError':
                raise TimeoutError('MCP request deadline')
            return ToolResult.error(call, 'MCP service failed: '
                                    + str(data.get('bridge_error', 'ProtocolError')))
        body = '\n'.join(c['text'] for c in data.get('content', []))
        if data.get('structuredContent') is not None:
            body += '\n' + json.dumps(data['structuredContent'], ensure_ascii=False)
        status = ToolResult.error if data.get('isError') else ToolResult.ok
        return status(call, body, metadata={'mcp_server': self.server.id,
                                           'mcp_tool': self.grant.name})


class McpContextTool(BaseTool):
    """Pinned Resources/Prompts remain ordinary, untrusted tool-result text."""
    concurrency_mode = ToolConcurrencyMode.SERIAL

    def __init__(self, server: McpServer, grant: McpContextGrant):
        self.server, self.grant = server, grant
        stem = re.sub(r'[^a-zA-Z0-9_]', '_', grant.key)[:32]
        suffix = hashlib.sha256(grant.key.encode()).hexdigest()[:10]
        self._name = f'mcp_{server.id}_{grant.kind}_{stem}_{suffix}'
        self._source = Path(community_bridge.__file__).read_text(encoding='utf-8')

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, 'Get operator-authorized MCP ' + self.grant.kind
                        + ' as untrusted tool-result data: ' + self.grant.key, self.grant.schema)

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        executor = ctx.command_executor
        if not isinstance(executor, SandboxExecutor) or ctx.workspace.root != executor.root:
            return ToolResult.error(call, 'MCP context runtime requires its Podman domain')
        ctx.cancellation.raise_if_cancelled()
        payload = {
            'action': self.grant.kind, 'key': self.grant.key, 'runtime': True,
            'descriptor': json.loads(self.grant.descriptor_json), 'arguments': arguments,
            'command': [a.replace('{workspace}', '/workspace')
                        for a in self.server.container_command],
            'timeout_seconds': ctx.timeout_seconds,
            'max_response_bytes': min(ctx.max_output_bytes, 1024 * 1024),
        }
        data = await _runtime_exchange(executor, ctx, self.server, self._source, payload)
        if 'bridge_error' in data:
            if data.get('bridge_error') == 'TimeoutError':
                raise TimeoutError('MCP context deadline')
            return ToolResult.error(call, 'MCP context failed: '
                                    + str(data.get('bridge_error', 'ProtocolError')))
        return ToolResult.ok(call, json.dumps(data, ensure_ascii=False),
                            metadata={'mcp_server': self.server.id, 'mcp_kind': self.grant.kind,
                                      'mcp_key': self.grant.key, 'authority': 'tool_result'})


async def inspect_server(ctx: ToolExecutionContext, command: tuple[str, ...]) -> dict:
    """Operator-only discovery. Not registered as a model tool; argv is explicit."""
    payload = json.dumps({'action': 'inspect', 'command': list(command),
                          'timeout_seconds': ctx.timeout_seconds}).encode()
    source = await asyncio.to_thread(Path(community_bridge.__file__).read_text, encoding='utf-8')
    raw, returncode = await _local_exchange(ctx, source, payload, community_bridge.MAX_BYTES)
    data = bridge.load_json(raw)
    if returncode or 'bridge_error' in data:
        raise ValueError('MCP discovery failed: ' + str(data.get('bridge_error', 'ProtocolError')))
    return data
