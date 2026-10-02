"""Real containers cannot publish when precise sampling discovers an oversized verifier request."""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from codeagent.context.calibration_config import CalibrationConfig
from codeagent.llm.routing import ModelRoutingConfig, RoutingLlmClient, StaticModelRouter
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.global_verifier import LlmGlobalVerifier
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.runtime.local_verifier import LlmLocalVerifier
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_token_calibration import Provider
from tests.test_verification_budget import GRAPH, MC

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('MINDCODE_PODMAN_TEST_IMAGE') is None,
    reason='requires independent Linux VM and an explicitly trusted Podman image',
)


@pytest.mark.parametrize('stage', ['local', 'global'])
async def test_precise_verifier_overflow_blocks_actual_candidate_publication(tmp_path, stage):
    _init_repo(tmp_path)
    original = _git_out(tmp_path, 'rev-parse', 'HEAD')
    cfg = replace(_config(tmp_path), execution_backend='podman',
                  sandbox_image=os.environ['MINDCODE_PODMAN_TEST_IMAGE'],
                  verify_command='test $(cat note.txt) = candidate')
    cfg = replace(cfg, profile=replace(cfg.profile, master_max_replans=0))
    provider = Provider(exact=10_000)
    verifier_client = RoutingLlmClient(
        {'anthropic': provider}, StaticModelRouter(ModelRoutingConfig()),
        calibration=CalibrationConfig(boundary_ratio=0.01),
    )
    local = LlmLocalVerifier(verifier_client, MC) if stage == 'local' else None
    global_verifier = LlmGlobalVerifier(verifier_client, MC) if stage == 'global' else None
    worker_client = StubLlmClient([
        [('write_file', {'path': 'note.txt', 'content': 'candidate'})], 'done',
    ])
    async with MasterSession(
        cfg, llm_client=worker_client, planner=StaticPlanner(GRAPH),
        local_verifier=local, global_verifier=global_verifier,
    ) as session:
        result = await session.run_task('保留全部要求并修改文件')
        assert not result.accepted and not result.integrated
        assert not (tmp_path / 'note.txt').exists()
        assert _git_out(tmp_path, 'rev-parse', 'HEAD') == original
        assert result.scheduler is not None
        if stage == 'local':
            outcome = result.scheduler.workers['s']
            assert outcome.verification.indeterminate and outcome.run.reflection_count == 0
        assert len(provider.counts) == 1 and not provider.chats
        assert worker_client.call_count == 2 and _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles
