"""Actual container boundary and publication; fixed model, genuine MCP stdio service."""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from codeagent.execution.models import ExecutionLimits
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.mcp.config import McpConfig
from codeagent.tool.mcp.project_tool import McpProjectTool
from codeagent.tool.sandbox import SandboxTools
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_mcp_project import context

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('MINDCODE_PODMAN_TEST_IMAGE') is None,
    reason='requires independent Ubuntu VM and an explicitly trusted Podman image',
)


async def test_mcp_reads_container_snapshot_and_blocks_host_and_private_paths(tmp_path):
    ctx = context(tmp_path)
    (ctx.workspace.root / 'calc.py').write_text('class HostOnly: pass\n')
    image = os.environ['MINDCODE_PODMAN_TEST_IMAGE']
    manager = PodmanSandboxManager(image, limits=ExecutionLimits(
        memory_mib=128, workspace_mib=16, temporary_mib=8, cpus=.5, pids=16,
    ))
    snapshot = TreeSnapshot((SnapshotEntry('calc.py', b'class ContainerOnly: pass\n'),))
    handle = await manager.open(snapshot)
    try:
        tools = SandboxTools(SandboxExecutor(manager, handle, ctx.workspace.root))
        ctx = replace(ctx, command_executor=tools.executor)
        tool = McpProjectTool('python_symbols')
        result = await tools.execute(tool, ctx, {'path': 'calc.py'})
        assert not result.is_error and 'ContainerOnly' in result.content
        assert 'HostOnly' not in result.content
        for path in [str(tmp_path / 'host-secret.py'), '../escape.py', '.env', '/etc/passwd']:
            denied = await tools.execute(tool, ctx, {'path': path})
            assert denied.is_error
        assert await manager.seal(handle) == snapshot
        assert not manager._handles
        assert (ctx.workspace.root / 'calc.py').read_text() == 'class HostOnly: pass\n'
    finally:
        await manager.aclose()


@pytest.mark.parametrize('accept', [True, False])
async def test_mcp_worker_tool_keeps_independent_publication_gate(tmp_path, accept):
    _init_repo(tmp_path)
    before = _git_out(tmp_path, 'rev-parse', 'HEAD')
    cfg = replace(_config(tmp_path), execution_backend='podman',
                  sandbox_image=os.environ['MINDCODE_PODMAN_TEST_IMAGE'],
                  mcp=McpConfig(('python_symbols',)),
                  verify_command='test -f calc.py' if accept else 'exit 1')
    cfg = replace(cfg, profile=replace(cfg.profile, master_max_replans=0))
    class Worker(StubLlmClient):
        observed = False
        async def chat(self, messages, *, model_config, tools=()):
            if self.call_count == 2:
                results = [b for m in messages for b in m.tool_results]
                assert any('WorkerOnly' in b.content and not b.is_error for b in results)
                self.observed = True
            return await super().chat(messages, model_config=model_config, tools=tools)
    client = Worker([
        [('write_file', {'path': 'calc.py', 'content': 'class WorkerOnly: pass\n'})],
        [('mcp_project_python_symbols', {'path': 'calc.py'})], 'done',
    ])
    async with MasterSession(
        cfg, llm_client=client,
        planner=StaticPlanner(TaskGraph([Step('s', 'default', 'write and inspect calc.py')])),
    ) as session:
        result = await session.run_task('write and inspect a Python class')
        assert client.observed and result.accepted is accept and result.integrated is accept
        assert (tmp_path / 'calc.py').exists() is accept
        assert (_git_out(tmp_path, 'rev-parse', 'HEAD') != before) is accept
        assert _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles
