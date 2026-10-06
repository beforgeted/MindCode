"""Official Fetch in offline Podman; controlled network and lifecycle negatives."""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace

import pytest

from codeagent.execution.fetch import ControlledFetcher, FetchPolicy
from codeagent.execution.models import SandboxError
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.infra.cancellation import CancelledByUser
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.mcp.community import McpCommunityTool
from codeagent.tool.sandbox import SandboxTools
from tests.test_mcp_fetch import HOST, ROBOTS, URL, fetch_server, record
from tests.test_mcp_project import context

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_FETCH_IMAGE'),
    reason='requires independent Ubuntu VM and pinned official Fetch runtime image',
)


async def domain(tmp_path, *, hosts=(HOST,), limit=1024 * 1024):
    ctx = context(tmp_path)
    server = await fetch_server(tmp_path, hosts, ctx)
    server = replace(server, fetch_policy=FetchPolicy(hosts, max_bytes=limit))
    manager = PodmanSandboxManager(os.environ['MINDCODE_FETCH_IMAGE'])
    handle = await manager.open(TreeSnapshot((SnapshotEntry('keep.txt', b'original'),)))
    ctx = replace(ctx, command_executor=SandboxExecutor(manager, handle, ctx.workspace.root),
                  timeout_seconds=60)
    return manager, handle, ctx, McpCommunityTool(server, server.grants[0])


async def clean(manager, handle):
    assert not manager._handles
    with pytest.raises(SandboxError):
        await manager.seal(handle)


async def test_official_fetch_html_raw_paging_and_no_live_fallback(tmp_path, monkeypatch):
    manager, handle, ctx, tool = await domain(tmp_path)
    assert isinstance(ctx.command_executor, SandboxExecutor)
    html = (b'<html><head><title>Compatibility</title></head><body><article><h1>Fetch</h1>'
            b'<p>Official implementation extracts this bounded synthetic page.</p>'
            b'<p>Transport authority remains outside the offline service.</p>'
            b'</article></body></html>')
    async def hop(self, url, maximum, cancellation):
        return (record(b'User-agent: *\nAllow: /', url=ROBOTS) if url == ROBOTS
                else record(html, content_type='text/html'))
    monkeypatch.setattr(ControlledFetcher, 'hop', hop)
    try:
        result = await SandboxTools(ctx.command_executor).execute(tool, ctx, {'url': URL})
        assert not result.is_error and 'Official implementation' in result.content
        raw = await tool.execute(ctx, {'url': URL, 'raw': True, 'start_index': 6, 'max_length': 12})
        assert not raw.is_error and 'start_index of 18' in raw.content
        normalized = await tool.execute(ctx, {'url': 'https://example.org:443/a/../page',
                                              'raw': True, 'max_length': 12})
        assert not normalized.is_error
        assert 'Contents of https://example.org/page:' in normalized.content
        blocked = await manager.execute(handle,
            "python3 -I -c 'import socket; socket.create_connection((\"1.1.1.1\",443),1)'")
        assert blocked.returncode != 0
        leftover = await manager.execute(handle, 'find /tmp -name "mcp-fetch-*"')
        assert leftover.returncode == 0 and not leftover.stdout.strip()
        assert (await manager.seal(handle)).entries == (SnapshotEntry('keep.txt', b'original'),)
    finally:
        await manager.aclose()


@pytest.mark.parametrize('fault', ['robots', 'redirect', 'private_dns', 'schema'])
async def test_fetch_rejection_discards_domain_without_page_escape(tmp_path, monkeypatch, fault):
    manager, handle, ctx, tool = await domain(tmp_path)
    calls = []
    if fault == 'schema':
        import json
        descriptor = json.loads(tool.grant.descriptor_json)
        descriptor['inputSchema']['title'] = 'Changed schema'
        tool = McpCommunityTool(tool.server,
                                replace(tool.grant, descriptor_json=json.dumps(descriptor)))
    async def hop(self, url, maximum, cancellation):
        calls.append(url)
        if fault == 'private_dns':
            raise ValueError('DNS contains a non-public address')
        if url == ROBOTS:
            rules = b'User-agent: *\n' + (b'Disallow: /' if fault == 'robots' else b'Allow: /')
            return record(rules, url=ROBOTS)
        return {'url': url, 'status': 302, 'location': 'https://other.org/escape'}
    monkeypatch.setattr(ControlledFetcher, 'hop', hop)
    try:
        with pytest.raises(ValueError):
            await tool.execute(ctx, {'url': URL})
        await clean(manager, handle)
        assert calls == ([] if fault == 'schema' else
                         [ROBOTS, URL] if fault == 'redirect' else [ROBOTS])
    finally:
        await manager.aclose()


@pytest.mark.parametrize('fault', ['timeout', 'cancel'])
async def test_fetch_network_wait_timeout_or_cancel_closes_worker(tmp_path, monkeypatch, fault):
    manager, handle, ctx, tool = await domain(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()
    async def hop(self, *args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    monkeypatch.setattr(ControlledFetcher, 'hop', hop)
    ctx = replace(ctx, timeout_seconds=5 if fault == 'timeout' else 30)
    task = asyncio.create_task(tool.execute(ctx, {'url': URL}))
    try:
        await asyncio.wait_for(started.wait(), 15)
        if fault == 'cancel':
            task.cancel()
        with pytest.raises((TimeoutError, asyncio.CancelledError, CancelledByUser)):
            await task
        assert stopped.is_set()
        await clean(manager, handle)
    finally:
        await manager.aclose()


@pytest.mark.skipif(not os.environ.get('MINDCODE_FETCH_LIVE_URL'),
                    reason='requires fixed official public HTTPS fixture')
@pytest.mark.parametrize('fault', ['none', 'oversize'])
async def test_real_https_through_official_fetch_in_offline_worker(tmp_path, fault):
    from urllib.parse import urlsplit
    url = os.environ['MINDCODE_FETCH_LIVE_URL']
    manager, handle, ctx, tool = await domain(tmp_path, hosts=(urlsplit(url).hostname,),
                                            limit=1024 if fault == 'oversize' else 1024 * 1024)
    try:
        if fault == 'oversize':
            with pytest.raises(ValueError, match='byte limit'):
                await tool.execute(ctx, {'url': url, 'raw': True})
            await clean(manager, handle)
        else:
            result = await tool.execute(ctx, {'url': url, 'raw': True, 'max_length': 1000})
            expected = os.environ.get('MINDCODE_FETCH_LIVE_EXPECTED', 'Fetch MCP Server')
            assert not result.is_error and expected in result.content
            assert (await manager.seal(handle)).entries == (SnapshotEntry('keep.txt', b'original'),)
    finally:
        await manager.aclose()
