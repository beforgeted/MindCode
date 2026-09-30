from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.cli.app import _handle_command
from codeagent.config import AppConfig
from codeagent.context.compact.chunker import CompactionChunk
from codeagent.context.compact.history_compactor import ConversationHistoryCompactor
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import current_trace, trace_scope
from codeagent.llm.client import LlmError
from codeagent.llm.observed_client import ObservedLlmClient
from codeagent.llm.stub_client import StubLlmClient
from codeagent.llm.types import LlmResponse, ModelConfig, Usage
from codeagent.observability import JsonTrajectoryExporter
from codeagent.orchestration.master_session import build_master
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.run_store import (
    AttemptRecord,
    AttemptState,
    SqliteRunStore,
    StepOutcome,
)
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.session import AgentSession


class RoleClient:
    """无需网络，根据装配赋予的角色返回实际可解析结果。"""

    async def chat(self, messages, *, model_config, tools=()):
        await asyncio.sleep(0)  # 并发 Worker 必须跨越 await，检查上下文不会串线。
        role = current_trace().get("role")
        if role == "planner":
            data = {"steps": [{"id": name, "instruction": name, "read_only": True}
                              for name in ("a", "b")]}
        elif role == "local_verifier":
            data = {"ok": True, "reason": "ok", "feedback": ""}
        elif role == "global_verifier":
            data = {"accept": True, "reason": "ok"}
        elif role == "judge":
            data = {"shouldRemember": False, "scope": "project", "type": "fact",
                    "content": "skip", "importance": 1, "confidence": 0.1, "rationale": "test"}
        elif role in ("compact_map", "compact_reduce"):
            data = {"goal": "test", "constraints": [], "decisions": [], "completed_work": [],
                    "files": [], "tests": [], "failed_attempts": [], "open_issues": [],
                    "next_steps": [], "evidence_refs": []}
            if role == "compact_reduce":
                data.update(version=1, updated_at="2026-09-30T00:00:00+00:00")
        else:
            data = {"done": True}
        return LlmResponse("provider-id", json.dumps(data), usage=Usage(10, 2, 3, 4))

    async def count_tokens(self, messages, *, model_config, tools=()):
        return None


def _config(tmp_path):
    return AppConfig(workspace_root=tmp_path, home=tmp_path / ".home", model="test-model",
                     use_stub_llm=False, profile=replace(ContextProfile(), context_window=20_000))


async def _read_report(path: str):
    return json.loads(await asyncio.to_thread(Path(path).read_text, encoding="utf-8"))


async def _master(session, client):
    return await build_master(
        config=session.config, llm_client=client, engine=session.engine,
        event_store=session.event_store, metrics=session.metrics, definition=session.definition,
        artifact_store=session.artifact_store,
    )


async def test_parallel_roles_task_totals_and_resume_in_new_session(tmp_path, capsys):
    config, client = _config(tmp_path), RoleClient()
    async with AgentSession(config, llm_client=client) as session:
        await session.send("unrelated single-agent conversation")
        before = session.metrics.counters["llm.input_tokens"]
        master = await _master(session, client)
        first = await master.run("parallel", session_id=session.session_id)
        assert first.integrated and first.trajectory_path
        report = await _read_report(first.trajectory_path)
        assert report["totals"]["calls"] == 6  # planner + 2 workers + 2 local + global
        delta = session.metrics.counters["llm.input_tokens"] - before
        assert report["totals"]["input_tokens"] == delta
        assert set(report["by_role"]) == {"planner", "worker", "local_verifier", "global_verifier"}
        assert report["totals"]["cache_read_tokens"] == 18
        assert report["totals"]["cost"] is None
        for step in ("a", "b"):
            assert report["by_step"][step]["calls"] == 2
            calls = [c for c in report["llm_calls"] if c["trace"].get("step_id") == step]
            assert len({c["trace"]["agent_run_id"] for c in calls}) == 1
        assert len(report["by_worker"]) == 3  # 两个 Worker + 非 Worker 调用
        assert len(report["steps"]) == 2
        assert current_trace() == {}
        second = await master.run("another task", session_id=session.session_id)
        assert second.trajectory_path
        report2 = await _read_report(second.trajectory_path)
        assert report2["totals"]["input_tokens"] == 60
        assert report2["session_cumulative_snapshot"]["counters"]["llm.input_tokens"] == 130
        await _handle_command(
            session, f"/trajectory {first.master_run_id}", config=config, client=client,
        )
        assert "轨迹导出" in capsys.readouterr().out
    async with AgentSession(config, llm_client=RoleClient()) as session:
        master = await _master(session, session.llm_client)
        recovered = await master.run("", session_id=session.session_id,
                                     resume_master_run_id=first.master_run_id)
        assert recovered.trajectory_path
        report = await _read_report(recovered.trajectory_path)
        assert report["totals"]["calls"] == 6  # 恢复不能重计旧调用
        assert report["session_cumulative_snapshot"]["counters"].get("llm.calls", 0) == 0


async def test_failed_cancelled_calls_and_legacy_events_do_not_gain_trace(tmp_path):
    events = JsonlEventStore(tmp_path)
    metrics = Metrics()

    class FailureClient(RoleClient):
        async def chat(self, messages, *, model_config, tools=()):
            if model_config.model == "cancel":
                raise asyncio.CancelledError()
            raise LlmError("secret should not be persisted")

    events.append_nowait(AgentEvent(EventType.USER_MESSAGE, "old", payload={"text": "old"}))
    observed = ObservedLlmClient(FailureClient(), metrics=metrics, events=events, session_id="s")
    for model, exception in (("error", LlmError), ("cancel", asyncio.CancelledError)):
        with trace_scope(master_run_id="m", role="worker"):
            with pytest.raises(exception):
                await observed.chat([], model_config=ModelConfig(model=model))
    with trace_scope(master_run_id="m"):
        old = await events.query("old")
        assert "trace" not in old[0].payload
    calls = await events.query("s", types=[EventType.LLM_CALL])
    assert [e.payload["status"] for e in calls] == ["error", "cancelled"]
    assert all(e.payload["usage"] is None for e in calls)
    assert "secret" not in json.dumps([e.payload for e in calls])
    assert metrics.counters["llm.attempts"] == 2 and metrics.counters["llm.calls"] == 0
    await events.aclose()


async def test_compaction_roles_preserve_parent_trace(tmp_path):
    events = JsonlEventStore(tmp_path)
    observed = ObservedLlmClient(RoleClient(), metrics=Metrics(), events=events, session_id="s")
    compactor = ConversationHistoryCompactor(observed, HeuristicTokenEstimator(), ModelConfig())
    with trace_scope(master_run_id="m", attempt_no=2, step_id="a", agent_run_id="w", role="worker"):
        delta = await compactor._mapper.summarize(
            CompactionChunk((), (), 0), max_output_tokens=1000, focus=None,
        )
        await compactor._reducer.reduce(None, (delta,), max_output_tokens=1000, focus=None)
        assert current_trace()["role"] == "worker"
    calls = await events.query("s", types=[EventType.LLM_CALL])
    assert [c.payload["role"] for c in calls] == ["compact_map", "compact_reduce"]
    assert all(c.payload["trace"]["attempt_no"] == 2 for c in calls)
    await events.aclose()


async def test_export_failure_does_not_change_task_outcome(tmp_path, monkeypatch):
    async with AgentSession(_config(tmp_path), llm_client=RoleClient()) as session:
        master = await _master(session, session.llm_client)

        async def fail(*args, **kwargs):
            raise OSError("disk unavailable")

        monkeypatch.setattr(master._trajectory_exporter, "export", fail)
        final = await master.run("task", session_id=session.session_id)
        assert final.integrated and final.trajectory_error == "OSError"
        assert session.metrics.counters["observability.export_failures"] == 1


async def test_stalled_export_has_bounded_wait(tmp_path, monkeypatch):
    async with AgentSession(_config(tmp_path), llm_client=RoleClient()) as session:
        master = await _master(session, session.llm_client)
        master._trajectory_timeout = 0.01

        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(master._trajectory_exporter, "export", stalled)
        async with asyncio.timeout(2):
            final = await master.run("task", session_id=session.session_id)
        assert final.integrated and final.trajectory_error == "TimeoutError"


async def test_transition_history_and_step_attempts_survive_reopen(tmp_path):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    await store.start()
    await store.save_run(master_run_id="m", session_id="s", task="t", status="running",
                         graph=TaskGraph([Step("a", "default", "a")]))
    for number in (1, 2):
        await store.save_attempt("m", AttemptRecord(number, state=AttemptState.RUNNING))
        await store.update_attempt("m", number, state=AttemptState.VERIFYING)
        await store.update_attempt("m", number, state=AttemptState.VERIFYING)
        await store.record_step("m", StepOutcome("a", "failed", attempt_no=number,
                                                agent_run_id=f"w{number}"))
    reopened = SqliteRunStore(path)
    await reopened.start()
    data = await reopened.load_observations("m")
    assert [s["attempt_no"] for s in data["steps"]] == [1, 2]
    assert len(data["transitions"]) == 4

    def install_fault():
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TRIGGER reject_transition BEFORE INSERT ON attempt_transition
                            BEGIN SELECT RAISE(ABORT, 'failure'); END""")

    await asyncio.to_thread(install_fault)
    with pytest.raises(RuntimeError):
        await reopened.update_attempt("m", 2, state=AttemptState.PROMOTING)
    record = await reopened.load_run("m")
    assert record and record.last_attempt and record.last_attempt.state == AttemptState.VERIFYING


async def test_legacy_schema_migration_and_report_path_validation(tmp_path):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    await store.start()
    await store.save_run(master_run_id="old", session_id="s", task="old", status="success",
                         graph=TaskGraph([Step("a", "default", "a")]))

    def remove_new_tables():
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE attempt_transition")
            conn.execute("DROP TABLE step_execution")

    await asyncio.to_thread(remove_new_tables)
    reopened = SqliteRunStore(path)
    await reopened.start()
    events = JsonlEventStore(tmp_path)
    exporter = JsonTrajectoryExporter(tmp_path, reopened, events)
    report = await exporter.build("old")
    assert report["coverage"]["run_persisted"]
    assert not report["coverage"]["attempt_history_available"]
    assert not report["coverage"]["llm_events_available"]
    with pytest.raises(ValueError):
        await exporter.export("../escape")
    await events.aclose()


async def test_git_trajectory_contains_state_timeline_and_tool_results(tmp_path):
    def init_repo():
        for args in (["init"], ["config", "user.name", "test"],
                     ["config", "user.email", "test@example.com"]):
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
        (tmp_path / "seed.txt").write_text("seed")
        for args in (["add", "seed.txt"], ["commit", "-m", "seed"]):
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    await asyncio.to_thread(init_repo)
    client = StubLlmClient([[('write_file', {"path": "a.txt", "content": "A"})], "done"])
    config = replace(_config(tmp_path), use_stub_llm=True)
    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics, definition=session.definition,
            planner=StaticPlanner(TaskGraph([Step("a", "default", "write a.txt")])),
        )
        final = await master.run("write a.txt", session_id=session.session_id)
        assert final.integrated and final.trajectory_path
        report = await _read_report(final.trajectory_path)
        assert [t["state"] for t in report["transitions"]] == [
            "running", "candidate_frozen", "verifying", "verified", "promoting", "promoted",
        ]
        assert all(t["duration_ms"] >= 0 for t in report["transitions"][:-1])
        assert report["transitions"][-1]["duration_ms"] is None
        assert len(report["tools"]) == 2  # 执行器 call/result，不重复统计 History 协议消息
        result = next(t for t in report["tools"] if t["type"] == "tool_result")
        assert result["name"] == "write_file" and result["duration_ms"] >= 0
        assert result["trace"]["step_id"] == "a"
        assert report["steps"][0]["agent_run_id"] == result["agent_run_id"]


async def test_event_sink_failure_does_not_mask_model_result(tmp_path):
    from codeagent.evidence.event_store import NullEventStore

    class FailingEvents(NullEventStore):
        def append_nowait(self, event):
            raise OSError("event disk failure")

    metrics = Metrics()
    client = ObservedLlmClient(
        RoleClient(), metrics=metrics, events=FailingEvents(), session_id="s",
    )
    response = await client.chat([], model_config=ModelConfig())
    assert response.usage.input_tokens == 10
    assert metrics.counters["observability.event_failures"] == 1
