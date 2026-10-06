from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from codeagent.tool.effects import EffectKind, RetryPolicy

PROJECT_TOOLS = frozenset({'read_text', 'python_symbols'})


@dataclass(frozen=True, slots=True)
class McpConfig:
    project_tools: tuple[str, ...] = ()
    servers: tuple = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.project_tools, tuple)
                or any(not isinstance(t, str) or t not in PROJECT_TOOLS
                       for t in self.project_tools)
                or len(self.project_tools) != len(set(self.project_tools))):
            raise ValueError('MCP requires a unique allowlist of bundled project tools')
        if (not isinstance(self.servers, tuple) or len(self.servers) > 8
                or len({s.id for s in self.servers}) != len(self.servers)):
            raise ValueError('MCP requires at most 8 unique configured servers')

    @classmethod
    def from_env(cls) -> McpConfig:
        filename = os.environ.get('CODEAGENT_MCP_CONFIG')
        if not filename:
            return cls()
        with Path(filename).open('rb') as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError('MCP configuration exceeds 16KiB')

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate MCP configuration key')
                result[key] = value
            return result

        data = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(data, dict) or type(data.get('version')) is not int
                or data.get('version') not in (1, 2)):
            raise ValueError('MCP requires version 1 and project_tools; commands are unsupported')
        if data['version'] == 1:
            if (set(data) != {'version', 'project_tools'}
                    or not isinstance(data['project_tools'], list)):
                raise ValueError('MCP v1 requires project_tools; commands are unsupported')
            return cls(tuple(data['project_tools']))
        if (set(data) - {'version', 'project_tools', 'servers'}
                or not isinstance(data.get('project_tools', []), list)
                or not isinstance(data.get('servers'), list) or len(data['servers']) > 8):
            raise ValueError('MCP v2 requires a servers list')
        from codeagent.tool.mcp.community import McpContextGrant, McpGrant, McpServer
        servers = []
        for item in data['servers']:
            fields = {'id', 'command', 'container_command', 'catalog', 'catalog_sha256', 'tools'}
            if (not isinstance(item, dict) or not fields <= set(item)
                    or set(item) - fields - {'resources', 'prompts'}
                    or not isinstance(item['catalog'], str) or not isinstance(item['tools'], dict)
                    or len(item['tools']) > 64
                    or not isinstance(item['command'], list)
                    or not isinstance(item['container_command'], list)):
                raise ValueError('invalid MCP server configuration')
            path = Path(item['catalog'])
            if not path.is_absolute():
                path = Path(filename).absolute().parent / path
            with path.open('rb') as stream:
                raw_catalog = stream.read(1024 * 1024 + 1)
            if (len(raw_catalog) > 1024 * 1024
                    or hashlib.sha256(raw_catalog).hexdigest() != item['catalog_sha256']):
                raise ValueError('MCP catalog exceeds limit or differs from its SHA256 pin')
            catalog = json.loads(raw_catalog, object_pairs_hook=unique)
            tools = catalog.get('tools') if isinstance(catalog, dict) else None
            if not isinstance(tools, list) or len(tools) > 256:
                raise ValueError('invalid pinned MCP catalog')
            grants = []
            for name, policy in item['tools'].items():
                if (not isinstance(policy, dict) or set(policy) != {'effect', 'retry'}):
                    raise ValueError('operator must specify effect and retry for each MCP tool')
                matches = [t for t in tools if isinstance(t, dict) and t.get('name') == name]
                if len(matches) != 1:
                    raise ValueError('authorized MCP tool missing or duplicated in catalog')
                grants.append(McpGrant(name, json.dumps(matches[0]),
                                       EffectKind(policy['effect']), RetryPolicy(policy['retry'])))
            context_grants = []
            for kind in ('resource', 'prompt'):
                names = item.get(kind + 's', [])
                records = catalog.get(kind + 's', [])
                if (not isinstance(names, list) or len(names) > 64
                        or any(not isinstance(n, str) for n in names)
                        or len(set(names)) != len(names)
                        or not isinstance(records, list) or len(records) > 256):
                    raise ValueError('invalid MCP resource/prompt allowlist or catalog')
                for name in names:
                    matches = [r for r in records if isinstance(r, dict)
                               and r.get('uri' if kind == 'resource' else 'name') == name]
                    if len(matches) != 1:
                        raise ValueError('authorized MCP resource/prompt missing or duplicated')
                    context_grants.append(McpContextGrant(kind, name, json.dumps(matches[0])))
            servers.append(McpServer(item['id'], tuple(item['command']),
                                     tuple(item['container_command']), tuple(grants),
                                     tuple(context_grants)))
        return cls(tuple(data.get('project_tools', [])), tuple(servers))
