"""Trusted isolated downloader: stdlib only, no workspace imports or credentials."""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
from urllib.parse import urljoin, urlsplit


def canonical_host(host: str) -> str:
    if (not isinstance(host, str) or len(host) > 253 or host != host.lower()
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
            or any(len(p) > 63 or p.startswith('-') or p.endswith('-') for p in host.split('.'))):
        raise ValueError("allowlist requires canonical DNS hostnames")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("IP literals are not allowed")


def validate_url(url: str, hosts: tuple[str, ...]) -> tuple[str, str]:
    if (not isinstance(url, str) or not url or len(url) > 4096
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url) or '\\' in url):
        raise ValueError("invalid download URL")
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    canonical_host(host)
    if (parsed.scheme != 'https' or parsed.netloc not in (host, host + ':443')
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or '?' in url or '#' in url):
        raise ValueError("requires HTTPS:443 without credentials/query/fragment")
    if host not in hosts:
        raise ValueError("download host is not allowlisted")
    return host, parsed.path or '/'


def public_address(raw: str) -> str:
    address = ipaddress.ip_address(raw)
    # Be conservative across supported Python versions with different IANA snapshots.
    denied = ('192.0.0.0/24', '192.88.99.0/24', '64:ff9b::/96', '64:ff9b:1::/48', '2001::/23')
    if any(address in ipaddress.ip_network(network) for network in denied):
        raise ValueError('special-purpose addresses are not allowed')
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address.is_link_local):
        raise ValueError("DNS contains a non-public address")
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None or address.sixtofour is not None
        or address.teredo is not None or '%' in raw
    ):
        raise ValueError("IPv6 transition/scoped addresses are not allowed")
    return str(address)


def resolve_public(host: str) -> str:
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not records or len(records) > 64:
        raise ValueError("invalid DNS response")
    addresses = [public_address(str(record[4][0])) for record in records]
    # Prefer IPv4 when available; all records must pass, not just the selected one.
    return next((a for a in addresses if ':' not in a), addresses[0])


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        context = ssl.create_default_context()
        context.set_alpn_protocols(['http/1.1'])
        super().__init__(host, 443, timeout=timeout, context=context)
        self.address = address
        self.tls_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def download(request: dict, hops: list[dict]) -> bytes:
    url = request['url']
    hosts = tuple(request['hosts'])
    maximum = request['max_bytes']
    redirects = request['max_redirects']
    for index in range(redirects + 1):
        host, path = validate_url(url, hosts)
        address = resolve_public(host)
        hop = {'url': url, 'ip': address, 'status': None}
        hops.append(hop)
        connection = PinnedHTTPSConnection(host, address, request['timeout_seconds'])
        try:
            connection.request('GET', path, headers={
                'Host': host, 'Accept-Encoding': 'identity', 'User-Agent': 'MindCode-Download/1',
            })
            response = connection.getresponse()
            hop['status'] = response.status
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if (not location or len(location) > 4096 or index == redirects
                        or any(ord(c) <= 32 or ord(c) >= 127 for c in location)):
                    raise ValueError("redirect limit or invalid location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("download HTTP status is not 200")
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError("encoded responses are not supported")
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > maximum):
                raise ValueError("download exceeds byte limit")
            data = bytearray()
            while chunk := response.read(min(65536, maximum - len(data) + 1)):
                data.extend(chunk)
                if len(data) > maximum:
                    raise ValueError("download exceeds byte limit")
            if length is not None and len(data) != int(length):
                raise ValueError("incomplete response")
            if hashlib.sha256(data).hexdigest() != request['sha256']:
                raise ValueError("download SHA256 mismatch")
            return bytes(data)
        finally:
            connection.close()
    raise ValueError("redirect limit")


def main() -> None:
    hops: list[dict] = []
    try:
        request = json.load(sys.stdin)
        data = download(request, hops)
        result = {'data': base64.b64encode(data).decode('ascii'), 'hops': hops}
        code = 0
    except Exception as exc:
        # Only our fixed ValueError messages are surfaced; remote/library text is untrusted.
        result = {'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                  'hops': hops}
        code = 1
    print(json.dumps(result), flush=True)
    raise SystemExit(code)


if __name__ == '__main__':
    main()
