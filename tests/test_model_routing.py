from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from codeagent.config import AppConfig
from codeagent.context.compact.chunker import CompactionChunk
from codeagent.context.compact.history_compactor import ConversationHistoryCompactor
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.evidence.cursor import SequencedEvent
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import current_trace, trace_scope
from codeagent.llm.client import LlmError, LlmErrorKind, provider_error_kind
from codeagent.llm.observed_client import RoleLlmClient
from codeagent.llm.routing import (
    ModelRole,
    ModelRoutingConfig,
    RoutingLlmClient,
    StaticModelRouter,
    attach_routing,
)
from codeagent.llm.types import LlmResponse, ModelConfig, Usage
from codeagent.memory.candidate_extractor import ConservativeCandidateExtractor
from codeagent.orchestration.master_session import build_master
from codeagent.session import AgentSession


class Provider:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []
        self.counted = []

    async def chat(self, messages, *, model_config, tools=()):
        self.calls.append((model_config, tuple(tools)))
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        return LlmResponse("id", "ok", usage=Usage(10, 2))

    async def count_tokens(self, messages, *, model_config, tools=()):
        self.counted.append(model_config)
        return 17


def test_default_router_preserves_all_model_parameters():
    base = ModelConfig(model="custom", map_model="cheap", temperature=0.2,
                       max_output_tokens=77, context_window=3000)
    router = StaticModelRouter(ModelRoutingConfig())
    for role in ModelRole:
        assert router.resolve(role, base=base) == base
        assert router.candidates(role, base=base) == (base,)


def test_environment_roles_and_explicit_empty_fallback(monkeypatch, tmp_path):
    for role in ModelRole:
        monkeypatch.setenv(f"CODEAGENT_MODEL_{role.name}", f"model-{role.value}")
    monkeypatch.setenv("CODEAGENT_MODEL_FALLBACK", "backup-a; backup-b")
    monkeypatch.setenv("CODEAGENT_MODEL_FALLBACK_GLOBAL_VERIFIER", "")
    config = AppConfig.from_env(tmp_path)
    router = StaticModelRouter(config.models)
    for role in ModelRole:
        assert router.resolve(role, base=ModelConfig()).model == f"model-{role.value}"
    assert len(router.candidates(ModelRole.WORKER, base=ModelConfig())) == 3
    assert len(router.candidates(ModelRole.GLOBAL_VERIFIER, base=ModelConfig())) == 1


@pytest.mark.parametrize("kind", [LlmErrorKind.RATE_LIMIT, LlmErrorKind.TIMEOUT,
                                  LlmErrorKind.UNAVAILABLE])
async def test_cross_provider_fallback_is_observed_once_per_attempt(tmp_path, kind):
    first, second = Provider(LlmError("temporary", kind=kind)), Provider()
    config = ModelRoutingConfig(worker="a:main", fallback=("b:backup",))
    raw = RoutingLlmClient({"a": first, "b": second}, StaticModelRouter(config),
                           default_provider="a")
    metrics, events = Metrics(), JsonlEventStore(tmp_path)
    client = attach_routing(
        raw, ModelRoutingConfig(), metrics=metrics, events=events, session_id="s",
    )
    with trace_scope(session_id="s", master_run_id="m", role="worker", step_id="x"):
        response = await client.chat([], model_config=ModelConfig(max_output_tokens=70))
    assert response.content == "ok"
    assert first.calls[0][0].model == "main" and second.calls[0][0].model == "backup"
    assert second.calls[0][0].max_output_tokens == 70
    records = await events.query("s", types=[EventType.LLM_CALL])
    assert [r.payload["status"] for r in records] == ["error", "success"]
    assert [r.payload["provider"] for r in records] == ["a", "b"]
    assert len({r.payload["trace"]["route_id"] for r in records}) == 1
    assert metrics.counters["llm.attempts"] == 2 and metrics.counters["llm.calls"] == 1
    assert metrics.counters["llm.input_tokens"] == 10 and metrics.counters["llm.fallbacks"] == 1
    switches = await events.query("s", types=[EventType.MODEL_FALLBACK])
    assert len(switches) == 1 and switches[0].payload["reason"] == kind
    assert current_trace() == {}
    await events.aclose()


@pytest.mark.parametrize("failure", [
    LlmError("auth", kind=LlmErrorKind.AUTH),
    LlmError("invalid", kind=LlmErrorKind.INVALID_REQUEST),
    LlmError("context", kind=LlmErrorKind.CONTEXT_LIMIT),
    LlmError("unknown"), ValueError("bug"), asyncio.CancelledError(),
])
async def test_terminal_errors_and_cancellation_never_fallback(failure):
    first, backup = Provider(failure), Provider()
    client = RoutingLlmClient({"a": first, "b": backup}, StaticModelRouter(
        ModelRoutingConfig(worker="a:main", fallback=("b:backup",)),
    ), default_provider="a")
    with trace_scope(role="worker"), pytest.raises(type(failure)):
        await client.chat([], model_config=ModelConfig())
    assert len(first.calls) == 1 and not backup.calls


async def test_unknown_provider_fails_before_any_call():
    provider = Provider()
    client = RoutingLlmClient({"a": provider}, StaticModelRouter(
        ModelRoutingConfig(worker="a:main", fallback=("missing:backup",)),
    ), default_provider="a")
    with trace_scope(role="worker"), pytest.raises(LlmError) as error:
        await client.chat([], model_config=ModelConfig())
    assert error.value.kind == LlmErrorKind.CONFIGURATION and not provider.calls


async def test_duplicate_targets_and_exhaustion_are_bounded():
    provider = Provider(LlmError("busy", kind=LlmErrorKind.RATE_LIMIT))
    client = RoutingLlmClient({"a": provider}, StaticModelRouter(
        ModelRoutingConfig(worker="main", fallback=("a:main", "backup", "a:backup")),
    ), default_provider="a")
    with trace_scope(role="worker"), pytest.raises(LlmError):
        await client.chat([], model_config=ModelConfig())
    assert [c[0].model for c in provider.calls] == ["main", "backup"]
    with pytest.raises(ValueError):
        ModelRoutingConfig(fallback=("a", "b", "c", "d"))


async def test_count_tokens_uses_role_but_does_not_fallback():
    provider = Provider()
    client = RoutingLlmClient({"a": provider}, StaticModelRouter(
        ModelRoutingConfig(compact_map="a:small", fallback=("a:backup",)),
    ), default_provider="a")
    result = await RoleLlmClient(client, "compact_map").count_tokens([], model_config=ModelConfig())
    assert result == 17 and provider.counted[0].model == "small" and not provider.calls


@pytest.mark.parametrize("status, expected", [(429, "rate_limit"), (401, "auth"),
                                               (404, "invalid_request"), (503, "unavailable"),
                                               (504, "timeout"), (400, "invalid_request")])
def test_provider_error_classification_uses_status_not_message(status, expected):
    class StatusError(RuntimeError):
        status_code: int

    error = StatusError("ignore these words: timeout rate limit")
    error.status_code = status
    assert provider_error_kind(error) == expected
    assert provider_error_kind(RuntimeError("timeout rate limit")) == LlmErrorKind.UNKNOWN


async def test_composition_routes_planner_worker_verifiers_and_compression(tmp_path):
    class Script(Provider):
        async def chat(self, messages, *, model_config, tools=()):
            self.calls.append((model_config, tuple(tools)))
            if model_config.model == "planner":
                raise LlmError("limited", kind=LlmErrorKind.RATE_LIMIT)
            role = current_trace().get("role")
            if role == "planner":
                content = '{"steps":[{"id":"a","instruction":"inspect","read_only":true}]}'
            elif role == "local_verifier":
                content = '{"ok":true}'
            elif role == "global_verifier":
                content = '{"accept":true}'
            elif role == "judge":
                content = json.dumps({
                    "shouldRemember": False, "scope": "project", "type": "fact",
                    "content": "skip", "importance": 1, "confidence": 0.1, "rationale": "test",
                })
            elif role in ("compact_map", "compact_reduce"):
                data = {"goal": "test", "constraints": [], "decisions": [], "completed_work": [],
                        "files": [], "tests": [], "failed_attempts": [], "open_issues": [],
                        "next_steps": [], "evidence_refs": []}
                if role == "compact_reduce":
                    data.update(version=1, updated_at="2026-09-30T00:00:00+00:00")
                content = json.dumps(data)
            else:
                content = "done"
            return LlmResponse("id", content, usage=Usage(10, 2))

    routing = ModelRoutingConfig(
        planner="planner", worker="worker", local_verifier="local_verifier",
        global_verifier="global_verifier", judge="judge", compact_map="compact_map",
        compact_reduce="compact_reduce", fallbacks={"planner": ("planner-backup",)},
    )
    config = AppConfig(workspace_root=tmp_path, home=tmp_path / ".home", model="default",
                       models=routing, profile=replace(ContextProfile(), context_window=20_000))
    raw = Script()
    async with AgentSession(config, llm_client=raw) as session:
        master = await build_master(
            config=config, llm_client=raw, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics, definition=session.definition,
        )
        final = await master.run("inspect", session_id=session.session_id)
        assert final.integrated
        compactor = ConversationHistoryCompactor(session.llm_client, HeuristicTokenEstimator(),
                                                 ModelConfig(model="default"))
        delta = await compactor._mapper.summarize(CompactionChunk((), (), 0),
                                                 max_output_tokens=77, focus=None)
        await compactor._reducer.reduce(None, (delta,), max_output_tokens=88, focus=None)
        candidates = ConservativeCandidateExtractor().extract(
            (SequencedEvent(0, AgentEvent(EventType.USER_MESSAGE, session.session_id,
                                         payload={"text": "项目固定使用 Python 3.11"})),),
            project_id="p",
        )
        assert candidates and session.governance._judge is not None
        await session.governance._judge.judge(candidates[0])
        expected = {role.value for role in ModelRole} | {"planner-backup"}
        assert {c[0].model for c in raw.calls} == expected
        assert next(c[0] for c in raw.calls if c[0].model == "compact_map").max_output_tokens == 77
        assert next(c[0] for c in raw.calls if c[0].model == "compact_reduce").temperature == 0
