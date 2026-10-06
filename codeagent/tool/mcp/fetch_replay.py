"""Offline HTTP transport adapter; official Fetch owns parsing and MCP behavior."""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
from pathlib import Path
from typing import Any


def install(records: dict) -> None:
    httpx: Any = importlib.import_module('httpx')
    readability: Any = importlib.import_module('readabilipy.simple_json')
    # The upstream library otherwise attempts `npm install` when Node is present.
    # Select its existing pure-Python fallback explicitly; never install at runtime.
    readability.have_node = lambda: False

    class ReplayClient:
        def __init__(self, **kwargs):
            if kwargs.get('proxy') is not None:
                raise ValueError('fetch proxies are unsupported')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            if str(url) not in records:
                raise httpx.RequestError('URL has no authorized bounded response')
            record = records[str(url)]
            return httpx.Response(record['status'],
                                  content=base64.b64decode(record['data'], validate=True),
                                  headers={'content-type': record['content_type']},
                                  request=httpx.Request('GET', str(url)))

    # Only this isolated service process is patched; there is no live transport fallback.
    httpx.AsyncClient = ReplayClient


def main() -> None:
    install(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    server = importlib.import_module('mcp_server_fetch.server')
    asyncio.run(server.serve())


if __name__ == '__main__':
    main()
