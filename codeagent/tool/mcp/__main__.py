from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.mcp.community import McpCommunityTool, McpContextTool, inspect_server
from codeagent.tool.mcp.config import McpConfig
from codeagent.workspace.context import WorkspaceContext


def main() -> None:
    if sys.argv[1:] == ['--configured-tools']:
        config = McpConfig.from_env()
        tools = [{'server': server.id, 'remote': grant.name,
                           'tool': McpCommunityTool(server, grant).name,
                           'effect': grant.effect, 'retry': grant.retry}
                          for server in config.servers for grant in server.grants]
        tools.extend({'server': server.id, 'remote': grant.key, 'kind': grant.kind,
                      'tool': McpContextTool(server, grant).name,
                      'effect': 'read_only', 'retry': 'safe'}
                     for server in config.servers for grant in server.context_grants)
        print(json.dumps(tools, ensure_ascii=False, indent=2))
        return
    parser = argparse.ArgumentParser(description='Operator-only community stdio MCP discovery')
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not 0 < args.timeout <= 120:
        parser.error('explicit command and a timeout in (0,120] required')
    with tempfile.TemporaryDirectory(prefix='mindcode-mcp-probe-') as state:
        ctx = ToolExecutionContext(
            agent_run_id='operator', session_id='discovery', tool_run_id='discovery',
            call_id='discovery', workspace=WorkspaceContext.local(args.workspace.resolve()),
            cancellation=CancellationToken(), artifact_store=FileArtifactStore(Path(state)),
            timeout_seconds=args.timeout,
        )
        catalog = asyncio.run(inspect_server(ctx, tuple(command)))
    raw = json.dumps(catalog, ensure_ascii=False, indent=2).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as stream:
        stream.write(raw)
    print(json.dumps({'catalog_sha256': hashlib.sha256(raw).hexdigest(),
                      'tools': len(catalog.get('tools', [])),
                      'resources': len(catalog.get('resources', [])),
                      'prompts': len(catalog.get('prompts', []))}))


if __name__ == '__main__':
    main()
