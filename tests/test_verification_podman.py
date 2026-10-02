"""Actual rootless containers: uncertain LLM verification never publishes changes."""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.global_verifier import LlmGlobalVerifier
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.runtime.local_verifier import LlmLocalVerifier
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_verification_budget import GRAPH, MC, AuditProvider

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MINDCODE_PODMAN_TEST_IMAGE") is None,
    reason="requires independent Linux VM and an explicitly trusted Podman image",
)


@pytest.mark.parametrize("failure", ["local", "coverage", "cross_file", "success"])
async def test_actual_worker_and_candidate_require_complete_verification(tmp_path, failure):
    image = os.environ["MINDCODE_PODMAN_TEST_IMAGE"]
    _init_repo(tmp_path)
    original = _git_out(tmp_path, "rev-parse", "HEAD")
    config = replace(_config(tmp_path), execution_backend="podman", sandbox_image=image,
                     verify_command="test $(cat note.txt) = candidate")
    config = replace(config, profile=replace(config.profile, master_max_replans=0))
    provider = AuditProvider(failure="coverage" if failure == "coverage" else "",
                             cross_file=failure == "cross_file")
    local = LlmLocalVerifier(StubLlmClient(["invalid"]), MC) if failure == "local" else None
    client = StubLlmClient([
        [("write_file", {"path": "note.txt", "content": "candidate"})], "done",
    ])
    async with MasterSession(
        config, llm_client=client, planner=StaticPlanner(GRAPH), local_verifier=local,
        global_verifier=LlmGlobalVerifier(provider, MC),
    ) as session:
        result = await session.run_task("保留全部要求并修改文件")
        accepted = failure == "success"
        assert result.accepted is accepted and result.integrated is accepted
        assert (tmp_path / "note.txt").exists() is accepted
        assert (_git_out(tmp_path, "rev-parse", "HEAD") != original) is accepted
        assert result.scheduler is not None
        worker = result.scheduler.workers["s"]
        if failure == "local":
            assert worker.verification.indeterminate and not worker.verification.ok
            assert worker.run.reflection_count == 0 and not provider.calls
        assert client.call_count == 2
        assert _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles
