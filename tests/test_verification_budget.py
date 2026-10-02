"""Long-input contracts: complete evidence, bounded calls, and no uncertain publication."""
from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from typing import cast

import pytest

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.agent.run import AgentRun
from codeagent.config import AppConfig
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.execution.snapshot import SnapshotEntry, SnapshotLimits, TreeSnapshot
from codeagent.infra.trace import current_trace
from codeagent.llm.client import LlmError, LlmErrorKind
from codeagent.llm.request_budget import RequestBudgetError, check_request
from codeagent.llm.stub_client import StubLlmClient
from codeagent.llm.types import LlmResponse, ModelConfig
from codeagent.orchestration.global_verifier import LlmGlobalVerifier, VerificationTarget
from codeagent.orchestration.master_session import build_master
from codeagent.orchestration.planner import LlmPlanner, StaticPlanner
from codeagent.orchestration.step_scheduler import SchedulerResult
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.orchestration.verification_limits import VerificationLimits
from codeagent.runtime.agent_runtime import AgentRuntime
from codeagent.runtime.local_verifier import LlmLocalVerifier, VerificationResult
from codeagent.runtime.react_engine import ReActEngine
from codeagent.session import AgentSession
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
from codeagent.workspace.manager import LocalWorkspaceManager
from codeagent.workspace.snapshot import SnapshotWorkspaceManager
from codeagent.workspace.verification_evidence import binary_unit, collect_evidence
from tests.test_agent_runtime import FakeEngine
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_master_runtime import FakeRuntime, _master

MC = ModelConfig(model="fixed", context_window=2400, max_output_tokens=128)
DEFN = AgentDefinition(id="default", name="D", system_prompt="")
GRAPH = TaskGraph([Step("s", "default", "保留全部要求并修改文件")])


class AuditProvider:
    def __init__(self, *, window=2400, failure="", cross_file=False):
        self.config = replace(MC, context_window=window)
        self.failure = failure
        self.cross_file = cross_file
        self.calls: list[dict] = []
        self.traces: list[dict] = []

    def effective_config(self, config):
        return self.config

    async def count_tokens(self, *args, **kwargs):
        raise AssertionError("budget checks must not make token-count network calls")

    async def chat(self, messages, *, model_config, tools=()):
        assert model_config == self.config
        check_request(messages, model_config, HeuristicTokenEstimator())
        prompt = json.loads(messages[1].text)
        self.calls.append(prompt)
        self.traces.append(current_trace())
        if self.failure == "timeout":
            await asyncio.sleep(60)
        if "units" in prompt:
            units = prompt["units"]
            if self.failure == "later" and len(self.calls) == 2:
                raise RuntimeError("later batch down")
            ids = [u["id"] for u in units]
            if self.failure == "coverage":
                ids = ids[:-1]
            if self.failure == "duplicate":
                ids = [*ids, ids[0]]
            accept = not any("FORBIDDEN_AT_TAIL" in u["diff"] for u in units)
            output = {"checked_ids": ids, "accept": accept, "reason": "tail violation" if
                      not accept else "", "findings": ["interfaces require a global check"]}
            if self.failure == "large_report":
                output["findings"] = ["x" * 12000]
        else:
            output = {"accept": not self.cross_file, "reason": "cross-file mismatch" if
                      self.cross_file else "complete", "replan_instruction": ""}
        return LlmResponse("fixed", json.dumps(output), stop_reason="end_turn")


def target(*, count=4, chars=4000, tail=False):
    files = tuple(f"file_{i}.txt" for i in range(count))
    diffs = [(p, f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-previous\n+"
              + "x" * chars + ("FORBIDDEN_AT_TAIL" if tail and i == count - 1 else "") + "\n")
             for i, p in enumerate(files)]
    evidence = collect_evidence("base", "candidate", files, diffs, max_bytes=100000)
    assert evidence.complete
    return VerificationTarget(revision="candidate", changed_files=files, evidence=evidence)


def local_run(tmp_path):
    run = AgentRun.create(DEFN, session_id="s", workspace=WorkspaceContext.local(tmp_path))
    run.context.instruction = "保留必须满足的原始目标"
    return run


async def test_global_checks_all_tail_evidence_and_then_overall():
    provider = AuditProvider()
    artifact = target()
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), artifact,
    )
    assert verdict.accept and not verdict.indeterminate
    evidence = artifact.evidence
    assert evidence is not None
    assert len("".join(u.text for u in evidence.units)) > 12000
    assert set(verdict.checked_units) == {u.id for u in evidence.units}
    assert verdict.evidence_digest == evidence.digest
    assert len(provider.calls) == 5  # four complete chunks, then a global review
    assert "batch_reports" in provider.calls[-1]
    assert all(t["verification_digest"] == evidence.digest for t in provider.traces)


async def test_violation_in_last_batch_rejects_without_final_vote():
    provider = AuditProvider()
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), target(tail=True),
    )
    assert not verdict.accept and not verdict.indeterminate
    assert len(provider.calls) == 4 and verdict.reason == "tail violation"


async def test_passing_batches_cannot_override_cross_file_rejection():
    provider = AuditProvider(cross_file=True)
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), target(),
    )
    assert not verdict.accept and verdict.reason == "cross-file mismatch"
    assert len(provider.calls) == 5


@pytest.mark.parametrize("failure", ["later", "coverage", "duplicate", "large_report"])
async def test_partial_or_unusable_reports_never_pass(failure):
    provider = AuditProvider(failure=failure)
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), target(),
    )
    assert not verdict.accept and verdict.indeterminate
    assert not any("batch_reports" in c for c in provider.calls)


@pytest.mark.parametrize("kind", ["single_hunk", "task", "batch_limit", "window"])
async def test_budget_failures_are_detected_before_any_provider_call(kind):
    provider = AuditProvider(window=200 if kind == "window" else 2400)
    limits = VerificationLimits(max_batches=1 if kind == "batch_limit" else 32)
    artifact = target(count=1, chars=20000) if kind == "single_hunk" else target()
    task = "约束" * 5000 if kind == "task" else "任务"
    verdict = await LlmGlobalVerifier(provider, MC, limits=limits).verify(
        task, GRAPH, SchedulerResult(), artifact,
    )
    assert verdict.indeterminate and not verdict.accept and not provider.calls


@pytest.mark.parametrize("kind", ["missing", "revision", "path", "digest", "incomplete"])
async def test_incomplete_or_mismatched_evidence_never_passes(kind):
    provider = AuditProvider()
    artifact = target(count=1)
    evidence = artifact.evidence
    assert evidence is not None
    if kind == "missing":
        artifact = replace(artifact, evidence=None, diff="apparently safe truncated diff")
    elif kind == "revision":
        artifact = replace(artifact, revision="other")
    elif kind == "path":
        artifact = replace(artifact, changed_files=("missing.txt",))
    elif kind == "digest":
        unit = replace(evidence.units[0], text="tampered")
        artifact = replace(artifact, evidence=replace(evidence, units=(unit,)))
    else:
        artifact = replace(
            artifact, evidence=replace(evidence, complete=False, detail="missing tail"),
        )
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), artifact,
    )
    assert verdict.indeterminate and not verdict.accept and not provider.calls


async def test_failed_deterministic_check_skips_all_model_calls():
    provider = AuditProvider()
    artifact = replace(target(), deterministic_ok=False, deterministic_detail="test failed")
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), artifact,
    )
    assert not verdict.accept and not verdict.indeterminate and not provider.calls


async def test_timeout_and_external_cancellation_do_not_become_acceptance():
    provider = AuditProvider(failure="timeout")
    verifier = LlmGlobalVerifier(provider, MC, limits=VerificationLimits(timeout_seconds=0.02))
    verdict = await verifier.verify("任务", GRAPH, SchedulerResult(), target())
    assert not verdict.accept and verdict.indeterminate
    task = asyncio.create_task(LlmGlobalVerifier(provider, MC).verify(
        "任务", GRAPH, SchedulerResult(), target(),
    ))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_timeout_during_request_planning_stops_before_provider_call():
    import time

    class SlowEstimator(HeuristicTokenEstimator):
        def estimate(self, messages):
            time.sleep(0.01)
            return super().estimate(messages)

    provider = AuditProvider()
    verifier = LlmGlobalVerifier(provider, MC, estimator=SlowEstimator(),
                                 limits=VerificationLimits(timeout_seconds=0.005))
    verdict = await verifier.verify("任务", GRAPH, SchedulerResult(), target())
    assert verdict.indeterminate and not verdict.accept and not provider.calls


@pytest.mark.parametrize("raw", ['{"accept":"false"}', '{"accept":1}', '{}', '[]'])
async def test_global_strict_verdict_schema(raw):
    client = StubLlmClient([raw, raw])
    verdict = await LlmGlobalVerifier(client, MC).verify("任务", GRAPH, SchedulerResult())
    assert verdict.indeterminate and not verdict.accept


async def test_global_json_repair_is_bounded_and_rechecks_budget():
    client = StubLlmClient(["invalid", '{"accept":true}'])
    verdict = await LlmGlobalVerifier(client, MC).verify("任务", GRAPH, SchedulerResult())
    assert verdict.accept and client.call_count == 2
    assert len(client.seen_calls[1]) == 3


@pytest.mark.parametrize("phase", ["local", "global", "planner"])
async def test_max_tokens_even_with_valid_json_is_not_success(tmp_path, phase):
    raw = {"ok": True} if phase == "local" else {"accept": True}
    if phase == "planner":
        raw = {"steps": [{"id": "s", "instruction": "x"}]}
    client = StubLlmClient([LlmResponse("fixed", json.dumps(raw), stop_reason="max_tokens")] * 2)
    if phase == "local":
        run = local_run(tmp_path)
        verdict = await LlmLocalVerifier(client, MC).verify(
            run, AgentRunResult.success(run.run_id, "done"),
        )
        assert not verdict.ok and verdict.indeterminate
    elif phase == "global":
        verdict = await LlmGlobalVerifier(client, MC).verify("任务", GRAPH, SchedulerResult())
        assert not verdict.accept and verdict.indeterminate
    else:
        # Existing invalid-output fallback retains the entire original task.
        graph = await LlmPlanner(client, MC).plan("完整原始任务")
        assert graph.steps[0].instruction == "完整原始任务"


@pytest.mark.parametrize("raw", ['{}', '{"ok":"false"}', 'invalid', '{"ok":1}'])
async def test_local_invalid_output_is_indeterminate(tmp_path, raw):
    run = local_run(tmp_path)
    verdict = await LlmLocalVerifier(StubLlmClient([raw]), MC).verify(
        run, AgentRunResult.success(run.run_id, "done"),
    )
    assert not verdict.ok and verdict.indeterminate


async def test_local_budget_and_missing_goal_make_no_call(tmp_path):
    run = local_run(tmp_path)
    client = StubLlmClient(['{"ok":true}'])
    verifier = LlmLocalVerifier(client, MC)
    result = AgentRunResult.success(run.run_id, "约束" * 5000)
    verdict = await verifier.verify(run, result)
    assert not verdict.ok and verdict.indeterminate and not client.call_count
    run.context.instruction = ""
    verdict = await verifier.verify(run, AgentRunResult.success(run.run_id, "done"))
    assert not verdict.ok and verdict.indeterminate and not client.call_count


async def test_goal_survives_reflection_and_is_sent_to_local_verifier(tmp_path):
    seen = []

    def reply(messages):
        seen.append(messages[1].text)
        return LlmResponse("fixed", '{"ok":true}' if len(seen) == 2 else
                           '{"ok":false,"feedback":"修正结果"}')

    engine = FakeEngine()
    runtime = AgentRuntime(
        react_engine=cast(ReActEngine, engine), workspace_manager=LocalWorkspaceManager(tmp_path),
        local_verifier=LlmLocalVerifier(StubLlmClient([reply, reply]), MC),
    )
    worker = await runtime.run(DEFN, GRAPH.steps[0], session_id="s")
    assert worker.verification.ok and engine.calls == 2
    assert all(GRAPH.steps[0].instruction in prompt for prompt in seen)
    assert worker.run.context.instruction == GRAPH.steps[0].instruction


async def test_local_unavailability_does_not_trigger_worker_reflection(tmp_path):
    class Unavailable:
        async def verify(self, run, result):
            return VerificationResult(ok=False, indeterminate=True, reason="unavailable")

    engine = FakeEngine()
    runtime = AgentRuntime(
        react_engine=cast(ReActEngine, engine), workspace_manager=LocalWorkspaceManager(tmp_path),
        local_verifier=Unavailable(),
    )
    worker = await runtime.run(DEFN, GRAPH.steps[0], session_id="s")
    assert worker.verification.indeterminate and not worker.verification.ok
    assert engine.calls == 1 and worker.run.reflection_count == 0


async def test_planner_budget_preserves_task_without_silent_fallback():
    client = StubLlmClient([])
    with pytest.raises(RequestBudgetError):
        await LlmPlanner(client, MC).plan("必须保留" * 5000)
    assert not client.call_count


async def test_provider_context_limit_does_not_degrade_to_single_step():
    def fail(messages):
        raise LlmError("context", kind=LlmErrorKind.CONTEXT_LIMIT)

    with pytest.raises(RequestBudgetError):
        await LlmPlanner(StubLlmClient([fail]), MC).plan("原始任务")


async def test_master_reports_planning_budget_failure_without_running_worker(tmp_path):
    runtime = FakeRuntime()
    master = _master(GRAPH, runtime, tmp=tmp_path)
    master._planner = LlmPlanner(StubLlmClient([]), MC)
    final = await master.run("必须保留" * 5000, session_id="s")
    assert not final.accepted and not final.integrated
    assert "未降级执行" in final.reason and not runtime.trace_ids


async def test_snapshot_evidence_covers_tail_and_file_metadata(tmp_path):
    manager = SnapshotWorkspaceManager(tmp_path, tmp_path / "state", limits=SnapshotLimits())
    old = ("unchanged\n" * 2000).encode()
    manager._snapshots = {
        "a": TreeSnapshot((SnapshotEntry("large.txt", old), SnapshotEntry("mode.txt", b""))),
        "b": TreeSnapshot((SnapshotEntry("large.txt", old + b"CRITICAL_TAIL\n"),
                           SnapshotEntry("mode.txt", b"", True), SnapshotEntry("empty.txt", b""))),
    }
    evidence = await manager.verification_evidence("a", "b", max_bytes=100000)
    assert evidence.complete
    assert set(evidence.changed_files) == {"large.txt", "mode.txt", "empty.txt"}
    assert "CRITICAL_TAIL" in "".join(u.text for u in evidence.units)
    assert "executable=True" in next(u.text for u in evidence.units if u.path == "mode.txt")


@pytest.mark.parametrize("data", [b"binary\0file", b"invalid\xffunicode", b"x" * 1000])
async def test_snapshot_unsupported_or_oversized_evidence_is_explicit(tmp_path, data):
    manager = SnapshotWorkspaceManager(tmp_path, tmp_path / "state", limits=SnapshotLimits())
    manager._snapshots = {"a": TreeSnapshot(()), "b": TreeSnapshot((SnapshotEntry("x", data),))}
    evidence = await manager.verification_evidence("a", "b", max_bytes=500)
    if len(data) > 500:
        assert not evidence.complete and evidence.detail and not evidence.units
    else:
        assert evidence.complete and evidence.binary_files == ("x",)
        metadata = json.loads(evidence.units[0].text)
        assert metadata["new"]["bytes"] == len(data)


@pytest.mark.parametrize("check", [None, False, True])
async def test_binary_metadata_requires_successful_independent_check(check):
    unit = binary_unit("wheel.whl", None, b"binary\0payload", old_mode="", new_mode="100644")
    evidence = collect_evidence("base", "candidate", ("wheel.whl",), [],
                                binary=(unit,), max_bytes=10000)
    assert evidence.complete
    artifact = VerificationTarget(revision="candidate", changed_files=("wheel.whl",),
                                  evidence=evidence, deterministic_ok=check)
    provider = AuditProvider()
    verdict = await LlmGlobalVerifier(provider, MC).verify(
        "下载验证过的依赖", GRAPH, SchedulerResult(), artifact,
    )
    assert verdict.accept is (check is True)
    assert bool(provider.calls) is (check is True)


async def test_git_evidence_retains_large_tail_unicode_paths_and_full_hunks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _git_out(repo, "rev-parse", "HEAD")
    path = " 前缀 中文.txt"
    (repo / path).write_text("line\n" * 5000 + "CRITICAL_TAIL\n", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(repo), "add", "."], check=True, capture_output=True,
    )
    await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(repo), "commit", "-m", "candidate"],
        check=True, capture_output=True,
    )
    revision = _git_out(repo, "rev-parse", "HEAD")
    manager = GitWorktreeWorkspaceManager(repo, tmp_path / "worktrees")
    evidence = await manager.verification_evidence(base, revision, max_bytes=100000)
    assert evidence.complete and evidence.changed_files == (path,)
    assert "CRITICAL_TAIL" in evidence.units[-1].text and len(evidence.units[-1].text) > 20000
    insufficient = await manager.verification_evidence(base, revision, max_bytes=100)
    assert not insufficient.complete and not insufficient.units
    missing = await manager.verification_evidence("does-not-exist", revision, max_bytes=100000)
    assert not missing.complete and "collection failed" in missing.detail


@pytest.mark.parametrize("operation", ["create", "modify", "delete"])
async def test_git_binary_evidence_is_bound_to_frozen_blob_hashes(tmp_path, operation):
    import hashlib

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    path = repo / "binary.dat"
    old = b"old\0content" if operation != "create" else None

    async def commit():
        await asyncio.to_thread(subprocess.run, ["git", "-C", str(repo), "add", "."],
                                check=True, capture_output=True)
        await asyncio.to_thread(subprocess.run, ["git", "-C", str(repo), "commit", "-m", "blob"],
                                check=True, capture_output=True)

    if old is not None:
        path.write_bytes(old)
        await commit()
    base = _git_out(repo, "rev-parse", "HEAD")
    new = b"new\0content" if operation != "delete" else None
    if new is None:
        path.unlink()
    else:
        path.write_bytes(new)
    await commit()
    revision = _git_out(repo, "rev-parse", "HEAD")
    path.write_bytes(b"unrelated working tree bytes")
    manager = GitWorktreeWorkspaceManager(repo, tmp_path / "worktrees")
    evidence = await manager.verification_evidence(base, revision, max_bytes=10000)
    assert evidence.complete and evidence.binary_files == ("binary.dat",)
    metadata = json.loads(evidence.units[0].text)
    for field, data in (("old", old), ("new", new)):
        assert metadata[field]["exists"] is (data is not None)
        assert metadata[field]["sha256"] == (hashlib.sha256(data).hexdigest() if data else None)


async def test_git_pathspec_magic_is_treated_as_a_literal_filename(tmp_path):
    import hashlib

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    async def git(*args, data=None):
        result = await asyncio.to_thread(
            subprocess.run, ["git", "-C", str(repo), *args], input=data,
            capture_output=True, check=True,
        )
        return result.stdout.decode().strip()

    base = await git("rev-parse", "HEAD")
    seed = await git("rev-parse", "HEAD:seed.txt")
    actual = b"actual\0binary"
    decoy = b"wrong\0binary"
    actual_oid = await git("hash-object", "-w", "--stdin", data=actual)
    decoy_oid = await git("hash-object", "-w", "--stdin", data=decoy)
    # Construct a tree without checking out Windows-forbidden filename characters.
    entries = (f"100644 blob {actual_oid}\t:(glob)decoy.dat\0"
               f"100644 blob {decoy_oid}\tdecoy.dat\0"
               f"100644 blob {seed}\tseed.txt\0").encode()
    tree = await git("mktree", "-z", data=entries)
    revision = await git("commit-tree", tree, "-p", base, "-m", "candidate")
    evidence = await GitWorktreeWorkspaceManager(repo, tmp_path / "wt").verification_evidence(
        base, revision, max_bytes=10000,
    )
    assert evidence.complete
    unit = next(u for u in evidence.units if u.path == ":(glob)decoy.dat")
    assert json.loads(unit.text)["new"]["sha256"] == hashlib.sha256(actual).hexdigest()


async def test_deterministic_default_cannot_publish_binary_without_check(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    config = replace(config, profile=replace(config.profile, master_max_replans=0))
    client = StubLlmClient([[('write_file', {"path": "note.bin", "content": "a\0b"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics,
            definition=session.definition, planner=StaticPlanner(GRAPH),
        )
        result = await master.run("create binary", session_id=session.session_id)
        assert not result.accepted and not result.integrated
        assert "二进制" in result.reason
        assert not (repo / "note.bin").exists()


@pytest.mark.parametrize("failure", ["coverage", "cross_file", "evidence_limit", "success"])
async def test_real_git_candidate_publication_requires_complete_global_review(tmp_path, failure):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    before = _git_out(repo, "rev-parse", "HEAD")
    provider = AuditProvider(failure="coverage" if failure == "coverage" else "",
                             cross_file=failure == "cross_file")
    client = StubLlmClient([[('write_file', {"path": "note.txt", "content": "candidate"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics, definition=session.definition,
            planner=StaticPlanner(GRAPH), global_verifier=LlmGlobalVerifier(provider, MC),
        )
        if failure == "evidence_limit":
            master._verification_limits = VerificationLimits(max_evidence_bytes=10)
        master._max_replans = 0
        result = await master.run("保留全部要求并修改文件", session_id=session.session_id)
        assert result.accepted is (failure == "success")
        assert result.integrated is (failure == "success")
        assert (repo / "note.txt").exists() is (failure == "success")
        assert (_git_out(repo, "rev-parse", "HEAD") != before) is (failure == "success")
        assert _worktree_count(repo) == 1
        assert client.call_count == 2
        if failure == "evidence_limit":
            assert not provider.calls


async def test_indeterminate_global_result_stops_automatic_replanning(tmp_path):
    from codeagent.orchestration.global_verifier import GlobalVerdict

    class Unavailable:
        async def verify(self, task, graph, results, target=None):
            return GlobalVerdict(accept=False, indeterminate=True, reason="unavailable")

    runtime = FakeRuntime()
    master = _master(GRAPH, runtime, Unavailable(), max_replans=3, tmp=tmp_path)
    result = await master.run("original constraints", session_id="s")
    assert not result.accepted and result.replans == 0 and len(runtime.trace_ids) == 1


def test_verification_limits_and_env_are_validated(tmp_path, monkeypatch):
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            VerificationLimits(max_batches=cast(int, value))
    monkeypatch.setenv("CODEAGENT_VERIFICATION_MAX_BATCHES", "7")
    monkeypatch.setenv("CODEAGENT_VERIFICATION_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("CODEAGENT_VERIFICATION_MAX_EVIDENCE_BYTES", "900")
    config = AppConfig.from_env(tmp_path)
    assert config.verification == VerificationLimits(7, 8, 900)
