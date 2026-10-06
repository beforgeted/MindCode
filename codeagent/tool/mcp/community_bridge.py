"""SDK-backed stdio probe/call, executed in the selected execution domain.

The controller supplies argv and pinned schemas. No shell, downloads, sampling or
elicitation callbacks. The outer executor owns cancellation and process-tree cleanup.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

MAX_BYTES = 5 * 1024 * 1024
MAX_MEMORY_BYTES = 1024 * 1024


def check_memory_path(path: Path) -> None:
    """State is a regular bounded candidate file, never a runtime or host path."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEMORY_BYTES:
        raise ValueError('workspace memory must be a regular file within its byte limit')


def workspace_memory_path(server_id: str, root: Path = Path('/workspace')) -> Path:
    if not isinstance(server_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', server_id):
        raise ValueError('invalid workspace memory owner')
    for directory in (root, root / 'mcp-state', root / 'mcp-state' / server_id):
        directory.mkdir(exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError('workspace memory directory must not be a link')
    path = root / 'mcp-state' / server_id / 'memory.jsonl'
    check_memory_path(path)
    return path


def runtime_command(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Resolve image executables; never bootstrap code from writable Worker paths."""
    def writable(path: Path) -> bool:
        return any(root == path or root in path.parents
                   for root in (Path('/workspace'), Path('/tmp')))
    executable = shutil.which(argv[0])
    if executable is None:
        raise FileNotFoundError('runtime executable is not installed')
    executable_path = Path(executable).resolve(strict=True)
    if writable(executable_path):
        raise ValueError('runtime executable must reside in the read-only image')
    for i, arg in enumerate(argv[1:], start=1):
        if not arg.startswith(('/', './')):
            continue
        target = Path(arg).resolve()
        # Workspace directories may be service data roots, not startup code.
        interpreter = executable_path.name.startswith(('python', 'node'))
        if writable(target) and (not target.is_dir() or (i == 1 and interpreter)):
            raise ValueError('mutable runtime startup file is unsupported')
    env = {k: v for k, v in os.environ.items()
           if k not in {'PYTHONPATH', 'PYTHONHOME', 'NODE_PATH', 'NODE_OPTIONS',
                        'MEMORY_FILE_PATH'}}
    return [str(executable_path), *argv[1:]], env


def check_schema(schema: Any) -> None:
    from jsonschema.validators import validator_for

    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise ValueError('tool input schema must be an object schema')
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('$ref', '$dynamicRef', '$recursiveRef') and (
                    not isinstance(item, str) or not item.startswith('#')
                ):
                    raise ValueError('remote schema references are unsupported')
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(schema)
    validator_for(schema).check_schema(schema)


def context_schema(kind: str, key: str, descriptor: Any) -> dict:
    if (kind not in ('resource', 'prompt') or not isinstance(key, str)
            or not key or len(key) > 2048 or not isinstance(descriptor, dict)
            or descriptor.get('uri' if kind == 'resource' else 'name') != key
            or len(json.dumps(descriptor).encode()) > 32768):
        raise ValueError('invalid pinned MCP resource/prompt')
    properties, required = {}, []
    if kind == 'prompt':
        args = descriptor.get('arguments', [])
        if not isinstance(args, list) or len(args) > 64:
            raise ValueError('invalid MCP prompt arguments')
        for arg in args:
            if (not isinstance(arg, dict) or not isinstance(arg.get('name'), str)
                    or not arg['name'] or len(arg['name']) > 128 or arg['name'] in properties
                    or type(arg.get('required', False)) is not bool):
                raise ValueError('invalid MCP prompt argument')
            properties[arg['name']] = {'type': 'string', 'maxLength': 4096}
            if arg.get('required'):
                required.append(arg['name'])
    return {'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': False}


def check_context_result(kind: str, key: str, data: dict, limit: int) -> None:
    if kind == 'resource':
        records = data.get('contents', [])
        if any(r.get('uri') != key or not isinstance(r.get('text'), str)
               or 'blob' in r for r in records):
            raise ValueError('only text from the authorized resource URI is supported')
    else:
        records = data.get('messages', [])
        if any(r.get('role') not in ('user', 'assistant')
               or r.get('content', {}).get('type') != 'text'
               or not isinstance(r['content'].get('text'), str) for r in records):
            raise ValueError('only text prompt messages are supported')
    if not records or len(json.dumps(data).encode()) > limit:
        raise ValueError('empty or oversized MCP context result')


async def exchange(payload: dict[str, Any]) -> dict:
    from jsonschema.validators import validator_for
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if payload['action'] == 'call':
        schema = payload['descriptor']['inputSchema']
        check_schema(schema)
        validator_for(schema)(schema).validate(payload['arguments'])
    elif payload['action'] in ('resource', 'prompt'):
        schema = context_schema(payload['action'], payload['key'], payload['descriptor'])
        validator_for(schema)(schema).validate(payload['arguments'])
    argv = payload['command']
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or any(not isinstance(a, str) or not a or '\x00' in a for a in argv)):
        raise ValueError('invalid operator command')
    runtime = payload.get('runtime', False)
    if runtime:
        argv, env = runtime_command(argv)
    else:
        env = dict(os.environ)
    memory_path = None
    if 'workspace_memory' in payload:
        if not runtime:
            raise ValueError('workspace memory requires the runtime domain')
        memory_path = workspace_memory_path(payload['workspace_memory'])
        env['MEMORY_FILE_PATH'] = str(memory_path)
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env,
                                  cwd='/' if runtime else None)
    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        # No callback functions are supplied: SDK rejects sampling/elicitation requests.
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            capabilities = initialized.capabilities.model_dump(mode='json', exclude_none=True)
            catalog: dict[str, Any] = {'protocolVersion': initialized.protocolVersion,
                                       'capabilities': capabilities}

            async def listing(kind: str):
                items = []
                cursor = None
                seen = set()
                for _ in range(16):
                    if kind == 'tools':
                        page = await session.list_tools(cursor=cursor)
                        records = page.tools
                    elif kind == 'resources':
                        page = await session.list_resources(cursor=cursor)
                        records = page.resources
                    elif kind == 'resourceTemplates':
                        page = await session.list_resource_templates(cursor=cursor)
                        records = page.resourceTemplates
                    else:
                        page = await session.list_prompts(cursor=cursor)
                        records = page.prompts
                    items.extend(r.model_dump(mode='json', exclude_none=True) for r in records)
                    if len(items) > 256 or len(json.dumps(items).encode()) > MAX_BYTES:
                        raise ValueError('catalog exceeds limits')
                    cursor = page.nextCursor
                    if not cursor:
                        return items
                    if cursor in seen:
                        raise ValueError('pagination cursor cycle')
                    seen.add(cursor)
                raise ValueError('catalog pagination exceeds 16 pages')

            if 'tools' in capabilities and payload['action'] in ('call', 'inspect', 'probe'):
                catalog['tools'] = await listing('tools')
            if payload['action'] in ('resource', 'prompt'):
                kind = payload['action']
                plural = kind + 's'
                if plural not in capabilities:
                    raise ValueError('authorized MCP capability missing')
                key = payload['key']
                records = await listing(plural)
                matches = [r for r in records if r.get('uri' if kind == 'resource'
                                                      else 'name') == key]
                if matches != [payload['descriptor']]:
                    raise ValueError('authorized resource/prompt descriptor changed')
                if kind == 'resource':
                    result = await session.read_resource(key)
                else:
                    result = await session.get_prompt(key, payload['arguments'])
                data = result.model_dump(mode='json', exclude_none=True)
                check_context_result(kind, key, data, payload['max_response_bytes'])
                return data
            if payload['action'] in ('inspect', 'probe'):
                for kind in ('resources', 'prompts'):
                    if kind in capabilities:
                        catalog[kind] = await listing(kind)
                if 'resources' in capabilities:
                    catalog['resourceTemplates'] = await listing('resourceTemplates')
                if payload['action'] == 'inspect':
                    return catalog
                # Operator probes only; these operations are not exposed as Agent tools.
                method = payload['method']
                if method == 'resources/read':
                    result = await session.read_resource(payload['params']['uri'])
                elif method == 'prompts/get':
                    result = await session.get_prompt(payload['params']['name'],
                                                      payload['params'].get('arguments'))
                else:
                    raise ValueError('unsupported operator probe')
                data = result.model_dump(mode='json', exclude_none=True)
                if len(json.dumps(data).encode()) > MAX_BYTES:
                    raise ValueError('probe output exceeds limit')
                return data
            descriptor = payload['descriptor']
            matches = [t for t in catalog.get('tools', []) if t['name'] == descriptor['name']]
            if (len(matches) != 1 or matches[0]['inputSchema'] != descriptor['inputSchema']):
                raise ValueError('authorized tool schema changed')
            result = await session.call_tool(descriptor['name'], payload['arguments'])
            if memory_path is not None:
                check_memory_path(memory_path)
            data = result.model_dump(mode='json', exclude_none=True)
            if any(c.get('type') != 'text' for c in data.get('content', [])):
                raise ValueError('non-text MCP content is unsupported by runtime tools')
            if len(json.dumps(data).encode()) > payload['max_response_bytes']:
                raise ValueError('MCP result exceeds limit')
            return data


async def main() -> None:
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError('request exceeds limit')
        payload = json.loads(raw)
        async with asyncio.timeout(payload['timeout_seconds']):
            result = await exchange(payload)
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        # SDK task groups wrap a single validation failure. Preserve its classification,
        # never its message/argv/environment; unrelated grouped failures stay opaque.
        while isinstance(exc, ExceptionGroup) and len(exc.exceptions) == 1:
            nested = exc.exceptions[0]
            if not isinstance(nested, Exception):
                break
            exc = nested
        print(json.dumps({'bridge_error': type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    asyncio.run(main())
