"""Real official Memory: cross-call files, isolated Workers and publication gates."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from codeagent.execution.models import SandboxError
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.infra.cancellation import CancelledByUser
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.mcp.community import McpCommunityTool
from codeagent.tool.mcp.config import McpConfig
from codeagent.tool.sandbox import SandboxTools
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_mcp_memory import memory_server
from tests.test_mcp_project import context

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_ECOSYSTEM_IMAGE'),
    reason='requires independent Ubuntu VM and a pinned official Memory runtime image',
)

STATE = 'mcp-state/memory/memory.jsonl'


def entities(name):
    return {'entities': [{'name': name, 'entityType': 'synthetic', 'observations': ['initial']}]}


async def invoke(manager, handle, ctx, server, name, arguments):
    executor = SandboxExecutor(manager, handle, ctx.workspace.root)
    ctx = replace(ctx, command_executor=executor, timeout_seconds=30)
    tool = McpCommunityTool(server, next(g for g in server.grants if g.name == name))
    return await SandboxTools(executor).execute(tool, ctx, arguments)


async def assert_no_service_process(manager, handle, ctx):
    probe = ('import pathlib; print([p.name for p in pathlib.Path("/proc").iterdir() '
             'if p.name.isdigit() and (p/"comm").exists() '
             'and (p/"comm").read_text().strip()=="node"])')
    output = await manager.execute_python(handle, probe, b'', cancellation=ctx.cancellation)
    assert output.returncode == 0 and output.stdout.strip() == b'[]'


async def test_real_memory_cross_call_state_and_session_close(tmp_path):
    server = memory_server('create_entities', 'add_observations', 'read_graph')
    ctx = context(tmp_path)
    manager = PodmanSandboxManager(os.environ['MINDCODE_ECOSYSTEM_IMAGE'])
    try:
        handle = await manager.open(TreeSnapshot(()))
        assert not (await invoke(manager, handle, ctx, server, 'create_entities',
                                 entities('alpha'))).is_error
        await assert_no_service_process(manager, handle, ctx)
        assert not (await invoke(manager, handle, ctx, server, 'add_observations',
                                 {'observations': [{'entityName': 'alpha',
                                                    'contents': ['second-call']}]})).is_error
        result = await invoke(manager, handle, ctx, server, 'read_graph', {})
        assert not result.is_error and 'second-call' in result.content
        await assert_no_service_process(manager, handle, ctx)
        snapshot = await manager.seal(handle)
        assert len(snapshot.entries) == 1 and snapshot.entries[0].path == STATE
        assert 'second-call' in snapshot.entries[0].data.decode()
        # A fresh domain sees persisted candidate bytes, never a reused process.
        restored = await manager.open(snapshot)
        result = await invoke(manager, restored, ctx, server, 'read_graph', {})
        assert not result.is_error and 'second-call' in result.content
        assert await manager.seal(restored) == snapshot
    finally:
        await manager.aclose()
    assert not manager._handles and not (tmp_path / STATE).exists()


async def test_real_memory_two_workers_and_serial_mutations(tmp_path):
    server = memory_server('create_entities', 'read_graph')
    ctx = context(tmp_path)
    manager = PodmanSandboxManager(os.environ['MINDCODE_ECOSYSTEM_IMAGE'])
    try:
        handles = [await manager.open(TreeSnapshot(())) for _ in range(2)]
        results = await asyncio.gather(
            invoke(manager, handles[0], ctx, server, 'create_entities', entities('worker-a')),
            invoke(manager, handles[0], ctx, server, 'create_entities', entities('worker-a2')),
            invoke(manager, handles[1], ctx, server, 'create_entities', entities('worker-b')),
        )
        assert all(not r.is_error for r in results)
        first = await invoke(manager, handles[0], ctx, server, 'read_graph', {})
        second = await invoke(manager, handles[1], ctx, server, 'read_graph', {})
        assert 'worker-a' in first.content and 'worker-a2' in first.content
        assert 'worker-b' not in first.content and 'worker-a' not in second.content
        assert 'worker-b' in second.content
        for handle in handles:
            await assert_no_service_process(manager, handle, ctx)
            await manager.seal(handle)
    finally:
        await manager.aclose()
    assert not manager._handles


@pytest.mark.parametrize('fault', ['corrupt', 'oversize', 'output_after_write', 'crash', 'link'])
async def test_memory_uncertain_outcome_discards_entire_domain(tmp_path, fault):
    server = memory_server('read_graph', 'create_entities')
    ctx = context(tmp_path)
    data = b'not valid JSON' if fault == 'corrupt' else b'x' * (1024 * 1024 + 1)
    snapshot = (TreeSnapshot((SnapshotEntry(STATE, data),))
                if fault in ('corrupt', 'oversize') else TreeSnapshot(()))
    manager = PodmanSandboxManager(os.environ['MINDCODE_ECOSYSTEM_IMAGE'])
    handle = await manager.open(snapshot)
    executor = SandboxExecutor(manager, handle, ctx.workspace.root)
    ctx = replace(ctx, command_executor=executor, timeout_seconds=30)
    if fault == 'link':
        output = await manager.execute_python(handle,
            'import os; os.symlink("/tmp", "/workspace/mcp-state")', b'',
            cancellation=ctx.cancellation)
        assert output.returncode == 0
    if fault == 'crash':
        # Artificial failure only: production positives always use official Memory.
        server = replace(server, container_command=('python3', '-I', '-c',
            'import os; os._exit(9)'))
    name = 'create_entities' if fault == 'output_after_write' else 'read_graph'
    tool = McpCommunityTool(server, next(g for g in server.grants if g.name == name))
    if fault == 'output_after_write':
        ctx = replace(ctx, max_output_bytes=32)
    try:
        result = await tool.execute(ctx, entities('uncertain') if name == 'create_entities' else {})
        assert result.is_error
        assert not manager._handles
        with pytest.raises(SandboxError):
            await manager.seal(handle)
        assert not (tmp_path / STATE).exists()
    finally:
        await manager.aclose()


@pytest.mark.parametrize('accepted', [True, False])
async def test_real_memory_worker_state_requires_independent_acceptance(tmp_path, accepted):
    _init_repo(tmp_path)
    before = _git_out(tmp_path, 'rev-parse', 'HEAD')
    server = memory_server('create_entities', 'read_graph')
    tools = {g.name: McpCommunityTool(server, g).name for g in server.grants}
    cfg = replace(_config(tmp_path), execution_backend='podman',
                  sandbox_image=os.environ['MINDCODE_ECOSYSTEM_IMAGE'],
                  mcp=McpConfig(servers=(server,)),
                  verify_command=f'test -s {STATE}' if accepted else 'exit 1')
    client = StubLlmClient([[(tools['create_entities'], entities('candidate'))],
                            [(tools['read_graph'], {})], 'done'])
    async with MasterSession(cfg, llm_client=client, planner=StaticPlanner(
        TaskGraph([Step('s', 'default', 'write and inspect candidate graph')]),
    )) as session:
        result = await session.run_task('record a synthetic entity with official Memory')
        blocks = [b for m in client.seen_calls[-1] for b in m.tool_results]
        assert any('candidate' in b.content and not b.is_error for b in blocks)
        assert result.accepted is accepted and result.integrated is accepted
        assert result.replans == 0
        assert (tmp_path / STATE).exists() is accepted
        assert (_git_out(tmp_path, 'rev-parse', 'HEAD') != before) is accepted
        if accepted:
            graph = json.loads((tmp_path / STATE).read_text())
            assert graph['name'] == 'candidate'
        assert _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles


@pytest.mark.parametrize('stop', ['timeout', 'cancel'])
async def test_real_memory_paused_service_reclaims_domain(tmp_path, stop):
    server = memory_server('read_graph')
    # Deliberate fault injection after loading the real immutable upstream module.
    script = ("await import('/opt/mcp-memory/node_modules/@modelcontextprotocol/"
              "server-memory/dist/index.js'); process.kill(process.pid, 'SIGSTOP');")
    server = replace(server, container_command=('node', '--input-type=module', '-e', script))
    manager = PodmanSandboxManager(os.environ['MINDCODE_ECOSYSTEM_IMAGE'])
    handle = await manager.open(TreeSnapshot(()))
    ctx = replace(context(tmp_path), timeout_seconds=5 if stop == 'timeout' else 30,
                  command_executor=SandboxExecutor(manager, handle, tmp_path / 'repo'))
    async def cancel_later():
        await asyncio.sleep(4)
        ctx.cancellation.cancel()
    cancel = asyncio.create_task(cancel_later()) if stop == 'cancel' else None
    try:
        with pytest.raises(TimeoutError if stop == 'timeout' else CancelledByUser):
            await McpCommunityTool(server, server.grants[0]).execute(ctx, {})
        assert not manager._handles and not (tmp_path / STATE).exists()
    finally:
        if cancel:
            cancel.cancel()
            await asyncio.gather(cancel, return_exceptions=True)
        await manager.aclose()
