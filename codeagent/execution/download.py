"""Controlled read-only network capability, separate from container connectivity."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from codeagent.evidence.event_store import NullEventStore, RawEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.execution.download_helper import canonical_host, validate_url
from codeagent.execution.process import run_bounded
from codeagent.infra.cancellation import CancellationToken, CancelledByUser
from codeagent.tool.approval import ApprovalPolicy, DenyExternalApprovalPolicy
from codeagent.tool.command_policy import CommandDecision
from codeagent.tool.effects import EffectKind, RetryPolicy

_AUDIT_FLUSH_SECONDS = 5.0


class DownloadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    hosts: tuple[str, ...] = ()
    approval_mode: str = 'prompt'
    max_bytes: int = 8 * 1024 * 1024
    timeout_seconds: float = 30
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if type(self.hosts) is not tuple:
            raise ValueError('download hosts must be an immutable tuple')
        for host in self.hosts:
            canonical_host(host)
        if len(set(self.hosts)) != len(self.hosts) or len(self.hosts) > 32:
            raise ValueError('invalid or duplicate download hosts')
        if self.approval_mode not in ('prompt', 'allowlist'):
            raise ValueError('download approval must be prompt or allowlist')
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 8 * 1024 * 1024:
            raise ValueError('download bytes must be between 1 and 8 MiB')
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError('download timeout must be between 0 and 60 seconds')
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 5:
            raise ValueError('download redirects must be between 0 and 5')


class ControlledDownloader:
    def __init__(
        self, policy: DownloadPolicy, *, approval: ApprovalPolicy | None = None,
        events: RawEventStore | None = None,
    ):
        self.policy = policy
        self.approval = approval or DenyExternalApprovalPolicy()
        self.events = events or NullEventStore()
        self._lock = asyncio.Lock()

    async def _flush_audit(self) -> None:
        # A failed writer can leave queue.join() pending forever. Never start the
        # request in that case, and keep cancellation cleanup bounded as well.
        await asyncio.wait_for(self.events.flush(), _AUDIT_FLUSH_SECONDS)

    async def fetch(
        self, url: str, sha256: str, cancellation: CancellationToken, *,
        session_id: str, agent_run_id: str, tool_run_id: str,
        max_bytes: int | None = None,
    ) -> bytes:
        cancellation.raise_if_cancelled()
        operation = asyncio.create_task(self._fetch(
            url, sha256, cancellation, session_id=session_id, agent_run_id=agent_run_id,
            tool_run_id=tool_run_id, max_bytes=max_bytes,
        ))
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (operation, cancelled), return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise CancelledByUser('download cancelled')
            return await operation
        finally:
            for task in (operation, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, cancelled, return_exceptions=True)

    async def _fetch(
        self, url: str, sha256: str, cancellation: CancellationToken, *,
        session_id: str, agent_run_id: str, tool_run_id: str,
        max_bytes: int | None = None,
    ) -> bytes:
        canonical = None
        status = 'failed'
        receipt: dict = {}
        authorized = False

        def record(state: str) -> None:
            self.events.append_nowait(AgentEvent(
                type=EventType.NETWORK_DOWNLOAD, session_id=session_id,
                agent_run_id=agent_run_id, tool_run_id=tool_run_id,
                payload={'url': canonical, 'sha256': sha256, 'status': state,
                         'approval_mode': self.policy.approval_mode, 'approved': authorized,
                         **receipt},
            ))

        try:
            cancellation.raise_if_cancelled()
            validate_url(url, self.policy.hosts)
            canonical = url
            if not isinstance(sha256, str) or re.fullmatch('[0-9a-f]{64}', sha256) is None:
                raise DownloadError('requires lowercase SHA256')
            if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
                raise DownloadError('invalid byte limit')
            maximum = min(
                self.policy.max_bytes, self.policy.max_bytes if max_bytes is None else max_bytes,
            )
            async with self._lock:
                cancellation.raise_if_cancelled()
                decision = CommandDecision(
                    True, EffectKind.READ_ONLY, RetryPolicy.SAFE, needs_approval=True,
                    reason='受控HTTPS文件下载（白名单和内容摘要约束）',
                )
                authorized = (
                    self.policy.approval_mode == 'allowlist'
                    or await self.approval.approve(decision, command=f'GET {url}\nSHA256 {sha256}')
                )
                cancellation.raise_if_cancelled()
                if not authorized:
                    status = 'denied'
                    raise DownloadError('download was not approved')
                record('started')
                await self._flush_audit()
                request = json.dumps({
                    'url': url, 'hosts': self.policy.hosts, 'sha256': sha256,
                    'max_bytes': maximum, 'max_redirects': self.policy.max_redirects,
                    'timeout_seconds': self.policy.timeout_seconds,
                }).encode()
                source = Path(__file__).with_name('download_helper.py').read_text(encoding='utf-8')
                output = await run_bounded(
                    (sys.executable, '-I', '-c', source), data=request,
                    max_bytes=2 * maximum + 65536,
                    timeout_seconds=self.policy.timeout_seconds, cancellation=cancellation,
                )
                result = json.loads(output.stdout)
                receipt['hops'] = result.get('hops', [])
                if output.returncode != 0:
                    raise DownloadError(str(result.get('error', 'download failed')))
                data = base64.b64decode(result['data'], validate=True)
                if len(data) > maximum or hashlib.sha256(data).hexdigest() != sha256:
                    raise DownloadError('invalid downloader payload')
                cancellation.raise_if_cancelled()
                receipt['bytes'] = len(data)
                status = 'downloaded'
                return data
        except (CancelledByUser, asyncio.CancelledError):
            status = 'cancelled'
            raise
        except TimeoutError:
            status = 'timeout'
            raise
        except Exception as exc:
            receipt['error_type'] = type(exc).__name__
            raise
        finally:
            # Downloaded means verified bytes, not workspace acceptance/publication.
            record(status)
            await self._flush_audit()
