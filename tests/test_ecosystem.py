"""Real community positives (opt-in), plus adversarial boundary regressions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.config import AppConfig
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from codeagent.skills.config import SkillConfig
from codeagent.skills.package import install_package, load_package
from codeagent.tool.mcp import community_bridge
from codeagent.tool.mcp.community import McpCommunityTool, McpGrant, McpServer, inspect_server
from codeagent.tool.mcp.config import McpConfig
from codeagent.tool.mcp.project_tool import _local_exchange
from tests.test_mcp_project import context

pytest.importorskip('yaml', reason='install codeagent[ecosystem] for package compatibility')
pytest.importorskip('jsonschema', reason='install codeagent[ecosystem] for MCP schema validation')


def fixture():
    filename = os.environ.get('MINDCODE_ECOSYSTEM_FIXTURE')
    if not filename:
        pytest.skip('requires explicitly installed pinned community sources and official servers')
    return json.loads(Path(filename).read_text(encoding='utf-8'))


def package_config(tmp_path, monkeypatch, path, package, *, tools=()):
    filename = tmp_path / 'skills.json'
    filename.write_text(json.dumps({'version': 1, 'active': 'review', 'packages': [{
        'id': 'review', 'path': str(path), 'sha256': package.digest, 'tools': list(tools),
    }]}), encoding='utf-8')
    monkeypatch.setenv('CODEAGENT_SKILLS_CONFIG', str(filename))
    return SkillConfig.from_env()


@pytest.mark.parametrize('repo', ['jay', 'portable'])
def test_real_community_skill_packages_round_trip(tmp_path, monkeypatch, repo):
    data = fixture()
    source = Path(data['sources']) / repo
    paths = (list(source.glob('*/SKILL.md')) if repo == 'jay'
             else list((source / 'skills').glob('*/SKILL.md'))
             + list((source / 'skills/archived').glob('*/SKILL.md')))
    assert len(paths) == (74 if repo == 'jay' else 75)
    for marker in paths:
        package = load_package(marker.parent)
        installed = install_package(marker.parent, tmp_path / repo)
        assert load_package(installed) == package
        cfg = package_config(tmp_path, monkeypatch, installed, package)
        assert cfg.definitions[0].package == package and cfg.definitions[0].tools == ()
        with pytest.raises(FileExistsError):
            install_package(marker.parent, tmp_path / repo)


async def test_real_companion_resources_frozen_and_scoped(tmp_path, monkeypatch):
    source = Path(fixture()['sources']) / 'portable/skills/code-tour'
    package = load_package(source)
    installed = install_package(source, tmp_path / 'installed')
    resource = 'skill_review_resource'
    cfg = package_config(tmp_path, monkeypatch, installed, package, tools=(resource,))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    script = installed / 'scripts/validate_tour.py'
    assert script.exists()
    script.write_text('MUTATED AFTER LOAD', encoding='utf-8')
    client = StubLlmClient([[(resource, {'path': 'scripts/validate_tour.py', 'limit': 12}),
                             (resource, {'path': '../../outside-secret'}),
                             ('write_file', {'path': 'escaped', 'content': 'bad'})], 'done'])
    async with AgentSession(AppConfig(workspace, tmp_path / 'state', skills=cfg),
                            llm_client=client) as session:
        assert (await session.send('inspect companion')).ok
        blocks = [b for m in client.seen_calls[-1] for b in m.tool_results]
        assert 'CodeTour validator' in blocks[0].content and not blocks[0].is_error
        assert 'MUTATED' not in blocks[0].content
        assert blocks[1].is_error and blocks[2].is_error
        assert not (workspace / 'escaped').exists()


@pytest.mark.parametrize('fault', ['duplicate', 'alias', 'tag', 'private', 'symlink', 'oversize'])
def test_malicious_package_rejected(tmp_path, fault):
    root = tmp_path / 'unsafe'
    root.mkdir()
    text = '---\nname: unsafe\ndescription: adversarial fixture\n---\nBody'
    if fault == 'duplicate':
        text = text.replace('name: unsafe', 'name: unsafe\nname: unsafe')
    elif fault == 'alias':
        text = text.replace('description: adversarial fixture',
                            'description: &a data\nmetadata: {key: *a}')
    elif fault == 'tag':
        text = text.replace('description: adversarial fixture',
                            'description: !!python/object/apply:os.system [echo forbidden]')
    elif fault == 'private':
        (root / '.env').write_text('SECRET', encoding='utf-8')
    elif fault == 'symlink':
        try:
            (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
        except OSError:
            pytest.skip('symlink creation unavailable')
    elif fault == 'oversize':
        text += 'a' * 65536
    (root / 'SKILL.md').write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        load_package(root)


@pytest.mark.parametrize('schema', [
    {'type': 'object', '$ref': 'https://example.invalid/schema'},
    {'type': 'object', 'properties': {'x': {'$ref': 'file:///etc/passwd'}}},
    {'type': 'object', '$dynamicRef': 'https://example.invalid/schema'},
    {'type': 'object', '$recursiveRef': 'https://example.invalid/schema'},
    {'type': 'array'},
])
def test_schema_never_fetches_remote_or_file_references(schema):
    with pytest.raises(ValueError):
        community_bridge.check_schema(schema)


async def test_generic_mcp_cannot_fallback_to_host(tmp_path, monkeypatch):
    grant = McpGrant('readonly', json.dumps({'name': 'readonly', 'description': 'negative case',
                                           'inputSchema': {'type': 'object'}}))
    server = McpServer('negative', ('never-run',), ('never-run',), (grant,))
    async def forbidden(*a, **k):
        raise AssertionError('host subprocess must never start')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    result = await McpCommunityTool(server, grant).execute(context(tmp_path), {})
    assert result.is_error and 'Podman' in result.content


@pytest.mark.parametrize('server_id', ['time', 'filesystem', 'everything', 'git'])
async def test_official_mcp_discovery_and_call(tmp_path, server_id):
    data = fixture()['servers'][server_id]
    ctx = replace(context(tmp_path), timeout_seconds=30)
    if server_id == 'git':
        from tests.test_master_integration import _init_repo
        _init_repo(ctx.workspace.root)
    command = tuple(a.replace('{workspace}', str(ctx.workspace.root)) for a in data['command'])
    catalog = await inspect_server(ctx, command)
    assert catalog['tools'] and catalog['protocolVersion']
    if server_id == 'everything':
        assert catalog['resources'] and catalog['prompts'] and catalog['resourceTemplates']
    descriptor = next(t for t in catalog['tools'] if t['name'] == data['tool'])
    arguments = {k: v.replace('{workspace}', str(ctx.workspace.root)) if isinstance(v, str) else v
                 for k, v in data['arguments'].items()}
    payload = {'action': 'call', 'command': list(command), 'descriptor': descriptor,
               'arguments': arguments, 'timeout_seconds': 30, 'max_response_bytes': 1048576}
    source = await asyncio.to_thread(Path(community_bridge.__file__).read_text, encoding='utf-8')
    raw, code = await _local_exchange(ctx, source, json.dumps(payload).encode(), 5 * 1048576)
    result = json.loads(raw)
    assert code == 0 and not result.get('isError') and result['content']
    assert data['expected'] in json.dumps(result)
    # Invalid real schema: validation runs before subprocess creation.
    payload['arguments'] = {'forged': True}
    raw, code = await _local_exchange(ctx, source, json.dumps(payload).encode(), 5 * 1048576)
    assert code != 0 and json.loads(raw)['bridge_error'] == 'ValidationError'


async def test_everything_resources_prompts_and_multimedia_boundary(tmp_path):
    data = fixture()['servers']['everything']
    ctx = replace(context(tmp_path), timeout_seconds=30)
    command = data['command']
    source = await asyncio.to_thread(Path(community_bridge.__file__).read_text, encoding='utf-8')
    probes = [('resources/read', {'uri': 'demo://resource/static/document/architecture.md'}),
              ('prompts/get', {'name': 'args-prompt', 'arguments': {'city': 'Paris'}})]
    for method, params in probes:
        raw, code = await _local_exchange(ctx, source, json.dumps({
            'action': 'probe', 'command': command, 'method': method,
            'params': params, 'timeout_seconds': 30,
        }).encode(), 5 * 1048576)
        assert code == 0 and json.loads(raw)
    catalog = await inspect_server(ctx, tuple(command))
    for name, arguments, accepted in [
        ('get-structured-content', {'location': 'Chicago'}, True),
        ('get-tiny-image', {}, False),
    ]:
        descriptor = next(t for t in catalog['tools'] if t['name'] == name)
        raw, code = await _local_exchange(ctx, source, json.dumps({
            'action': 'call', 'command': command, 'descriptor': descriptor,
            'arguments': arguments, 'timeout_seconds': 30, 'max_response_bytes': 1048576,
        }).encode(), 5 * 1048576)
        assert (code == 0) is accepted
        if accepted:
            assert json.loads(raw)['structuredContent']
        else:
            assert json.loads(raw)['bridge_error'] == 'ValueError'


async def test_real_server_schema_change_is_rejected_before_call(tmp_path):
    data = fixture()['servers']['everything']
    ctx = replace(context(tmp_path), timeout_seconds=30)
    catalog = await inspect_server(ctx, tuple(data['command']))
    descriptor = next(t for t in catalog['tools'] if t['name'] == 'echo')
    descriptor['inputSchema']['properties']['message']['type'] = 'integer'
    source = await asyncio.to_thread(Path(community_bridge.__file__).read_text, encoding='utf-8')
    raw, code = await _local_exchange(ctx, source, json.dumps({
        'action': 'call', 'command': data['command'], 'descriptor': descriptor,
        'arguments': {'message': 7}, 'timeout_seconds': 30, 'max_response_bytes': 1048576,
    }).encode(), 5 * 1048576)
    assert code != 0 and json.loads(raw)['bridge_error'] == 'ValueError'


def test_pinned_config_detects_catalog_tampering(tmp_path, monkeypatch):
    data = fixture()['servers']['time']
    raw = Path(data['catalog']).read_bytes()
    catalog = tmp_path / 'catalog.json'
    catalog.write_bytes(raw)
    filename = tmp_path / 'mcp.json'
    filename.write_text(json.dumps({'version': 2, 'servers': [{
        'id': 'time', 'command': data['command'], 'container_command': data['container_command'],
        'catalog': 'catalog.json', 'catalog_sha256': hashlib.sha256(raw).hexdigest(),
        'tools': {data['tool']: {'effect': 'read_only', 'retry': 'safe'}},
    }]}), encoding='utf-8')
    monkeypatch.setenv('CODEAGENT_MCP_CONFIG', str(filename))
    cfg = McpConfig.from_env()
    assert cfg.servers[0].grants[0].name == data['tool']
    catalog.write_bytes(b'{}')
    assert cfg.servers[0].grants[0].name == data['tool']
    with pytest.raises(ValueError, match='SHA256'):
        McpConfig.from_env()
