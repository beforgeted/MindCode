from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from dataclasses import replace

import pytest

from codeagent.config import AppConfig
from codeagent.evidence.event_store import NullEventStore
from codeagent.execution.download import ControlledDownloader, DownloadError, DownloadPolicy
from codeagent.execution.models import ExecutionPurpose, ProcessOutput, SandboxHandle
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotError
from codeagent.infra.cancellation import CancellationToken, CancelledByUser
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.builtin.download_file import DownloadFileTool
from codeagent.tool.executor import SandboxExecutor
from codeagent.workspace.context import WorkspaceContext

HOST = 'files.example.org'
URL = f'https://{HOST}/wheel'
BODY = b'checked data'
SHA = hashlib.sha256(BODY).hexdigest()


class Events(NullEventStore):
    def __init__(self):
        self.items = []
        self.flushes = 0
    def append_nowait(self, event):
        self.items.append(event)
    async def flush(self):
        self.flushes += 1


async def fetch(broker, token=None, **kwargs):
    return await broker.fetch(URL, SHA, token or CancellationToken(),
                              session_id='s', agent_run_id='r', tool_run_id='t', **kwargs)


@pytest.fixture
def process(monkeypatch):
    calls = []
    async def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProcessOutput(0, json.dumps({
            'data': base64.b64encode(BODY).decode(),
            'hops': [{'url': URL, 'ip': '8.8.8.8', 'status': 200}],
        }).encode(), b'')
    monkeypatch.setattr('codeagent.execution.download.run_bounded', run)
    return calls


async def test_default_noninteractive_denial_never_starts_helper(process):
    events = Events()
    broker = ControlledDownloader(DownloadPolicy((HOST,)), events=events)
    with pytest.raises(DownloadError, match='not approved'):
        await fetch(broker)
    assert process == [] and events.items[-1].payload['status'] == 'denied'


async def test_explicit_allowlist_isolated_helper_and_durable_audit(process):
    events = Events()
    broker = ControlledDownloader(DownloadPolicy((HOST,), 'allowlist'), events=events)
    assert await fetch(broker) == BODY
    argv, kwargs = process[0]
    assert argv[:3] == (sys.executable, '-I', '-c')
    assert json.loads(kwargs['data'])['hosts'] == [HOST]
    assert [e.payload['status'] for e in events.items] == ['started', 'downloaded']
    assert events.flushes == 2
    assert events.items[-1].payload['hops'][0]['ip'] == '8.8.8.8'
    assert 'data' not in events.items[-1].payload


async def test_no_network_before_operator_approval(process):
    entered, release = asyncio.Event(), asyncio.Event()
    class Approve:
        async def approve(self, decision, *, command):
            assert 'GET https://' in command and SHA in command
            entered.set()
            await release.wait()
            return True
    broker = ControlledDownloader(DownloadPolicy((HOST,)), approval=Approve())
    task = asyncio.create_task(fetch(broker))
    await entered.wait()
    assert not process
    release.set()
    assert await task == BODY


async def test_token_cancel_during_approval_drains_operation(process):
    entered, stopped = asyncio.Event(), asyncio.Event()
    class Approve:
        async def approve(self, decision, *, command):
            entered.set()
            try:
                await asyncio.Event().wait()
                return False
            finally:
                stopped.set()
    token = CancellationToken()
    events = Events()
    broker = ControlledDownloader(DownloadPolicy((HOST,)), approval=Approve(), events=events)
    task = asyncio.create_task(fetch(broker, token))
    await entered.wait()
    token.cancel()
    with pytest.raises(CancelledByUser):
        await asyncio.wait_for(task, 2)
    assert stopped.is_set() and not process
    assert events.items[-1].payload['status'] == 'cancelled'


@pytest.mark.parametrize('mode', ['token', 'task', 'timeout'])
async def test_cancel_and_timeout_drain_download_helper(monkeypatch, mode):
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def run(*args, **kwargs):
        entered.set()
        try:
            if mode == 'timeout':
                raise TimeoutError()
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr('codeagent.execution.download.run_bounded', run)
    events = Events()
    broker = ControlledDownloader(DownloadPolicy((HOST,), 'allowlist'), events=events)
    token = CancellationToken()
    task = asyncio.create_task(fetch(broker, token))
    await entered.wait()
    if mode == 'token':
        token.cancel()
    elif mode == 'task':
        task.cancel()
    exception = {'token': CancelledByUser, 'task': asyncio.CancelledError,
                 'timeout': TimeoutError}[mode]
    with pytest.raises(exception):
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    assert events.items[-1].payload['status'] == ('timeout' if mode == 'timeout' else 'cancelled')


async def test_response_hash_is_rechecked_in_controller(monkeypatch):
    async def forged(*args, **kwargs):
        return ProcessOutput(0, b'{"data":"eA==","hops":[]}', b'')
    monkeypatch.setattr('codeagent.execution.download.run_bounded', forged)
    broker = ControlledDownloader(DownloadPolicy((HOST,), 'allowlist'))
    with pytest.raises(DownloadError, match='invalid downloader payload'):
        await fetch(broker)


async def test_actual_download_subprocess_has_no_api_key_or_proxy(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:9999')
    source = (
        "import os,json,base64,sys; json.load(sys.stdin); "
        "assert 'ANTHROPIC_API_KEY' not in os.environ and 'HTTPS_PROXY' not in os.environ; "
        f"print(json.dumps({{'data': {base64.b64encode(BODY).decode()!r}, 'hops': []}}))"
    )
    monkeypatch.setattr('codeagent.execution.download.Path.read_text', lambda *a, **kw: source)
    broker = ControlledDownloader(DownloadPolicy((HOST,), 'allowlist'))
    assert await fetch(broker) == BODY


async def test_audit_unavailable_blocks_request(process):
    class Broken(Events):
        async def flush(self):
            raise OSError('audit unavailable')
    broker = ControlledDownloader(DownloadPolicy((HOST,), 'allowlist'), events=Broken())
    with pytest.raises(OSError):
        await fetch(broker)
    assert not process


async def test_hung_audit_writer_is_bounded_and_blocks_request(process, monkeypatch):
    monkeypatch.setattr('codeagent.execution.download._AUDIT_FLUSH_SECONDS', 0.01)
    class Hung(Events):
        async def flush(self):
            await asyncio.Event().wait()
    broker = ControlledDownloader(DownloadPolicy((HOST,), 'allowlist'), events=Hung())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fetch(broker), 1)
    assert not process


@pytest.mark.parametrize('values', [
    {'max_bytes': 0}, {'max_bytes': 8 * 1024 * 1024 + 1}, {'max_redirects': 6},
    {'timeout_seconds': float('nan')}, {'hosts': [HOST]}, {'approval_mode': 'automatic'},
])
def test_invalid_download_config(values):
    with pytest.raises(ValueError):
        DownloadPolicy(**values)


async def test_tool_is_only_registered_for_configured_podman(tmp_path):
    config = AppConfig(tmp_path, tmp_path / '.state', use_stub_llm=True)
    with pytest.raises(ValueError, match='Podman'):
        replace(config, downloads=DownloadPolicy((HOST,)))
    async with AgentSession(config, llm_client=StubLlmClient(['done'])) as session:
        assert not session.registry.has('download_file')
    config = replace(config, execution_backend='podman', sandbox_image='a' * 64,
                     downloads=DownloadPolicy((HOST,)))
    async with AgentSession(config, llm_client=StubLlmClient(['done'])) as session:
        assert 'download_file' in session.definition.allowed_tools


async def test_tool_requires_real_domain_and_rejects_path_before_fetch(
    tmp_path, artifact_store, process,
):
    context = ToolExecutionContext(
        'r', 's', 't', 'c', WorkspaceContext.local(tmp_path), CancellationToken(), artifact_store,
        downloader=ControlledDownloader(DownloadPolicy((HOST,), 'allowlist')),
    )
    tool = DownloadFileTool()
    assert (await tool.execute(context, {'url': URL, 'sha256': SHA, 'path': 'x'})).is_error
    manager = PodmanSandboxManager('a' * 64)
    handle = SandboxHandle('c', 'c', manager.owner, ExecutionPurpose.WORKER, 1, '1')
    context = replace(context, command_executor=SandboxExecutor(manager, handle, tmp_path))
    with pytest.raises(SnapshotError):
        await tool.execute(context, {'url': URL, 'sha256': SHA, 'path': '../escape'})
    assert not process and not (tmp_path / 'x').exists()
