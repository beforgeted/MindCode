"""Memory's candidate ownership and configuration, plus real official discovery."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.mcp import community_bridge
from codeagent.tool.mcp.community import McpCommunityTool, McpGrant, McpServer, inspect_server
from codeagent.tool.mcp.config import McpConfig
from tests.test_ecosystem import fixture
from tests.test_mcp_project import context


def memory_server(*names: str):
    data = fixture()['servers'].get('memory')
    if data is None:
        pytest.skip('requires an explicitly installed pinned official Memory server')
    catalog = json.loads(Path(data['catalog']).read_text(encoding='utf-8'))
    reads = {'read_graph', 'search_nodes', 'open_nodes'}
    grants = tuple(McpGrant(name, json.dumps(next(t for t in catalog['tools']
                                                if t['name'] == name)),
                            EffectKind.READ_ONLY if name in reads else EffectKind.WORKSPACE_WRITE,
                            RetryPolicy.SAFE if name in reads else RetryPolicy.NEVER)
                   for name in names)
    return McpServer('memory', tuple(data['command']), tuple(data['container_command']),
                     grants, workspace_memory=True)


@pytest.mark.parametrize('owner', ['', '../other', 'a/b', 'A', True, 'x' * 25])
def test_memory_owner_cannot_select_arbitrary_paths(tmp_path, owner):
    with pytest.raises(ValueError):
        community_bridge.workspace_memory_path(owner, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('fault', ['directory', 'oversize', 'file_in_parent'])
def test_memory_rejects_invalid_candidate_state(tmp_path, fault):
    path = community_bridge.workspace_memory_path('memory', tmp_path)
    if fault == 'directory':
        path.mkdir()
    elif fault == 'oversize':
        path.write_bytes(b'x' * (community_bridge.MAX_MEMORY_BYTES + 1))
    else:
        other = tmp_path / 'mcp-state/other'
        other.write_text('a file cannot own child state')
    with pytest.raises((ValueError, FileExistsError, NotADirectoryError)):
        community_bridge.workspace_memory_path('other' if fault == 'file_in_parent'
                                              else 'memory', tmp_path)


@pytest.mark.parametrize('link', ['parent', 'file'])
def test_memory_rejects_candidate_links(tmp_path, link):
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = community_bridge.workspace_memory_path('memory', tmp_path)
    if link == 'parent':
        path.parent.rmdir()
        target = path.parent
    else:
        target = path
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation is unavailable on this platform')
    with pytest.raises(ValueError):
        community_bridge.workspace_memory_path('memory', tmp_path)
    assert not list(outside.iterdir())


@pytest.mark.parametrize('setting', [False, True, 'true', 1])
def test_memory_config_is_explicit_and_frozen(tmp_path, monkeypatch, setting):
    descriptor = {'name': 'read_graph', 'inputSchema': {'type': 'object'}}
    raw = json.dumps({'tools': [descriptor]}).encode()
    (tmp_path / 'catalog.json').write_bytes(raw)
    import hashlib
    item = {'id': 'memory', 'command': ['node', '/image/index.js'],
            'container_command': ['node', '/image/index.js'], 'catalog': 'catalog.json',
            'catalog_sha256': hashlib.sha256(raw).hexdigest(), 'workspace_memory': setting,
            'tools': {'read_graph': {'effect': 'read_only', 'retry': 'safe'}}}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'version': 2, 'servers': [item]}), encoding='utf-8')
    monkeypatch.setenv('CODEAGENT_MCP_CONFIG', str(config))
    if type(setting) is not bool:
        with pytest.raises(ValueError):
            McpConfig.from_env()
    else:
        server = McpConfig.from_env().servers[0]
        assert server.workspace_memory is setting
        item['workspace_memory'] = not setting
        config.write_text(json.dumps({'version': 2, 'servers': [item]}), encoding='utf-8')
        assert server.workspace_memory is setting


def test_memory_mutation_cannot_enable_automatic_replay():
    grant = McpGrant('create_entities', json.dumps({'name': 'create_entities',
                    'inputSchema': {'type': 'object'}}), EffectKind.WORKSPACE_WRITE)
    with pytest.raises(ValueError, match='retry=never'):
        McpServer('memory', ('node', '/image/index.js'), ('node', '/image/index.js'),
                  (grant,), workspace_memory=True)


async def test_real_memory_discovery_and_no_host_runtime(tmp_path, monkeypatch):
    server = memory_server('read_graph', 'create_entities')
    ctx = replace(context(tmp_path), timeout_seconds=30)
    catalog = await inspect_server(ctx, server.command)
    assert {'create_entities', 'read_graph', 'add_observations', 'create_relations',
            'search_nodes', 'delete_entities'} <= {t['name'] for t in catalog['tools']}
    async def forbidden(*args, **kwargs):
        raise AssertionError('runtime may not execute on the host')
    import asyncio
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    result = await McpCommunityTool(server, server.grants[0]).execute(ctx, {})
    assert result.is_error and 'Podman' in result.content
