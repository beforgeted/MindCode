"""Prepare an opt-in matrix from pinned upstream sources and installed official servers.

No network installs, package scripts, model calls, or source modification. Run tests
with the emitted fixture; keep output in an ignored validation directory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.skills.package import load_package
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.mcp.community import inspect_server
from codeagent.workspace.context import WorkspaceContext


async def prepare(args) -> None:
    root, output = args.sources.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for repo in ('jay', 'portable'):
        source = root / repo
        paths = (list(source.glob('*/SKILL.md')) if repo == 'jay'
                 else list((source / 'skills').glob('*/SKILL.md'))
                 + list((source / 'skills/archived').glob('*/SKILL.md')))
        for marker in sorted(paths):
            record = {'repository': repo, 'path': marker.parent.relative_to(source).as_posix()}
            try:
                package = await asyncio.to_thread(load_package, marker.parent)
                record.update(status='pass', sha256=package.digest, files=len(package.files))
            except ValueError as exc:
                record.update(status='fail', reason=str(exc))
            records.append(record)
    node = str(args.node.resolve())
    node_root = args.node_root.resolve() / 'node_modules/@modelcontextprotocol'
    python = sys.executable
    servers = {
        'time': {'command': [python, '-m', 'mcp_server_time'],
                 'container_command': ['python3', '-m', 'mcp_server_time'],
                 'tool': 'get_current_time', 'arguments': {'timezone': 'UTC'}, 'expected': 'UTC'},
        'filesystem': {
            'command': [node, str(node_root / 'server-filesystem/dist/index.js'), '{workspace}'],
            'container_command': ['node', '/opt/mcp/node_modules/@modelcontextprotocol/'
                                  'server-filesystem/dist/index.js', '{workspace}'],
            'tool': 'read_text_file', 'arguments': {'path': '{workspace}/calc.py'},
            'expected': 'add',
        },
        'everything': {
            'command': [node, str(node_root / 'server-everything/dist/index.js'), 'stdio'],
            'container_command': ['node', '/opt/mcp/node_modules/@modelcontextprotocol/'
                                  'server-everything/dist/index.js', 'stdio'],
            'tool': 'echo', 'arguments': {'message': 'MINDCODE_ECOSYSTEM_SENTINEL'},
            'expected': 'MINDCODE_ECOSYSTEM_SENTINEL',
        },
        'git': {'command': [python, '-m', 'mcp_server_git'],
                'container_command': ['python3', '-m', 'mcp_server_git'],
                'tool': 'git_status', 'arguments': {'repo_path': '{workspace}'},
                'expected': 'On branch'},
    }
    if args.memory_node_root is not None:
        memory_root = args.memory_node_root.resolve() / 'node_modules/@modelcontextprotocol'
        servers['memory'] = {
            'command': [node, str(memory_root / 'server-memory/dist/index.js')],
            'container_command': ['node', '/opt/mcp-memory/node_modules/'
                                  '@modelcontextprotocol/server-memory/dist/index.js'],
            'tool': 'read_graph', 'arguments': {}, 'expected': 'entities',
        }
    ctx = ToolExecutionContext(
        agent_run_id='operator', session_id='matrix', tool_run_id='discovery', call_id='discovery',
        workspace=WorkspaceContext.local(output), cancellation=CancellationToken(),
        artifact_store=FileArtifactStore(output / 'artifacts'), timeout_seconds=30,
    )
    for name, server in servers.items():
        command = tuple(a.replace('{workspace}', str(output)) for a in server['command'])
        catalog = await inspect_server(ctx, command)
        raw = json.dumps(catalog, ensure_ascii=False, indent=2).encode()
        path = output / (name + '-catalog.json')
        await asyncio.to_thread(path.write_bytes, raw)
        server.update(catalog=str(path), catalog_sha256=hashlib.sha256(raw).hexdigest())
    fixture = {'sources': str(root), 'servers': servers}
    versions = {name: importlib.metadata.version(name) for name in
                ('PyYAML', 'jsonschema', 'mcp', 'mcp-server-time', 'mcp-server-git')}
    for name in ('filesystem', 'everything'):
        raw = await asyncio.to_thread((node_root / ('server-' + name) / 'package.json').read_text)
        versions['@modelcontextprotocol/server-' + name] = json.loads(raw)['version']
    if args.memory_node_root is not None:
        raw = await asyncio.to_thread((memory_root / 'server-memory/package.json').read_text)
        versions['@modelcontextprotocol/server-memory'] = json.loads(raw)['version']
    await asyncio.to_thread((output / 'fixture.json').write_text,
                            json.dumps(fixture, indent=2), encoding='utf-8')
    await asyncio.to_thread((output / 'skill-census.json').write_text,
                            json.dumps(records, indent=2), encoding='utf-8')
    await asyncio.to_thread((output / 'versions.json').write_text,
                            json.dumps(versions, indent=2), encoding='utf-8')
    print(json.dumps({'skill_packages': len(records),
                      'skill_pass': sum(r['status'] == 'pass' for r in records),
                      'servers': list(servers), 'fixture': str(output / 'fixture.json')}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--node-root', type=Path, required=True)
    parser.add_argument('--memory-node-root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(prepare(parser.parse_args()))


if __name__ == '__main__':
    main()
