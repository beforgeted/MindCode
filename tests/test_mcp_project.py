from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.config import AppConfig
from codeagent.context.profile import ContextProfile
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.evidence.event_store import NullEventStore
from codeagent.evidence.models import EventType
from codeagent.infra.cancellation import CancellationToken, CancelledByUser
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.execution_manager import ExecutionScope, ToolExecutionManager
from codeagent.tool.executor import filtered_env
from codeagent.tool.mcp import bridge, project_server
from codeagent.tool.mcp.config import McpConfig
from codeagent.tool.mcp.project_tool import McpProjectTool
from codeagent.tool.models import ToolCall, ToolResultStatus
from codeagent.tool.normalizer import ToolResultNormalizer
from codeagent.tool.registry import ToolRegistry
from codeagent.workspace.context import WorkspaceContext


class RecordingEvents(NullEventStore):
    def __init__(self):
        self.events = []

    def append_nowait(self, event):
        self.events.append(event)


def context(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
    return ToolExecutionContext(
        agent_run_id='worker', session_id='session', tool_run_id='tool', call_id='call',
        workspace=WorkspaceContext.local(repo), cancellation=CancellationToken(),
        artifact_store=FileArtifactStore(tmp_path / 'state'), timeout_seconds=5,
    )


@pytest.mark.parametrize('name', ['read_text', 'python_symbols'])
async def test_real_stdio_discovery_and_call(tmp_path, name):
    ctx = context(tmp_path)
    result = await McpProjectTool(name).execute(ctx, {'path': 'calc.py'})
    assert result.status == ToolResultStatus.OK and result.call_id == 'call'
    assert 'add' in result.content and result.metadata['mcp_tool'] == name
    assert sorted(p.name for p in ctx.workspace.root.iterdir()) == ['calc.py']
    if name == 'python_symbols':
        assert json.loads(result.content)['symbols'] == [
            {'name': 'add', 'kind': 'FunctionDef', 'line': 1},
        ]


@pytest.mark.parametrize('arguments', [
    {'path': '../outside.txt'}, {'path': '/etc/passwd'}, {'path': 'C:/secret'},
    {'path': '.env'}, {'path': '.private-docs/note.md'}, {'path': 'folder/.env'},
    {'path': 'a\\b'}, {'path': 'a//b'}, {'path': 'calc.py', 'limit': True},
    {'path': 'calc.py', 'offset': 0}, {'path': 'calc.py', 'limit': 201},
    {'path': 'calc.py', 'command': 'delete'}, {'path': 1},
])
async def test_bad_arguments_never_launch_service(tmp_path, monkeypatch, arguments):
    ctx = context(tmp_path)
    async def forbidden(*a, **k):
        raise AssertionError('must reject before process creation')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    assert (await McpProjectTool('read_text').execute(ctx, arguments)).is_error


@pytest.mark.parametrize('kind', ['missing', 'binary', 'oversized', 'invalid_python'])
async def test_service_rejections_are_bounded_and_do_not_expose_source(tmp_path, kind):
    ctx = context(tmp_path)
    path = ctx.workspace.root / 'bad.py'
    if kind != 'missing':
        path.write_bytes({'binary': b'\x00secret', 'oversized': b'x' * 262145,
                          'invalid_python': b'private_secret = (\n'}[kind])
    result = await McpProjectTool('python_symbols').execute(ctx, {'path': 'bad.py'})
    assert result.is_error and 'secret' not in result.content
    assert str(ctx.workspace.root) not in result.content


async def test_symlink_to_outside_is_not_followed(tmp_path):
    ctx = context(tmp_path)
    outside = tmp_path / 'outside.py'
    outside.write_text('SECRET_SENTINEL', encoding='utf-8')
    try:
        (ctx.workspace.root / 'link.py').symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable')
    result = await McpProjectTool('read_text').execute(ctx, {'path': 'link.py'})
    assert result.is_error and 'SECRET_SENTINEL' not in result.content


def test_allowlist_is_explicit_and_commands_are_not_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv('CODEAGENT_MCP_CONFIG', raising=False)
    assert not McpConfig.from_env().project_tools
    path = tmp_path / 'mcp.json'
    monkeypatch.setenv('CODEAGENT_MCP_CONFIG', str(path))
    path.write_text(json.dumps({'version': 1, 'project_tools': ['python_symbols']}))
    assert McpConfig.from_env() == McpConfig(('python_symbols',))
    for value in [
        {'version': 1, 'project_tools': ['write_file']},
        {'version': True, 'project_tools': []},
        {'version': 1, 'project_tools': ['read_text', 'read_text']},
        {'version': 1, 'project_tools': [], 'command': sys.executable},
    ]:
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            McpConfig.from_env()
    path.write_text('{"version":1,"version":1,"project_tools":[]}')
    with pytest.raises(ValueError, match='duplicate'):
        McpConfig.from_env()
    path.write_bytes(b'x' * 16385)
    with pytest.raises(ValueError, match='16KiB'):
        McpConfig.from_env()


async def test_session_opt_in_does_not_change_default_tools(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'calc.py').write_text('class Calculator:\n    pass\n', encoding='utf-8')
    config = AppConfig(repo, tmp_path / 'state', use_stub_llm=True)
    disabled = AgentSession(config, llm_client=StubLlmClient([]))
    assert not any(n.startswith('mcp_') for n in disabled.registry.names())
    client = StubLlmClient([[('mcp_project_python_symbols', {'path': 'calc.py'})], 'done'])
    async with AgentSession(replace(config, mcp=McpConfig(('python_symbols',))),
                            llm_client=client) as session:
        result = await session.send('find the class')
        assert result.ok
        assert 'mcp_project_python_symbols' in session.definition.allowed_tools
        await session.event_store.flush()
        assert any('Calculator' in str(m.blocks) for m in session.run.history.messages)


async def test_model_cannot_call_registered_tool_hidden_from_its_agent(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    config = AppConfig(ctx.workspace.root, tmp_path / 'session-state', use_stub_llm=True,
                       mcp=McpConfig(('python_symbols',)))
    client = StubLlmClient([[('mcp_project_python_symbols', {'path': 'calc.py'})], 'done'])
    session = AgentSession(config, llm_client=client)
    session.definition = replace(session.definition, allowed_tools=('read_file',))
    session.clear()
    async def forbidden(*a, **k):
        raise AssertionError('unadvertised MCP tool must not start a process')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    async with session:
        await session.send('try hidden tool')
        results = [b for m in session.run.history.messages for b in m.tool_results]
        assert len(results) == 1 and results[0].is_error
        assert '未获准' in results[0].content


async def test_failure_keeps_sibling_result_and_audit_identity(tmp_path):
    ctx = context(tmp_path)
    registry = ToolRegistry([McpProjectTool('python_symbols')])
    events = RecordingEvents()
    from codeagent.context.token_estimator import HeuristicTokenEstimator
    manager = ToolExecutionManager(
        registry=registry, artifact_store=ctx.artifact_store,
        normalizer=ToolResultNormalizer(estimator=HeuristicTokenEstimator(),
                                       artifact_store=ctx.artifact_store),
        event_store=events,
    )
    scope = ExecutionScope('worker', 'session', ctx.workspace, ctx.cancellation, ContextProfile())
    outcome = await manager.execute_batch(scope, [
        ToolCall('bad', 'mcp_project_python_symbols', {'path': '../private.py'}),
        ToolCall('good', 'mcp_project_python_symbols', {'path': 'calc.py'}),
    ])
    assert [r.call_id for r in outcome.results] == ['bad', 'good']
    assert outcome.results[0].is_error and not outcome.results[1].is_error
    assert len([e for e in events.events if e.type == EventType.TOOL_RESULT]) == 2


def fault_source(mode):
    return '''import json,sys,time,os
catalog=CATALOG
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    method=request['method']
    if method=='initialize':
        result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
    elif method=='tools/list': result={'tools':catalog}
    else:
        MODE
        result={'content':[{'type':'text','text':'ok'}],'isError':False}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
'''.replace('CATALOG', repr(project_server.CATALOG)).replace('MODE', mode)


@pytest.mark.parametrize('mode', [
    'sys.exit(0)', "print('invalid',flush=True); sys.exit(0)",
    "print('x'*100000,flush=True); sys.exit(0)",
    "print(json.dumps({'jsonrpc':'2.0','id':99,'result':{}}),flush=True); sys.exit(0)",
    "print('{\"jsonrpc\":\"2.0\",\"id\":3,\"id\":3,\"result\":{}}',flush=True);sys.exit(0)",
])
async def test_protocol_faults_fail_closed_and_child_is_reaped(mode):
    payload = {'source': fault_source(mode), 'catalog': project_server.CATALOG,
               'tool': 'read_text', 'arguments': {'path': 'a.py'}, 'max_response_bytes': 100}
    async with asyncio.timeout(5):
        with pytest.raises(ValueError):
            await bridge.call_project(payload)


async def test_catalog_annotations_do_not_authorize_extra_tool():
    changed = [*project_server.CATALOG, {'name': 'write', 'annotations': {'readOnlyHint': True}}]
    source = fault_source("raise AssertionError('must not call')").replace(
        'catalog=' + repr(project_server.CATALOG), 'catalog=' + repr(changed),
    )
    payload = {'source': source, 'catalog': project_server.CATALOG, 'tool': 'read_text',
               'arguments': {}, 'max_response_bytes': 100}
    with pytest.raises(ValueError, match='catalog'):
        await bridge.call_project(payload)


@pytest.mark.parametrize('cancel_kind', ['deadline', 'token', 'task'])
async def test_hung_service_bridge_is_cleaned_up(tmp_path, monkeypatch, cancel_kind):
    from codeagent.tool.mcp import project_tool
    ctx = context(tmp_path)
    ctx = replace(ctx, timeout_seconds=1)
    real_read = Path.read_text
    def replaced_read(path, *args, **kwargs):
        if path == Path(project_server.__file__):
            return fault_source('time.sleep(60)')
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', replaced_read)
    pids = []
    real_create = asyncio.create_subprocess_exec
    async def create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        pids.append(proc)
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    task = asyncio.create_task(project_tool.McpProjectTool('read_text').execute(
        ctx, {'path': 'calc.py'},
    ))
    if cancel_kind != 'deadline':
        await asyncio.sleep(.3)
        ctx.cancellation.cancel() if cancel_kind == 'token' else task.cancel()
    exception = {'deadline': TimeoutError, 'token': CancelledByUser,
                 'task': asyncio.CancelledError}[cancel_kind]
    with pytest.raises(exception):
        await task
    assert pids and all(p.returncode is not None for p in pids)


async def test_low_output_cap_is_enforced_before_normalizer(tmp_path):
    ctx = replace(context(tmp_path), max_output_bytes=1)
    assert (await McpProjectTool('read_text').execute(ctx, {'path': 'calc.py'})).is_error


async def test_registered_helper_is_not_reread_after_workspace_changes(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    tool = McpProjectTool('read_text')
    def forbidden(*args, **kwargs):
        raise AssertionError('registered controller source must be retained')
    monkeypatch.setattr(Path, 'read_text', forbidden)
    assert not (await tool.execute(ctx, {'path': 'calc.py'})).is_error


async def test_server_sampling_request_cannot_trigger_model_call():
    mode = '''print(json.dumps({'jsonrpc':'2.0','id':100,'method':'sampling/createMessage',
            'params':{'messages':[]}}),flush=True)
        refused=json.loads(sys.stdin.readline())
        assert refused['id']==100 and refused['error']['code']==-32601'''
    payload = {'source': fault_source(mode), 'catalog': project_server.CATALOG,
               'tool': 'read_text', 'arguments': {}, 'max_response_bytes': 100}
    async with asyncio.timeout(5):
        result = await bridge.call_project(payload)
    assert result['content'][0]['text'] == 'ok'


async def test_official_sdk_client_interoperates_with_bundled_service(tmp_path):
    pytest.importorskip('mcp', reason='optional official SDK interoperability dependency')
    sdk = importlib.import_module('mcp')
    transport = importlib.import_module('mcp.client.stdio')
    ctx = context(tmp_path)
    source = await asyncio.to_thread(Path(project_server.__file__).read_text, encoding='utf-8')
    params = sdk.StdioServerParameters(command=sys.executable, args=['-I', '-u', '-c', source],
                                       cwd=str(ctx.workspace.root), env=filtered_env())
    async with asyncio.timeout(10):
        async with transport.stdio_client(params) as (read, write):
            async with sdk.ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == 'mindcode-project'
                discovered = await session.list_tools()
                assert [t.name for t in discovered.tools] == ['read_text', 'python_symbols']
                result = await session.call_tool('python_symbols', {'path': 'calc.py'})
                assert not result.isError and 'add' in result.content[0].text
                denied = await session.call_tool('read_text', {'path': '../outside.py'})
                assert denied.isError


async def test_bounded_client_interoperates_with_official_sdk_server():
    pytest.importorskip('mcp', reason='optional official SDK interoperability dependency')
    source = '''import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
app=Server('official-sdk-fixture')
@app.list_tools()
async def tools():
    return [types.Tool(**d) for d in CATALOG]
@app.call_tool()
async def call(name,arguments):
    return [types.TextContent(type='text',text='SDK interoperability')]
async def main():
    async with stdio_server() as (read,write):
        await app.run(read,write,app.create_initialization_options())
asyncio.run(main())
'''.replace('CATALOG', repr(project_server.CATALOG))
    async with asyncio.timeout(10):
        result = await bridge.call_project({
            'source': source, 'catalog': project_server.CATALOG, 'tool': 'read_text',
            'arguments': {'path': 'a.py'}, 'max_response_bytes': 100,
        })
    assert result['content'][0]['text'] == 'SDK interoperability'
