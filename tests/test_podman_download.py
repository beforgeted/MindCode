from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from codeagent.config import AppConfig
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.execution.download import ControlledDownloader, DownloadError, DownloadPolicy
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.infra.cancellation import CancellationToken
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.session import AgentSession
from codeagent.tool import sandbox_download_helper
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.builtin.download_file import DownloadFileTool
from codeagent.tool.executor import SandboxExecutor
from codeagent.workspace.context import WorkspaceContext

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_PODMAN_TEST_IMAGE'),
    reason='requires independent Linux VM and explicit trusted image',
)
live = pytest.mark.skipif(
    not os.environ.get('MINDCODE_DOWNLOAD_TEST_URL')
    or not os.environ.get('MINDCODE_DOWNLOAD_TEST_SHA256'),
    reason='requires an explicitly approved public HTTPS fixture and digest',
)


@pytest.mark.parametrize('case', ['binary', 'reuse', 'conflict', 'symlink'])
async def test_binary_import_is_anchored_and_never_overwrites(case):
    body = b'\x00\xffbinary\n'
    sha = hashlib.sha256(body).hexdigest()
    entries = ()
    if case in ('reuse', 'conflict'):
        entries = (SnapshotEntry('file.bin', body if case == 'reuse' else b'user data'),)
    manager = PodmanSandboxManager(os.environ['MINDCODE_PODMAN_TEST_IMAGE'])
    handle = await manager.open(TreeSnapshot(entries))
    try:
        path = 'file.bin'
        if case == 'symlink':
            assert (await manager.execute(handle, 'ln -s /tmp parent')).returncode == 0
            path = 'parent/file.bin'
        source = await asyncio.to_thread(
            Path(sandbox_download_helper.__file__).read_text, encoding='utf-8',
        )
        output = await manager.execute_python(handle, source, json.dumps({
            'path': path, 'sha256': sha, 'data': base64.b64encode(body).decode(),
            'max_bytes': 1024,
        }).encode())
        if case in ('conflict', 'symlink'):
            assert output.returncode != 0
            if case == 'conflict':
                snapshot = await manager.seal(handle)
                assert snapshot.entries[0].data == b'user data'
            else:
                assert (await manager.execute(handle, 'test ! -f /tmp/file.bin')).returncode == 0
        else:
            assert output.returncode == 0
            snapshot = await manager.seal(handle)
            assert {e.path: e.data for e in snapshot.entries} == {'file.bin': body}
    finally:
        await manager.aclose()


def fixture_request():
    url = os.environ['MINDCODE_DOWNLOAD_TEST_URL']
    sha = os.environ['MINDCODE_DOWNLOAD_TEST_SHA256']
    policy = DownloadPolicy((urlsplit(url).hostname or '',), 'allowlist')
    return url, sha, policy


@live
async def test_real_https_download_into_offline_container_and_bad_hash_keeps_existing(tmp_path):
    url, sha, policy = fixture_request()
    manager = PodmanSandboxManager(os.environ['MINDCODE_PODMAN_TEST_IMAGE'])
    handle = await manager.open(TreeSnapshot((SnapshotEntry('user.txt', b'original'),)))
    context = ToolExecutionContext(
        'r', 's', 't', 'c', WorkspaceContext.local(tmp_path), CancellationToken(),
        FileArtifactStore(tmp_path / 'state'),
        command_executor=SandboxExecutor(manager, handle, tmp_path),
        downloader=ControlledDownloader(policy),
    )
    try:
        result = await DownloadFileTool().execute(
            context, {'url': url, 'sha256': sha, 'path': 'wheel.whl'},
        )
        assert not result.is_error
        # Network capability stays in the controller; arbitrary container TCP remains blocked.
        blocked = await manager.execute(handle,
                                        "python -c 'import socket; "
                                        "socket.create_connection((\"1.1.1.1\",443),1)'")
        assert blocked.returncode != 0
        with pytest.raises(DownloadError):
            await DownloadFileTool().execute(context, {'url': url, 'sha256': '0' * 64,
                                                       'path': 'user.txt'})
        snapshot = {e.path: e.data for e in (await manager.seal(handle)).entries}
        assert hashlib.sha256(snapshot['wheel.whl']).hexdigest() == sha
        assert snapshot['user.txt'] == b'original' and not (tmp_path / 'wheel.whl').exists()
    finally:
        await manager.aclose()


@live
@pytest.mark.parametrize('accept', [True, False], ids=['accept', 'reject'])
async def test_task_download_only_publishes_after_independent_acceptance(tmp_path, accept):
    url, sha, policy = fixture_request()
    root = tmp_path / 'plain'
    root.mkdir()
    (root / '.env').write_text('sentinel only')
    (root / 'user.txt').write_text('keep')
    verify = (
        "python -c 'import hashlib,pathlib; "
        f"assert hashlib.sha256(pathlib.Path(\"wheel.whl\").read_bytes()).hexdigest()==\"{sha}\"'"
        if accept else 'false'
    )
    config = AppConfig(root, tmp_path / 'state', use_stub_llm=True, execution_backend='podman',
                       sandbox_image=os.environ['MINDCODE_PODMAN_TEST_IMAGE'], downloads=policy,
                       verify_command=verify)
    client = StubLlmClient([
        [('download_file', {'url': url, 'sha256': sha, 'path': 'wheel.whl'})], 'done',
        [('download_file', {'url': url, 'sha256': sha, 'path': 'wheel.whl'})], 'done',
    ])
    async with MasterSession(
        config, llm_client=client,
        planner=StaticPlanner(TaskGraph([Step('s', 'default', 'download')])),
    ) as ms:
        final = await ms.run_task('download verified dependency')
        assert final.integrated == accept and final.scheduler is not None
        assert len(final.scheduler.integrated) == 1
    assert (root / '.env').read_text() == 'sentinel only'
    assert (root / 'user.txt').read_text() == 'keep'
    assert not (root / '.git').exists()
    if accept:
        assert hashlib.sha256((root / 'wheel.whl').read_bytes()).hexdigest() == sha
        assert [f.path for f in final.files] == ['wheel.whl']
    else:
        assert not (root / 'wheel.whl').exists() and final.files == ()


@live
@pytest.mark.parametrize('accept', [True, False], ids=['accept', 'reject'])
async def test_interactive_download_only_publishes_after_independent_acceptance(tmp_path, accept):
    url, sha, policy = fixture_request()
    root = tmp_path / 'plain'
    root.mkdir()
    (root / '.env').write_text('sentinel only')
    (root / 'user.txt').write_text('keep')
    verify = (
        "python -c 'import hashlib,pathlib; "
        f"assert hashlib.sha256(pathlib.Path(\"wheel.whl\").read_bytes()).hexdigest()==\"{sha}\"'"
        if accept else 'false'
    )
    config = AppConfig(root, tmp_path / 'state', use_stub_llm=True, execution_backend='podman',
                       sandbox_image=os.environ['MINDCODE_PODMAN_TEST_IMAGE'], downloads=policy,
                       verify_command=verify)
    client = StubLlmClient([
        [('download_file', {'url': url, 'sha256': sha, 'path': 'wheel.whl'})], 'done',
    ])
    async with AgentSession(config, llm_client=client) as session:
        final = await session.send('download verified dependency')
        assert final.ok is accept
    assert (root / '.env').read_text() == 'sentinel only'
    assert (root / 'user.txt').read_text() == 'keep'
    assert not (root / '.git').exists()
    if accept:
        assert hashlib.sha256((root / 'wheel.whl').read_bytes()).hexdigest() == sha
    else:
        assert not (root / 'wheel.whl').exists()
