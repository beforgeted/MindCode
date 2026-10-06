"""Explicit, credential-free network reads feeding an offline official Fetch server."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.execution import download_helper, fetch_helper
from codeagent.execution.download_helper import canonical_host, validate_url
from codeagent.execution.process import run_bounded
from codeagent.infra.cancellation import CancellationToken

USER_AGENT = ('ModelContextProtocol/1.0 '
              '(Autonomous; +https://github.com/modelcontextprotocol/servers)')
REDIRECTS = (301, 302, 303, 307, 308)


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    hosts: tuple[str, ...]
    max_bytes: int = 1024 * 1024
    max_redirects: int = 3
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        if (type(self.hosts) is not tuple or not 1 <= len(self.hosts) <= 32
                or len(set(self.hosts)) != len(self.hosts)):
            raise ValueError('fetch requires a nonempty immutable host allowlist')
        for host in self.hosts:
            canonical_host(host)
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 1024 * 1024:
            raise ValueError('fetch bytes must be between 1 and 1 MiB')
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 3:
            raise ValueError('fetch redirects must be between 0 and 3')
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60):
            raise ValueError('fetch timeout must be finite and within 60 seconds')


class ControlledFetcher:
    def __init__(self, policy: FetchPolicy, artifacts: ArtifactStore):
        self.policy, self.artifacts = policy, artifacts
        common = Path(download_helper.__file__).read_text(encoding='utf-8').split(
            "\nif __name__ == '__main__':", 1)[0]
        helper = Path(fetch_helper.__file__).read_text(encoding='utf-8')
        helper = helper.replace('from __future__ import annotations', '').replace(
            'from codeagent.execution.download_helper import '
            'PinnedHTTPSConnection, resolve_public, validate_url', '')
        self.source = common + '\n' + helper

    async def hop(self, url: str, maximum: int, cancellation: CancellationToken) -> dict:
        validate_url(url, self.policy.hosts)
        cancellation.raise_if_cancelled()
        # Persist intent before any DNS/network IO. An unavailable evidence store fails closed.
        await self.artifacts.save_text('network-fetch', json.dumps({
            'url': url, 'status': 'started', 'max_bytes': maximum,
        }))
        try:
            output = await run_bounded(
                (sys.executable, '-I', '-c', self.source),
                data=json.dumps({'url': url, 'hosts': self.policy.hosts, 'max_bytes': maximum,
                                 'max_redirects': 0, 'user_agent': USER_AGENT,
                                 'timeout_seconds': self.policy.timeout_seconds}).encode(),
                cancellation=cancellation, timeout_seconds=self.policy.timeout_seconds,
                max_bytes=2 * maximum + 65536,
            )
            record = json.loads(output.stdout)
            if output.returncode:
                raise ValueError('controlled fetch failed: ' + str(record.get('error', 'protocol')))
            if record['url'] != url:
                raise ValueError('fetch receipt URL mismatch')
            if record['status'] not in REDIRECTS:
                body = base64.b64decode(record['data'], validate=True)
                if (len(body) > maximum or record['bytes'] != len(body)
                        or hashlib.sha256(body).hexdigest() != record['sha256']):
                    raise ValueError('fetch receipt content mismatch')
            await self.artifacts.save_text('network-fetch', json.dumps({
                k: v for k, v in record.items() if k != 'data'}))
            return record
        except BaseException as exc:
            await self.artifacts.save_text('network-fetch', json.dumps({
                'url': url, 'status': 'failed', 'error_type': type(exc).__name__,
            }))
            raise

    async def robots(self, url: str, cancellation: CancellationToken) -> dict:
        validate_url(url, self.policy.hosts)
        parsed = urlsplit(url)
        current = f'https://{parsed.netloc}/robots.txt'
        for index in range(self.policy.max_redirects + 1):
            record = await self.hop(current, min(self.policy.max_bytes, 65536), cancellation)
            if record['status'] not in REDIRECTS:
                # Mirrors upstream's denial, but also rejects 5xx instead of treating it as rules.
                if record['status'] in (401, 403) or record['status'] >= 500:
                    raise ValueError('robots response forbids or cannot authorize fetch')
                return record
            if index == self.policy.max_redirects or not record.get('location'):
                raise ValueError('robots redirect limit or invalid location')
            current = urljoin(current, record['location'])
        raise ValueError('robots redirect limit')

    async def fetch(self, url: str, cancellation: CancellationToken,
                    authorize: Callable[[str, dict], Awaitable[None]]) -> dict[str, dict]:
        records: dict[str, dict] = {}
        current = url
        for index in range(self.policy.max_redirects + 1):
            validate_url(current, self.policy.hosts)
            parsed = urlsplit(current)
            robots_url = f'https://{parsed.netloc}/robots.txt'
            robots = await self.robots(current, cancellation)
            records[robots_url] = robots
            # Official robots parser runs offline in the current Podman domain before each page hop.
            await authorize(current, {robots_url: robots})
            record = await self.hop(current, self.policy.max_bytes, cancellation)
            if record['status'] not in REDIRECTS:
                if record['status'] != 200:
                    raise ValueError('fetch HTTP status must be 200')
                if current != url:
                    records[url] = record
                records[current] = record
                return records
            if index == self.policy.max_redirects or not record.get('location'):
                raise ValueError('page redirect limit or invalid location')
            current = urljoin(current, record['location'])
        raise ValueError('page redirect limit')
