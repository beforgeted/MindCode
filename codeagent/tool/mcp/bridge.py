"""Bounded stdio tools-only MCP client, injected with its server into the execution domain.

One process/session per tool call: no persistent unowned daemons or cross-Worker state.
Sampling, elicitation, resources, URLs and arbitrary server commands are unsupported.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

MAX_ENVELOPE = 5 * 1024 * 1024


def load_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate protocol key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('nonfinite protocol number')

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('expected protocol object')
    return value


async def call_project(payload: dict[str, Any]) -> dict:
    limit = min(MAX_ENVELOPE, payload['max_response_bytes'] + 65536)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-I', '-u', '-c', payload['source'],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=limit,
        env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout

    async def send(message):
        stdin.write(json.dumps(message, ensure_ascii=True).encode() + b'\n')
        await stdin.drain()

    async def rpc(request_id, method, params, byte_limit):
        await send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        for _ in range(8):
            line = await stdout.readline()
            if not line or len(line) > byte_limit or not line.endswith(b'\n'):
                raise ValueError('missing or oversized protocol response')
            response = load_json(line)
            if response.get('jsonrpc') != '2.0':
                raise ValueError('invalid JSON-RPC version')
            if 'method' in response:
                if 'id' in response:
                    # Refuse server-originated callbacks. Never invoke an LLM or fetch a URL.
                    await send({'jsonrpc': '2.0', 'id': response['id'], 'error': {
                        'code': -32601, 'message': 'client callbacks unsupported',
                    }})
                continue
            if type(response.get('id')) is not int or response['id'] != request_id:
                raise ValueError('response identity mismatch')
            if 'error' in response or not isinstance(response.get('result'), dict):
                raise ValueError('protocol error or invalid result')
            return response['result']
        raise ValueError('too many server messages')

    try:
        result = await rpc(1, 'initialize', {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'mindcode', 'version': '0.1.0'},
        }, 65536)
        if (result.get('protocolVersion') not in ('2025-11-25', '2025-06-18')
                or not isinstance(result.get('capabilities'), dict)
                or 'tools' not in result['capabilities']):
            raise ValueError('unsupported server capability or version')
        await send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        discovered = await rpc(2, 'tools/list', {}, 65536)
        # Trusted descriptors, not server annotations, define the accepted capability.
        if discovered.get('tools') != payload['catalog'] or discovered.get('nextCursor'):
            raise ValueError('tool catalog differs from authorized bundled service')
        result = await rpc(3, 'tools/call', {
            'name': payload['tool'], 'arguments': payload['arguments'],
        }, limit)
        content = result.get('content')
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get('type') != 'text'
                or not isinstance(content[0].get('text'), str)
                or type(result.get('isError', False)) is not bool):
            raise ValueError('only a single bounded text tool result is supported')
        if len(content[0]['text'].encode('utf-8')) > payload['max_response_bytes']:
            raise ValueError('tool result exceeds byte limit')
        return result
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def main() -> None:
    payload = load_json(sys.stdin.buffer.read(MAX_ENVELOPE + 1))
    try:
        async with asyncio.timeout(payload['timeout_seconds']):
            result = await call_project(payload)
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'bridge_error': type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    asyncio.run(main())
