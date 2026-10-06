from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from typing import cast

import pytest

from codeagent.config import AppConfig
from codeagent.knowledge import index
from codeagent.knowledge.tool import KnowledgeTool
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from codeagent.tool.builtin.grep import GrepTool
from codeagent.tool.execution_manager import ExecutionScope
from codeagent.tool.models import ToolCall
from tests.test_mcp_project import context


def citation(match):
    return {k: match[k] for k in ('path', 'start_line', 'end_line', 'file_sha256', 'index_version')}


def test_symbols_documents_and_references_use_real_lines(tmp_path):
    (tmp_path / 'calc.py').write_text(
        'class Calculator:\n    async def add(self, x):\n        return x + 1\n',
        encoding='utf-8',
    )
    (tmp_path / 'README.md').write_text('# 计价\n\n支持精确计价和权重。\n', encoding='utf-8')
    result = index.invoke(tmp_path, 'search', {'query': 'Calculator add', 'kind': 'symbol'})
    match = result['result']['matches'][0]
    assert match['name'] == 'Calculator.add' and match['start_line'] == 2
    read = index.invoke(tmp_path, 'get', citation(match), result['cache'])
    assert read['result']['excerpt'] == '    async def add(self, x):\n        return x + 1'
    assert read['stats']['reused'] == 2 and read['stats']['rebuilt'] == 0
    assert match['file_sha256'] == hashlib.sha256((tmp_path / 'calc.py').read_bytes()).hexdigest()
    doc = index.invoke(tmp_path, 'search', {'query': '精确计价', 'kind': 'document'})
    assert doc['result']['matches'][0]['start_line'] == 3
    assert doc['result']['matches'][0]['excerpt'] == '支持精确计价和权重。'


@pytest.mark.parametrize('mutation', ['modify_same_mtime', 'delete', 'rename', 'branch_content'])
def test_content_changes_invalidate_and_incrementally_rebuild(tmp_path, mutation):
    source = tmp_path / 'calc.py'
    source.write_text('def old(): pass\n')
    (tmp_path / 'stable.py').write_text('def stable(): pass\n')
    original = index.invoke(tmp_path, 'search', {'query': 'old', 'kind': 'symbol'})
    match = original['result']['matches'][0]
    info = source.stat()
    if mutation == 'delete':
        source.unlink()
    elif mutation == 'rename':
        source.rename(tmp_path / 'renamed.py')
    else:
        source.write_text('def new(): pass\n')
        if mutation == 'modify_same_mtime':
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    updated = index.invoke(tmp_path, 'search', {'query': 'old', 'kind': 'symbol'},
                           original['cache'])
    assert updated['index_version'] != original['index_version']
    assert updated['stats']['reused'] == 1
    if mutation == 'rename':
        assert updated['result']['matches'][0]['path'] == 'renamed.py'
    else:
        assert updated['result']['matches'] == []
    with pytest.raises(index.StaleIndex):
        index.invoke(tmp_path, 'get', citation(match), original['cache'])
    with pytest.raises(index.StaleIndex):
        index.invoke(tmp_path, 'search', {'query': 'old',
                                        'expected_version': original['index_version']})


@pytest.mark.parametrize('name', [
    '.private-docs/secret.md', '.env', 'folder/.env.prod', '.codeagent/data.txt',
    'credentials.json', 'folder/secrets.json', 'server.pem', 'id_ed25519',
    'node_modules/package/README.md', 'build/generated.txt',
])
def test_protected_paths_never_read_or_return(tmp_path, monkeypatch, name):
    secret = tmp_path / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('PRIVATE_SENTINEL', encoding='utf-8')
    (tmp_path / 'public.md').write_text('public text')
    real = index.read_file
    def guarded(root, path):
        assert path != name
        return real(root, path)
    monkeypatch.setattr(index, 'read_file', guarded)
    result = index.invoke(tmp_path, 'search', {'query': 'PRIVATE_SENTINEL'})
    assert result['result']['matches'] == [] and result['stats']['files'] == 1
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_root_nested_ignore_and_conservative_negation(tmp_path):
    for name in ['hidden.txt', 'docs/hidden.txt', 'docs/notes.md', 'docs/cache/a.md',
                 'keep/a.py', 'keep/deep/log.txt']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('secret_keyword')
    (tmp_path / '.gitignore').write_text('/hidden.txt\n**/log.txt\n!hidden.txt\n')
    (tmp_path / 'docs/.gitignore').write_text('cache/\nnotes.md\n')
    result = index.invoke(tmp_path, 'search', {'query': 'secret_keyword'})
    assert set(result['cache']['files']) == {'docs/hidden.txt', 'keep/a.py'}
    old_version = result['index_version']
    (tmp_path / '.gitignore').write_text('**/log.txt\n')
    updated = index.invoke(tmp_path, 'search', {'query': 'secret_keyword'}, result['cache'])
    assert 'hidden.txt' in updated['cache']['files']
    assert updated['index_version'] != old_version


def test_unsupported_ignore_syntax_fails_closed(tmp_path):
    (tmp_path / '.gitignore').write_text(r'escaped\ space.md')
    (tmp_path / 'escaped space.md').write_text('private')
    with pytest.raises(ValueError, match='ignore'):
        index.invoke(tmp_path, 'search', {'query': 'private'})


@pytest.mark.parametrize('kind', ['file_link', 'directory_link', 'hardlink'])
def test_links_cannot_leak_outside_source(tmp_path, kind):
    root = tmp_path / 'repo'
    root.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('OUTSIDE_SENTINEL')
    try:
        if kind == 'hardlink':
            os.link(outside, root / 'linked.md')
        elif kind == 'file_link':
            (root / 'linked.md').symlink_to(outside)
        else:
            (root / 'linked').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('link creation unavailable')
    result = index.invoke(root, 'search', {'query': 'OUTSIDE_SENTINEL'})
    assert not result['result']['matches'] and not result['cache']['files']


def test_invalid_python_is_observable_binary_skipped_and_scope_limits_fail(tmp_path, monkeypatch):
    (tmp_path / 'bad.py').write_text('def bad(:\n')
    (tmp_path / 'binary.bin').write_bytes(b'\x00private')
    result = index.invoke(tmp_path, 'search', {'query': 'bad', 'kind': 'symbol'})
    assert not result['result']['matches'] and result['stats']['invalid_python'] == 1
    assert set(result['cache']['files']) == {'bad.py'}
    monkeypatch.setattr(index, 'MAX_FILE_BYTES', 5)
    with pytest.raises(OverflowError):
        index.invoke(tmp_path, 'search', {'query': 'bad'})


@pytest.mark.parametrize('args', [
    {}, {'query': ''}, {'query': 'x', 'limit': True}, {'query': 'x', 'limit': 21},
    {'query': 'x', 'kind': 'network'}, {'query': 'x', 'path': '/etc/passwd'},
    {'query': 'x', 'expected_version': 'old'}, {'query': 'x' * 257},
])
async def test_invalid_queries_never_start_process(tmp_path, monkeypatch, args):
    ctx = context(tmp_path)
    async def forbidden(*a, **kw):
        raise AssertionError('invalid query started a process')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    assert (await KnowledgeTool('search').execute(ctx, args)).is_error


async def test_actual_local_helper_freeze_get_stale_and_fallback(tmp_path):
    ctx = context(tmp_path)
    search = KnowledgeTool('search')
    (ctx.workspace.root / 'ast.py').write_text('raise RuntimeError("workspace import")')
    result = await search.execute(ctx, {'query': 'add', 'kind': 'symbol'})
    assert not result.is_error
    data = json.loads(result.content)
    match = data['result']['matches'][0]
    read = await KnowledgeTool('get').execute(ctx, citation(match))
    assert not read.is_error and 'return a + b' in read.content
    (ctx.workspace.root / 'calc.py').write_text('def sum_values(a, b): return a + b\n')
    stale = await KnowledgeTool('get').execute(ctx, citation(match))
    assert stale.is_error and '失效' in stale.content
    fallback = await GrepTool().execute(ctx, {'pattern': 'sum_values'})
    assert not fallback.is_error and 'sum_values' in fallback.content
    assert not any(p.name.startswith('knowledge') for p in ctx.workspace.root.iterdir())


async def test_session_opt_in_and_agent_permission_gate(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    cfg = AppConfig(ctx.workspace.root, tmp_path / 'session', use_stub_llm=True)
    disabled = AgentSession(cfg, llm_client=StubLlmClient([]))
    assert 'knowledge_search' not in disabled.registry.names()
    async with AgentSession(replace(cfg, knowledge_enabled=True),
                            llm_client=StubLlmClient([])) as session:
        assert 'knowledge_search' in session.registry.names()
        scope = ExecutionScope('run', 'session', ctx.workspace, ctx.cancellation, cfg.profile,
                               allowed_tools=('grep',))
        async def forbidden(*a, **kw):
            raise AssertionError('unauthorized index process')
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
        result = await session.execution_manager.execute_batch(
            scope, [ToolCall('one', 'knowledge_search', {'query': 'add'})])
        assert result.results[0].is_error and '未获准' in result.results[0].content


def test_config_is_explicit_and_strict(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEAGENT_KNOWLEDGE', '1')
    assert AppConfig.from_env(tmp_path).knowledge_enabled
    monkeypatch.setenv('CODEAGENT_KNOWLEDGE', 'yes')
    with pytest.raises(ValueError, match='0 or 1'):
        AppConfig.from_env(tmp_path)
    with pytest.raises(ValueError, match='boolean'):
        AppConfig(tmp_path, tmp_path / 'state', knowledge_enabled=cast(bool, 1))


@pytest.mark.parametrize('change', [
    {'path': '../secret.txt'}, {'path': '.private-docs/secret.md'}, {'path': 'C:/secret'},
    {'start_line': True}, {'end_line': 42}, {'file_sha256': 'invalid'}, {'extra': 'write'},
])
async def test_invalid_citation_never_launches_reader(tmp_path, monkeypatch, change):
    ctx = context(tmp_path)
    args = {'path': 'calc.py', 'start_line': 1, 'end_line': 2,
            'file_sha256': '0' * 64, 'index_version': '0' * 64, **change}
    async def forbidden(*a, **kw):
        raise AssertionError('invalid citation started helper')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    assert (await KnowledgeTool('get').execute(ctx, args)).is_error


def test_symbol_rank_not_dominated_by_doc_mentions(tmp_path):
    (tmp_path / 'aaa.md').write_text(('WantedClass mentions\n\n' * 50), encoding='utf-8')
    (tmp_path / 'zzz.py').write_text('class WantedClass: pass\n')
    result = index.invoke(tmp_path, 'search', {'query': 'WantedClass', 'limit': 1})
    assert result['result']['matches'][0]['path'] == 'zzz.py'
    assert result['result']['matches'][0]['kind'] == 'symbol'
    assert result['result']['total_matches'] > 1


@pytest.mark.parametrize('limit', ['MAX_FILES', 'MAX_TOTAL_BYTES', 'MAX_ENTRIES', 'MAX_ENVELOPE'])
def test_resource_limits_never_return_partial_success(tmp_path, monkeypatch, limit):
    (tmp_path / 'a.py').write_text('def first(): pass\n')
    (tmp_path / 'b.py').write_text('def second(): pass\n')
    monkeypatch.setattr(index, limit, 1)
    with pytest.raises(OverflowError):
        index.invoke(tmp_path, 'search', {'query': 'first'})


async def test_bounded_result_and_scope_change_do_not_expose_old_cache(tmp_path):
    ctx = context(tmp_path)
    tool = KnowledgeTool('search')
    tiny = await tool.execute(replace(ctx, max_output_bytes=10), {'query': 'add'})
    assert tiny.is_error and 'limit' in tiny.content
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'different.py').write_text('class Different: pass\n')
    from codeagent.workspace.context import WorkspaceContext
    switched = await tool.execute(replace(ctx, workspace=WorkspaceContext.local(other)),
                                  {'query': 'add'})
    data = json.loads(switched.content)
    assert data['result']['matches'] == [] and data['stats']['rebuilt'] == 1


async def test_local_timeout_clears_process_and_next_query_still_works(tmp_path):
    ctx = context(tmp_path)
    slow = KnowledgeTool('search')
    slow._source = 'import time\ntime.sleep(30)'
    with pytest.raises(TimeoutError):
        await slow.execute(replace(ctx, timeout_seconds=.1), {'query': 'add'})
    next_query = await KnowledgeTool('search').execute(ctx, {'query': 'add'})
    assert not next_query.is_error
