"""One pinned HTTPS hop. Never follows redirects or loads ambient credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import sys

from codeagent.execution.download_helper import PinnedHTTPSConnection, resolve_public, validate_url


def fetch_hop(request: dict) -> dict:
    host, path = validate_url(request['url'], tuple(request['hosts']))
    address = resolve_public(host)
    connection = PinnedHTTPSConnection(host, address, request['timeout_seconds'])
    try:
        connection.request('GET', path, headers={
            'Host': host, 'Accept-Encoding': 'identity', 'User-Agent': request['user_agent'],
        })
        response = connection.getresponse()
        status = response.status
        record = {'url': request['url'], 'ip': address, 'status': status}
        if status in (301, 302, 303, 307, 308):
            location = response.getheader('Location', '')
            if len(location) > 4096:
                raise ValueError('oversized redirect location')
            return {**record, 'location': location}
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('encoded responses are unsupported')
        maximum = request['max_bytes']
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > maximum):
            raise ValueError('fetch exceeds byte limit')
        data = bytearray()
        while chunk := response.read(min(65536, maximum - len(data) + 1)):
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError('fetch exceeds byte limit')
        if length is not None and len(data) != int(length):
            raise ValueError('incomplete fetch response')
        content_type = response.getheader('Content-Type', 'text/plain')
        if len(content_type) > 256:
            raise ValueError('oversized content type')
        return {**record, 'data': base64.b64encode(data).decode('ascii'),
                'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                'content_type': content_type}
    finally:
        connection.close()


def main() -> None:
    try:
        result = fetch_hop(json.load(sys.stdin))
        code = 0
    except Exception as exc:
        result = {'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
        code = 1
    print(json.dumps(result), flush=True)
    raise SystemExit(code)


if __name__ == '__main__':
    main()
