"""Controlled HTTP authorization and real upstream Fetch's offline semantics."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from dataclasses import replace

import pytest

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.execution import fetch_helper
from codeagent.execution.fetch import ControlledFetcher, FetchPolicy
from codeagent.infra.cancellation import CancellationToken
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.mcp import community_bridge, fetch_replay
from codeagent.tool.mcp.community import McpCommunityTool, McpGrant, McpServer, inspect_server
from tests.test_mcp_project import context

HOST = 'example.org'
URL = 'https://example.org/page'
ROBOTS = 'https://example.org/robots.txt'


def record(body=b'hello', *, url=URL, status=200, content_type='text/plain'):
    return {'url': url, 'ip': '93.184.216.34', 'status': status,
            'data': base64.b64encode(body).decode(), 'bytes': len(body),
            'sha256': hashlib.sha256(body).hexdigest(), 'content_type': content_type}


async def fetch_server(tmp_path, hosts=(HOST,), ctx=None):
    if importlib.util.find_spec('mcp_server_fetch') is None:
        pytest.skip('requires pinned official Fetch source and dependencies')
    catalog = await inspect_server(replace(ctx or context(tmp_path), timeout_seconds=30),
                                   (sys.executable, '-m', 'mcp_server_fetch'))
    grant = McpGrant('fetch', json.dumps(catalog['tools'][0]),
                     EffectKind.READ_ONLY, RetryPolicy.NEVER)
    return McpServer('fetch', (sys.executable, '-m', 'mcp_server_fetch'),
                     ('python3', '-m', 'mcp_server_fetch'), (grant,),
                     fetch_policy=FetchPolicy(hosts))


@pytest.mark.parametrize('kwargs', [
    {'hosts': []}, {'hosts': ()}, {'hosts': ('127.0.0.1',)},
    {'hosts': ('Example.org',)}, {'hosts': (HOST, HOST)}, {'max_bytes': True},
    {'max_bytes': 1048577}, {'max_redirects': True}, {'max_redirects': 4},
    {'timeout_seconds': float('nan')}, {'timeout_seconds': True},
])
def test_fetch_policy_is_bounded_and_operator_owned(kwargs):
    with pytest.raises(ValueError):
        FetchPolicy(**({'hosts': (HOST,)} | kwargs))


@pytest.mark.parametrize('url', [
    'http://example.org/page', 'https://127.0.0.1/page', 'https://example.org:444/page',
    'https://user:secret@example.org/page', 'https://example.org/page?token=secret',
    'https://example.org/page#x', 'https://other.org/page', 'file:///etc/passwd',
])
async def test_denied_url_never_reaches_network(tmp_path, monkeypatch, url):
    import codeagent.execution.fetch as module
    async def forbidden(*args, **kwargs):
        raise AssertionError('network must not start')
    monkeypatch.setattr(module, 'run_bounded', forbidden)
    with pytest.raises(ValueError):
        await ControlledFetcher(FetchPolicy((HOST,)), FileArtifactStore(tmp_path)).fetch(
            url, CancellationToken(), forbidden)


async def test_redirect_is_authorized_before_new_connection(tmp_path, monkeypatch):
    fetcher = ControlledFetcher(FetchPolicy((HOST,)), FileArtifactStore(tmp_path))
    calls = []
    async def hop(url, maximum, cancellation):
        calls.append(url)
        if url == ROBOTS:
            return record(b'User-agent: *\nAllow: /', url=ROBOTS)
        return {'url': url, 'status': 302, 'location': 'https://other.org/secret'}
    async def authorize(url, records):
        assert url == URL and ROBOTS in records
    monkeypatch.setattr(fetcher, 'hop', hop)
    with pytest.raises(ValueError, match='allowlisted'):
        await fetcher.fetch(URL, CancellationToken(), authorize)
    assert calls == [ROBOTS, URL]


async def test_robots_deny_happens_before_page_request(tmp_path, monkeypatch):
    fetcher = ControlledFetcher(FetchPolicy((HOST,)), FileArtifactStore(tmp_path))
    calls = []
    async def hop(url, maximum, cancellation):
        calls.append(url)
        return record(b'User-agent: *\nDisallow: /', url=ROBOTS)
    async def authorize(url, records):
        raise ValueError('official parser denies')
    monkeypatch.setattr(fetcher, 'hop', hop)
    with pytest.raises(ValueError):
        await fetcher.fetch(URL, CancellationToken(), authorize)
    assert calls == [ROBOTS]


@pytest.mark.parametrize('fault', ['declared_oversize', 'stream_oversize', 'encoding', 'short'])
def test_download_limit_precedes_decode_or_extraction(monkeypatch, fault):
    class Response:
        status = 200
        def getheader(self, name, default=None):
            return {'Content-Length': ('99' if fault == 'declared_oversize'
                                       else '3' if fault == 'short' else None),
                    'Content-Encoding': 'gzip' if fault == 'encoding' else 'identity'}.get(
                        name, default)
        def read(self, n):
            if fault == 'stream_oversize':
                return b'x' * n
            return b''
    class Connection:
        def __init__(self, *args):
            self.closed = False
        def request(self, *args, **kwargs):
            assert set(kwargs['headers']) == {'Host', 'Accept-Encoding', 'User-Agent'}
        def getresponse(self):
            return Response()
        def close(self):
            self.closed = True
    import codeagent.execution.download_helper as helper
    monkeypatch.setattr(fetch_helper, 'PinnedHTTPSConnection', Connection)
    monkeypatch.setattr(fetch_helper, 'resolve_public', lambda host: '93.184.216.34')
    # Production resolution rejects every private DNS record, not just selected IP.
    with pytest.raises(ValueError):
        helper.public_address('127.0.0.1')
    with pytest.raises(ValueError):
        fetch_helper.fetch_hop({'url': URL, 'hosts': (HOST,),
                               'max_bytes': 8 if fault == 'short' else 2,
                               'timeout_seconds': 1, 'user_agent': 'test'})


def test_mixed_public_private_dns_prevents_connection(monkeypatch):
    import socket

    import codeagent.execution.download_helper as helper
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (2, 1, 6, '', ('93.184.216.34', 443)), (2, 1, 6, '', ('127.0.0.1', 443))])
    def forbidden(*args, **kwargs):
        raise AssertionError('mixed DNS must not open a connection')
    monkeypatch.setattr(fetch_helper, 'PinnedHTTPSConnection', forbidden)
    monkeypatch.setattr(fetch_helper, 'resolve_public', helper.resolve_public)
    with pytest.raises(ValueError, match='non-public'):
        fetch_helper.fetch_hop({'url': URL, 'hosts': (HOST,), 'max_bytes': 8,
                               'timeout_seconds': 1, 'user_agent': 'test'})


async def test_fetch_config_roundtrip_freezes_policy(tmp_path, monkeypatch):
    from codeagent.tool.mcp.config import McpConfig
    server = await fetch_server(tmp_path)
    raw = json.dumps({'tools': [json.loads(server.grants[0].descriptor_json)]}).encode()
    catalog = tmp_path / 'catalog.json'
    catalog.write_bytes(raw)
    item = {'id': 'fetch', 'command': list(server.command),
            'container_command': list(server.container_command), 'catalog': 'catalog.json',
            'catalog_sha256': hashlib.sha256(raw).hexdigest(),
            'tools': {'fetch': {'effect': 'read_only', 'retry': 'never'}},
            'fetch': {'hosts': [HOST], 'max_bytes': 1024}}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'version': 2, 'servers': [item]}))
    monkeypatch.setenv('CODEAGENT_MCP_CONFIG', str(config))
    frozen = McpConfig.from_env().servers[0]
    item['fetch']['hosts'].append('other.org')
    config.write_text(json.dumps({'version': 2, 'servers': [item]}))
    assert frozen.fetch_policy == FetchPolicy((HOST,), max_bytes=1024)


async def test_audit_failure_prevents_network(tmp_path, monkeypatch):
    class BrokenStore(FileArtifactStore):
        async def save_text(self, *args, **kwargs):
            raise OSError('audit unavailable')
    import codeagent.execution.fetch as module
    async def forbidden(*args, **kwargs):
        raise AssertionError('network must not start')
    monkeypatch.setattr(module, 'run_bounded', forbidden)
    with pytest.raises(OSError):
        await ControlledFetcher(FetchPolicy((HOST,)), BrokenStore(tmp_path)).hop(
            URL, 100, CancellationToken())


@pytest.mark.parametrize('fault', ['retry', 'prompt', 'command', 'state'])
def test_fetch_grant_cannot_expand_authority(fault):
    grant = McpGrant('fetch', json.dumps({'name': 'fetch', 'inputSchema': {'type': 'object'}}),
                     retry=RetryPolicy.SAFE if fault == 'retry' else RetryPolicy.NEVER)
    from codeagent.tool.mcp.community import McpContextGrant
    context_grants = ((McpContextGrant('prompt', 'fetch', json.dumps({'name': 'fetch'})),)
                      if fault == 'prompt' else ())
    with pytest.raises(ValueError):
        McpServer('fetch', ('python', '-m', 'mcp_server_fetch'),
                  ('python3', '-m', 'other') if fault == 'command'
                  else ('python3', '-m', 'mcp_server_fetch'), (grant,), context_grants,
                  workspace_memory=fault == 'state', fetch_policy=FetchPolicy((HOST,)))


async def test_real_fetch_catalog_and_offline_text_paging(tmp_path):
    server = await fetch_server(tmp_path)
    payload = {'action': 'call', 'runtime': False, 'descriptor': json.loads(
        server.grants[0].descriptor_json), 'arguments': {'url': URL, 'max_length': 5,
        'start_index': 6}, 'command': list(server.command), 'max_response_bytes': 4096}
    # Local test invokes actual pinned community server through SDK, with offline transport only.
    import asyncio
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as name:
        receipt = Path(name) / 'responses.json'
        receipt.write_text(json.dumps({ROBOTS: record(b'User-agent: *\nAllow: /', url=ROBOTS),
                                      URL: record(b'first second third')}))
        payload['command'] = [sys.executable, '-I', '-c',
                              await asyncio.to_thread(Path(fetch_replay.__file__).read_text),
                              str(receipt)]
        result = await community_bridge.exchange(payload)
    assert not result.get('isError')
    text = result['content'][0]['text']
    assert 'secon' in text and 'start_index of 11' in text
    assert McpCommunityTool(server, server.grants[0]).retry_policy is RetryPolicy.NEVER


def test_html_fallback_never_attempts_node_or_npm(monkeypatch):
    if importlib.util.find_spec('mcp_server_fetch') is None:
        pytest.skip('requires pinned official Fetch dependencies')
    httpx = importlib.import_module('httpx')
    readability = importlib.import_module('readabilipy.simple_json')
    monkeypatch.setattr(httpx, 'AsyncClient', httpx.AsyncClient)
    monkeypatch.setattr(readability, 'have_node', readability.have_node)
    def forbidden(*args, **kwargs):
        raise AssertionError('HTML extraction must not start Node/npm')
    monkeypatch.setattr(readability, 'run_npm_install', forbidden)
    monkeypatch.setattr(readability.subprocess, 'run', forbidden)
    fetch_replay.install({})
    assert not readability.have_node()
    content = readability.simple_json_from_html_string(
        '<html><body><h1>Bounded</h1><p>Offline article content.</p></body></html>',
        use_readability=True)
    assert 'Offline article content.' in content['content']
