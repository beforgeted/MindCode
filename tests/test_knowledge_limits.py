"""Regressions for the real trial's cache envelope and terminal domain failure."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codeagent.agent.models import RunStatus
from codeagent.context.history.conversation_history import validate_tool_protocol
from codeagent.execution.models import SandboxError
from codeagent.execution.podman import _ATTEST
from codeagent.execution.snapshot import TreeSnapshot
from codeagent.infra.cancellation import CancelledByUser
from codeagent.knowledge import index
from codeagent.knowledge.tool import KnowledgeTool
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.sandbox import SandboxTools
from scenarios.knowledge_tasks import tasks
from tests.test_podman_sandbox import FakePodman


def policy_response(tmp_path):
    task = next(t for t in tasks() if t.name == 'policy_weights')
    for name, text in task.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    return index.invoke(tmp_path, 'search', {'query': 'routing', 'kind': 'path', 'limit': 20})


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def test_original_trial_envelope_drops_cache_without_losing_results(tmp_path):
    response = policy_response(tmp_path)
    assert size(response) == 16718
    bounded = index.bounded_response(response, 16384, 16384)
    assert 'cache' not in bounded and size(bounded) == 1744
    assert bounded['result'] == response['result']
    assert bounded['index_version'] == response['index_version']
    # A tiny visible budget must reject, never present a partial match set.
    assert index.bounded_response(response, 16384, 100) == {'error': 'ResultLimit'}
    assert index.bounded_response(response, 1024, 16384) == {'error': 'TransportLimit'}
    assert index.bounded_response(response, index.MAX_ENVELOPE, 16384) is response


@pytest.mark.parametrize('transport,result', [(True, 1), (63, 1), (1024, False), (1024, 0)])
def test_invalid_internal_response_budgets_rejected(tmp_path, transport, result):
    with pytest.raises(ValueError, match='budget'):
        index.bounded_response(policy_response(tmp_path), transport, result)


def test_document_query_contract_is_explicit_and_all_terms_match_one_record(tmp_path):
    policy_response(tmp_path)
    combined = index.invoke(tmp_path, 'search', {
        'query': '渠道 权重 归一 overrides DEFAULTS', 'kind': 'document'})
    assert combined['result']['matches'] == []
    single = index.invoke(tmp_path, 'search', {'query': '权重', 'kind': 'document'})
    assert single['result']['matches']
    assert '同一条记录' in KnowledgeTool('search').spec.description


class FatalPodman(FakePodman):
    def __init__(self, fault):
        super().__init__()
        self.fault = fault
        self.executed = 0
        self.armed = False

    async def _control(self, *args, **kwargs):
        if self.armed and args[0] == 'exec' and args[-1] != _ATTEST:
            self.executed += 1
            raise self.fault
        return await super()._control(*args, **kwargs)


@pytest.mark.parametrize('fault,status', [
    (SandboxError('执行输出超出字节上限'), RunStatus.FAILED),
    (TimeoutError(), RunStatus.FAILED),
    (CancelledByUser('cancel'), RunStatus.CANCELLED),
])
async def test_closed_domain_stops_before_next_model_and_pads_batch(
        config, workspace, monkeypatch, fault, status):
    monkeypatch.setattr('codeagent.execution.podman._process_start', lambda _: '42')
    manager = FatalPodman(fault)
    handle = await manager.open(TreeSnapshot(()))
    manager.armed = True
    client = StubLlmClient([
        [('knowledge_search', {'query': 'routing'}),
         ('write_file', {'path': 'should_not_run.py', 'content': 'BAD'})],
        'must not reach model again',
    ])
    async with AgentSession(replace(config, knowledge_enabled=True), llm_client=client) as session:
        session.run.sandbox = SandboxTools(SandboxExecutor(manager, handle, workspace))
        result = await session.send('query')
        assert result.status is status and client.call_count == 1
        assert manager.executed == 1 and not manager.is_active(handle)
        assert not (workspace / 'should_not_run.py').exists()
        runs = session.run.context.tool_runs
        assert len(runs) == 2
        assert all(r.result is not None and r.result.metadata['execution_domain_closed']
                   for r in runs)
        validate_tool_protocol(session.run.history.messages)
    await manager.aclose()


async def test_already_closed_domain_never_calls_model(config, workspace, monkeypatch):
    monkeypatch.setattr('codeagent.execution.podman._process_start', lambda _: '42')
    manager = FakePodman()
    handle = await manager.open(TreeSnapshot(()))
    await manager.close(handle)
    client = StubLlmClient(['must not reach model'])
    async with AgentSession(config, llm_client=client) as session:
        session.run.sandbox = SandboxTools(SandboxExecutor(manager, handle, workspace))
        result = await session.send('query')
        assert result.status is RunStatus.FAILED and client.call_count == 0
        validate_tool_protocol(session.run.history.messages)
    await manager.aclose()
