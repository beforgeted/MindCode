"""Official community servers in the actual independent-VM execution domain."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.execution.models import ExecutionLimits
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.infra.cancellation import CancelledByUser
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.mcp.community import McpCommunityTool, McpGrant, McpServer
from codeagent.tool.mcp.config import McpConfig
from codeagent.tool.sandbox import SandboxTools
from tests.test_ecosystem import fixture
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_mcp_project import context

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_ECOSYSTEM_IMAGE'),
    reason='requires an independent Ubuntu VM and a pinned community-runtime Podman image',
)


def configured_server(server_id: str, *, tool_name: str | None = None):
    data = fixture()['servers'][server_id]
    catalog = json.loads(Path(data['catalog']).read_text(encoding='utf-8'))
    name = tool_name or data['tool']
    descriptor = next(t for t in catalog['tools'] if t['name'] == name)
    grant = McpGrant(name, json.dumps(descriptor),
                     EffectKind.WORKSPACE_WRITE if name == 'write_file' else EffectKind.READ_ONLY,
                     RetryPolicy.NEVER if name == 'write_file' else RetryPolicy.SAFE)
    server = McpServer(server_id, tuple(data['command']), tuple(data['container_command']),
                       (grant,))
    return server, McpCommunityTool(server, grant)


@pytest.mark.parametrize('server_id', ['time', 'filesystem', 'everything', 'git'])
async def test_official_server_executes_in_container(tmp_path, server_id):
    ctx = replace(context(tmp_path), timeout_seconds=30)
    _server, tool = configured_server(server_id)
    image = os.environ['MINDCODE_ECOSYSTEM_IMAGE']
    manager = PodmanSandboxManager(image, limits=ExecutionLimits(memory_mib=512, pids=64))
    snapshot = TreeSnapshot((SnapshotEntry('calc.py', b'def add(a, b): return a + b\n'),))
    handle = await manager.open(snapshot)
    try:
        executor = SandboxExecutor(manager, handle, ctx.workspace.root)
        ctx = replace(ctx, command_executor=executor)
        arguments = fixture()['servers'][server_id]['arguments']
        arguments = {k: v.replace('{workspace}', '/workspace') if isinstance(v, str) else v
                     for k, v in arguments.items()}
        if server_id == 'git':
            # The host .git is intentionally never mounted. Use an owned temporary repo.
            output = await manager.execute_python(handle,
                'import subprocess; subprocess.run(["git","init","/tmp/mcp-owned-repo"],'
                'check=True, capture_output=True)', b'', cancellation=ctx.cancellation)
            assert output.returncode == 0
            arguments = {'repo_path': '/tmp/mcp-owned-repo'}
        result = await SandboxTools(executor).execute(tool, ctx, arguments)
        assert not result.is_error
        assert fixture()['servers'][server_id]['expected'] in result.content
        if server_id == 'filesystem':
            # Real upstream path permissions plus absence of host/private data.
            for path in (str(tmp_path / 'host-secret'), '/etc/passwd',
                         '/workspace/.private-docs/x'):
                assert (await SandboxTools(executor).execute(tool, ctx, {'path': path})).is_error
        assert await manager.seal(handle) == snapshot
        assert not manager._handles
    finally:
        await manager.aclose()


@pytest.mark.parametrize('accepted', [True, False])
async def test_community_mcp_worker_preserves_publication_gate(tmp_path, accepted):
    _init_repo(tmp_path)
    before = _git_out(tmp_path, 'rev-parse', 'HEAD')
    server, tool = configured_server('filesystem')
    _write_server, write_tool = configured_server('filesystem', tool_name='write_file')
    server = replace(server, grants=(tool.grant, write_tool.grant))
    cfg = replace(_config(tmp_path), execution_backend='podman',
                  sandbox_image=os.environ['MINDCODE_ECOSYSTEM_IMAGE'],
                  mcp=McpConfig(servers=(server,)),
                  verify_command='test -f calc.py' if accepted else 'exit 1')
    cfg = replace(cfg, profile=replace(cfg.profile, master_max_replans=0))
    client = StubLlmClient([
        [(write_tool.name, {'path': '/workspace/calc.py',
                           'content': 'def add(a,b): return a+b\n'})],
        [(tool.name, {'path': '/workspace/calc.py'})], 'done',
    ])
    async with MasterSession(cfg, llm_client=client, planner=StaticPlanner(
        TaskGraph([Step('s', 'default', 'write and inspect code')]),
    )) as session:
        result = await session.run_task('write code and inspect using official MCP')
        blocks = [b for m in client.seen_calls[-1] for b in m.tool_results]
        assert any('def add' in b.content and not b.is_error for b in blocks)
        assert result.accepted is accepted and result.integrated is accepted
        assert (tmp_path / 'calc.py').exists() is accepted
        assert (_git_out(tmp_path, 'rev-parse', 'HEAD') != before) is accepted
        assert _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles


@pytest.mark.parametrize('stop', ['timeout', 'cancel'])
async def test_real_long_running_server_cleanup(tmp_path, stop):
    ctx = replace(context(tmp_path), timeout_seconds=5 if stop == 'timeout' else 30)
    _server, tool = configured_server('everything', tool_name='trigger-long-running-operation')
    manager = PodmanSandboxManager(os.environ['MINDCODE_ECOSYSTEM_IMAGE'])
    handle = await manager.open(TreeSnapshot(()))
    ctx = replace(ctx, command_executor=SandboxExecutor(manager, handle, ctx.workspace.root))
    async def cancel_later():
        await asyncio.sleep(4)
        ctx.cancellation.cancel()
    cancel = asyncio.create_task(cancel_later()) if stop == 'cancel' else None
    try:
        expected = TimeoutError if stop == 'timeout' else CancelledByUser
        with pytest.raises(expected):
            await tool.execute(ctx, {'duration': 30, 'steps': 30})
    finally:
        if cancel:
            cancel.cancel()
            await asyncio.gather(cancel, return_exceptions=True)
        await manager.aclose()
    assert not manager._handles


@pytest.mark.parametrize('kind', ['module', 'script'])
async def test_readonly_server_ignores_workspace_module_shadow(tmp_path, kind):
    ctx = replace(context(tmp_path), timeout_seconds=30)
    _server, tool = configured_server('time')
    if kind == 'script':
        server = replace(_server, container_command=('python3', '/workspace/mcp_server_time.py'))
        tool = McpCommunityTool(server, tool.grant)
    manager = PodmanSandboxManager(os.environ['MINDCODE_ECOSYSTEM_IMAGE'])
    shadow = b'open("/workspace/hijacked", "w").write("shadow executed")\nraise RuntimeError()\n'
    snapshot = TreeSnapshot((SnapshotEntry('mcp_server_time.py', shadow),))
    handle = await manager.open(snapshot)
    try:
        ctx = replace(ctx, command_executor=SandboxExecutor(manager, handle, ctx.workspace.root))
        result = await tool.execute(ctx, {'timezone': 'UTC'})
        assert result.is_error is (kind == 'script')
        assert await manager.seal(handle) == snapshot
    finally:
        await manager.aclose()
