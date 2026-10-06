"""Actual VM container candidate queries and independent publication gates."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from codeagent.agent.models import RunStatus
from codeagent.context.history.conversation_history import validate_tool_protocol
from codeagent.execution.models import ExecutionLimits, SandboxError
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.infra.cancellation import CancelledByUser
from codeagent.knowledge.tool import KnowledgeTool
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.session import AgentSession
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.sandbox import SandboxTools
from scenarios.knowledge_tasks import tasks
from tests.test_knowledge import citation
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_mcp_project import context

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_PODMAN_TEST_IMAGE'),
    reason='requires independent Ubuntu VM and explicitly trusted Podman image',
)


def manager():
    return PodmanSandboxManager(os.environ['MINDCODE_PODMAN_TEST_IMAGE'], limits=ExecutionLimits(
        memory_mib=128, workspace_mib=16, temporary_mib=8, cpus=.5, pids=16,
    ))


async def test_actual_candidate_incremental_references_privacy_and_no_index_artifact(tmp_path):
    ctx = context(tmp_path)
    host = (ctx.workspace.root / 'calc.py').read_bytes()
    snapshot = TreeSnapshot((
        SnapshotEntry('.gitignore', b'ignored.txt\n'),
        SnapshotEntry('calc.py', b'class CandidateOnly:\n    pass\n'),
        SnapshotEntry('credentials.json', b'{"secret":"PRIVATE_SENTINEL"}'),
        SnapshotEntry('docs/guide.md', b'CandidateOnly usage\n'),
        SnapshotEntry('ignored.txt', b'PRIVATE_SENTINEL'),
    ))
    sandbox = manager()
    handle = await sandbox.open(snapshot)
    executor = SandboxExecutor(sandbox, handle, ctx.workspace.root)
    ctx = replace(ctx, command_executor=executor)
    tools = SandboxTools(executor)
    try:
        search = KnowledgeTool('search')
        found = await tools.execute(search, ctx, {'query': 'CandidateOnly', 'kind': 'symbol'})
        assert not found.is_error and 'def add' not in found.content
        data = json.loads(found.content)
        match = data['result']['matches'][0]
        read = await tools.execute(KnowledgeTool('get'), ctx, citation(match))
        assert not read.is_error and 'class CandidateOnly' in read.content
        cached = await tools.execute(search, ctx, {'query': 'CandidateOnly'})
        assert json.loads(cached.content)['stats']['rebuilt'] == 0
        assert executor.knowledge.cache
        private = await tools.execute(search, ctx, {'query': 'PRIVATE_SENTINEL'})
        assert not json.loads(private.content)['result']['matches']
        assert await sandbox.seal(handle) == snapshot
        assert (ctx.workspace.root / 'calc.py').read_bytes() == host
    finally:
        await sandbox.aclose()


@pytest.mark.parametrize('operation', ['modify', 'rename', 'delete'])
async def test_actual_commands_invalidate_candidate_reference(tmp_path, operation):
    ctx = context(tmp_path)
    sandbox = manager()
    snapshot = TreeSnapshot((SnapshotEntry('calc.py', b'def Original(): pass\n'),))
    handle = await sandbox.open(snapshot)
    executor = SandboxExecutor(sandbox, handle, ctx.workspace.root)
    ctx = replace(ctx, command_executor=executor)
    search = KnowledgeTool('search')
    try:
        found = await search.execute(ctx, {'query': 'Original', 'kind': 'symbol'})
        match = json.loads(found.content)['result']['matches'][0]
        command = {
            'modify': "printf 'def Changed(): pass\\n' > calc.py",
            'rename': 'mv calc.py renamed.py', 'delete': 'rm calc.py',
        }[operation]
        changed = await sandbox.execute(handle, command)
        assert changed.returncode == 0
        stale = await KnowledgeTool('get').execute(ctx, citation(match))
        assert stale.is_error and '失效' in stale.content
        fresh = await search.execute(ctx, {'query': 'Original', 'kind': 'symbol'})
        assert not fresh.is_error
        hits = json.loads(fresh.content)['result']['matches']
        assert (hits[0]['path'] == 'renamed.py') if operation == 'rename' else hits == []
        sealed = await sandbox.seal(handle)
        assert all('knowledge' not in e.path for e in sealed.entries)
    finally:
        await sandbox.aclose()


async def test_two_workers_keep_independent_index_and_closed_handles_cannot_read(tmp_path):
    ctx = context(tmp_path)
    sandbox = manager()
    left = await sandbox.open(TreeSnapshot((SnapshotEntry('a.py', b'class Left: pass\n'),)))
    right = await sandbox.open(TreeSnapshot((SnapshotEntry('a.py', b'class Right: pass\n'),)))
    left_executor = SandboxExecutor(sandbox, left, ctx.workspace.root)
    right_executor = SandboxExecutor(sandbox, right, ctx.workspace.root)
    tool = KnowledgeTool('search')
    try:
        lhs = await tool.execute(replace(ctx, command_executor=left_executor), {'query': 'Left'})
        rhs = await tool.execute(replace(ctx, command_executor=right_executor), {'query': 'Left'})
        assert json.loads(lhs.content)['result']['matches']
        assert not json.loads(rhs.content)['result']['matches']
        assert left_executor.knowledge.cache is not right_executor.knowledge.cache
        await sandbox.close(left)
        with pytest.raises(SandboxError):
            await tool.execute(replace(ctx, command_executor=left_executor), {'query': 'Left'})
    finally:
        await sandbox.aclose()


@pytest.mark.parametrize('accept', [True, False])
async def test_knowledge_worker_search_after_write_keeps_publication_gate(tmp_path, accept):
    _init_repo(tmp_path)
    before = _git_out(tmp_path, 'rev-parse', 'HEAD')
    cfg = replace(_config(tmp_path), execution_backend='podman',
                  sandbox_image=os.environ['MINDCODE_PODMAN_TEST_IMAGE'], knowledge_enabled=True,
                  verify_command='test -f calc.py' if accept else 'exit 1')
    cfg = replace(cfg, profile=replace(cfg.profile, master_max_replans=0))
    class Worker(StubLlmClient):
        observed = False
        async def chat(self, messages, *, model_config, tools=()):
            if self.call_count == 2:
                results = [b for m in messages for b in m.tool_results]
                result = next(b for b in results if '"index_version":' in b.content)
                assert not result.is_error and 'WorkerCurrent' in result.content
                self.observed = True
            return await super().chat(messages, model_config=model_config, tools=tools)
    client = Worker([
        [('write_file', {'path': 'calc.py', 'content': 'class WorkerCurrent: pass\n'})],
        [('knowledge_search', {'query': 'WorkerCurrent', 'kind': 'symbol'})], 'done',
    ])
    async with MasterSession(
        cfg, llm_client=client,
        planner=StaticPlanner(TaskGraph([Step('s', 'default', 'write then query calc.py')])),
    ) as session:
        result = await session.run_task('write then find class')
        assert client.observed and result.accepted is accept and result.integrated is accept
        assert (tmp_path / 'calc.py').exists() is accept
        assert (_git_out(tmp_path, 'rev-parse', 'HEAD') != before) is accept
        assert _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles


@pytest.mark.parametrize('fault', ['timeout', 'cancel'])
async def test_helper_timeout_or_cancel_closes_actual_domain(tmp_path, fault):
    ctx = context(tmp_path)
    sandbox = manager()
    handle = await sandbox.open(TreeSnapshot((SnapshotEntry('a.py', b'class A: pass\n'),)))
    executor = SandboxExecutor(sandbox, handle, ctx.workspace.root)
    ctx = replace(ctx, command_executor=executor, timeout_seconds=.15 if fault == 'timeout' else 20)
    tool = KnowledgeTool('search')
    # Artificial paused helper only for deadline/cancellation fault injection.
    tool._source = 'import time\ntime.sleep(30)'
    task = asyncio.create_task(tool.execute(ctx, {'query': 'A'}))
    try:
        if fault == 'cancel':
            await asyncio.sleep(.15)
            ctx.cancellation.cancel()
        with pytest.raises(TimeoutError if fault == 'timeout' else CancelledByUser):
            await task
        assert not sandbox._handles and not executor.knowledge.cache
    finally:
        await sandbox.aclose()


async def test_actual_trial_16k_transport_preserves_results_and_domain(tmp_path):
    ctx = context(tmp_path)
    task = next(t for t in tasks() if t.name == 'policy_weights')
    snapshot = TreeSnapshot(tuple(
        SnapshotEntry(p, v.encode()) for p, v in sorted(task.files.items())))
    sandbox = PodmanSandboxManager(os.environ['MINDCODE_PODMAN_TEST_IMAGE'], limits=ExecutionLimits(
        memory_mib=192, workspace_mib=24, temporary_mib=8, cpus=.5, pids=24,
        output_bytes=16384))
    handle = await sandbox.open(snapshot)
    executor = SandboxExecutor(sandbox, handle, ctx.workspace.root)
    ctx = replace(ctx, command_executor=executor, max_output_bytes=16384)
    search = KnowledgeTool('search')
    try:
        found = await search.execute(ctx, {'query': 'routing', 'kind': 'path', 'limit': 20})
        assert not found.is_error
        data = json.loads(found.content)
        assert len(data['result']['matches']) == 4 and not executor.knowledge.cache
        assert sandbox.is_active(handle)
        # Citation integrity does not depend on retaining a cache.
        read = await KnowledgeTool('get').execute(ctx, citation(data['result']['matches'][0]))
        assert not read.is_error
        narrow = await search.execute(ctx, {'query': 'docs/current.md', 'kind': 'path'})
        assert not narrow.is_error and executor.knowledge.cache
        cached = await search.execute(ctx, {'query': 'docs/current.md', 'kind': 'path'})
        assert json.loads(cached.content)['stats']['reused'] == 31
        too_large = await search.execute(replace(ctx, max_output_bytes=100), {'query': 'routing'})
        assert too_large.is_error and 'limit' in too_large.content
        alive = await sandbox.execute(handle, 'cat routing/catalog.py')
        assert alive.returncode == 0 and b'DEFAULTS' in alive.stdout
        assert await sandbox.seal(handle) == snapshot
    finally:
        await sandbox.aclose()


async def test_actual_output_fault_stops_react_and_completes_tool_protocol(config, workspace):
    sandbox = PodmanSandboxManager(os.environ['MINDCODE_PODMAN_TEST_IMAGE'], limits=ExecutionLimits(
        memory_mib=192, workspace_mib=24, temporary_mib=8, cpus=.5, pids=24,
        output_bytes=16384))
    handle = await sandbox.open(TreeSnapshot(()))
    client = StubLlmClient([
        [('run_command', {'command': "python -c \"print('x'*20000)\""}),
         ('knowledge_search', {'query': 'routing'}),
         ('write_file', {'path': 'closed.py', 'content': 'BAD'})],
        'must not call model again',
    ])
    try:
        async with AgentSession(replace(config, knowledge_enabled=True),
                                llm_client=client) as session:
            session.run.sandbox = SandboxTools(SandboxExecutor(sandbox, handle, workspace))
            result = await session.send('exercise hard output limit')
            assert result.status is RunStatus.FAILED and client.call_count == 1
            runs = session.run.context.tool_runs
            assert len(runs) == 3
            assert all(r.result is not None and r.result.metadata['execution_domain_closed']
                       for r in runs)
            validate_tool_protocol(session.run.history.messages)
            assert not sandbox._handles and not (workspace / 'closed.py').exists()
            with pytest.raises(SandboxError):
                await sandbox.seal(handle)
    finally:
        await sandbox.aclose()
