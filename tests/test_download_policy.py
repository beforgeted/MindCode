from __future__ import annotations

import hashlib
import io
import socket
import ssl

import pytest

from codeagent.execution import download_helper as helper
from codeagent.execution.download import DownloadPolicy

HOSTS = ('files.example.org', 'cdn.example.org')
URL = 'https://files.example.org/package.whl'
BODY = b'wheel contents'
SHA = hashlib.sha256(BODY).hexdigest()


@pytest.mark.parametrize('host', [
    '*', '*.example.org', 'https://example.org', 'localhost', '127.0.0.1',
    'EXAMPLE.org', 'example.org.', '-example.org', 'x..org', 'x' * 64 + '.org',
])
def test_only_exact_canonical_dns_hosts_can_be_configured(host):
    with pytest.raises(ValueError):
        DownloadPolicy((host,))


@pytest.mark.parametrize('url', [
    'http://files.example.org/a', 'https://evil.org/a', 'https://files.example.org.evil.org/a',
    'https://files.example.org:444/a', 'https://user:pass@files.example.org/a',
    'https://files.example.org/a?q=secret', 'https://files.example.org/a#fragment',
    'https://127.0.0.1/a', 'https://[::1]/a', 'https://files.example.org\\@evil.org/a',
    'https://files.example.org/\r\nHeader: bad', 'https://FILES.example.org/a',
    'https://files.example.org:0443/a',
])
def test_invalid_or_unapproved_urls_are_rejected(url):
    with pytest.raises(ValueError):
        helper.validate_url(url, HOSTS)


@pytest.mark.parametrize('address', [
    '127.0.0.1', '10.0.0.1', '192.168.0.1', '169.254.169.254', '100.64.0.1',
    '0.0.0.0', '224.0.0.1', '192.0.0.8', '192.88.99.1', '198.18.0.1',
    '::1', 'fc00::1', 'fe80::1', 'ff02::1', '::ffff:8.8.8.8',
    '2002:0808:0808::1', '64:ff9b::808:808', '2001:0000:4136:e378:8000:63bf:3fff:fdd2',
])
def test_dns_special_and_private_addresses_are_rejected(address):
    with pytest.raises(ValueError):
        helper.public_address(address)


def test_mixed_dns_answer_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
    ])
    with pytest.raises(ValueError):
        helper.resolve_public('files.example.org')


def test_pinned_connection_preserves_tls_hostname_and_never_connects_by_dns(monkeypatch):
    connected = []
    wrapped = []
    class Socket:
        def close(self):
            pass
    class Context:
        def set_alpn_protocols(self, protocols):
            assert protocols == ['http/1.1']
        def wrap_socket(self, sock, *, server_hostname):
            wrapped.append(server_hostname)
            return sock
    monkeypatch.setattr(ssl, 'create_default_context', lambda: Context())
    def connect(address, *, timeout):
        connected.append(address)
        return Socket()
    monkeypatch.setattr(socket, 'create_connection', connect)
    connection = helper.PinnedHTTPSConnection('files.example.org', '8.8.8.8', 5)
    connection.connect()
    assert connected == [('8.8.8.8', 443)] and wrapped == ['files.example.org']


def test_real_tls_context_requires_certificate_and_hostname_verification():
    connection = helper.PinnedHTTPSConnection('files.example.org', '8.8.8.8', 5)
    assert connection.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert connection.tls_context.check_hostname


def test_tls_handshake_failure_closes_pinned_socket(monkeypatch):
    closed = []
    class Socket:
        def close(self):
            closed.append(True)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: Socket())
    def invalid_certificate(*a, **kw):
        raise ssl.SSLCertVerificationError('synthetic untrusted certificate')
    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket', invalid_certificate)
    connection = helper.PinnedHTTPSConnection('files.example.org', '8.8.8.8', 5)
    with pytest.raises(ssl.SSLCertVerificationError):
        connection.connect()
    assert closed == [True]


@pytest.fixture
def transport(monkeypatch):
    responses = []
    requests = []
    closed = []
    class Response:
        def __init__(self, status=200, body=BODY, headers=None):
            self.status, self.body, self.headers = status, io.BytesIO(body), headers or {}
        def getheader(self, name, default=None):
            return self.headers.get(name, default)
        def read(self, size):
            return self.body.read(size)
    class Connection:
        def __init__(self, host, address, timeout):
            self.host = host
            assert address == '8.8.8.8'
        def request(self, method, path, *, headers):
            requests.append((self.host, method, path, headers))
        def getresponse(self):
            return responses.pop(0)
        def close(self):
            closed.append(self.host)
    monkeypatch.setattr(helper, 'resolve_public', lambda host: '8.8.8.8')
    monkeypatch.setattr(helper, 'PinnedHTTPSConnection', Connection)
    return Response, responses, requests, closed


def request(**overrides):
    return {'url': URL, 'hosts': HOSTS, 'sha256': SHA,
            'max_bytes': 1024, 'timeout_seconds': 5, 'max_redirects': 2, **overrides}


def test_redirect_checks_each_hop_and_uses_only_fixed_headers(transport):
    response, responses, requests, closed = transport
    responses.extend([
        response(302, headers={'Location': 'https://cdn.example.org/wheel'}), response(),
    ])
    hops = []
    assert helper.download(request(), hops) == BODY
    assert [h['url'] for h in hops] == [URL, 'https://cdn.example.org/wheel']
    assert len(closed) == 2
    assert all(method == 'GET' and set(headers) == {'Host', 'Accept-Encoding', 'User-Agent'}
               for _, method, _, headers in requests)


@pytest.mark.parametrize('location', [
    'https://evil.org/x', 'http://files.example.org/x', 'https://user@files.example.org/x',
    'https://files.example.org/x?q=secret', 'https://127.0.0.1/x', '\n/hidden',
])
def test_redirect_cannot_bypass_origin_policy(transport, location):
    response, responses, requests, closed = transport
    responses.append(response(302, headers={'Location': location}))
    with pytest.raises(ValueError):
        helper.download(request(), [])
    assert len(requests) == len(closed) == 1


def test_redirect_same_hostname_gets_dns_revalidated(transport, monkeypatch):
    response, responses, requests, _ = transport
    responses.append(response(302, headers={'Location': '/again'}))
    calls = []
    def resolve(host):
        calls.append(host)
        if len(calls) > 1:
            raise ValueError('DNS now private')
        return '8.8.8.8'
    monkeypatch.setattr(helper, 'resolve_public', resolve)
    with pytest.raises(ValueError, match='DNS now private'):
        helper.download(request(), [])
    assert len(requests) == 1 and len(calls) == 2


def test_redirect_loop_is_bounded(transport):
    response, responses, requests, closed = transport
    responses.extend(response(302, headers={'Location': '/again'}) for _ in range(3))
    with pytest.raises(ValueError, match='redirect'):
        helper.download(request(), [])
    assert len(requests) == len(closed) == 3


@pytest.mark.parametrize('headers,body', [
    ({'Content-Length': '10000'}, BODY), ({}, b'x' * 1025),
    ({'Content-Length': '999'}, BODY), ({'Content-Encoding': 'gzip'}, BODY),
    ({}, b'wrong hash'),
])
def test_length_encoding_and_content_hash_fail_closed(transport, headers, body):
    response, responses, _, closed = transport
    responses.append(response(body=body, headers=headers))
    with pytest.raises(ValueError):
        helper.download(request(), [])
    assert len(closed) == 1
