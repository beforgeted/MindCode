"""Exact-count boundary sampling, isolation, and conservative publication budgets."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codeagent.config import AppConfig
from codeagent.context.calibration_config import CalibrationConfig
from codeagent.context.compact.request_budget import request_fits
from codeagent.context.history.conversation_history import ConversationHistory
from codeagent.context.manager import ContextManager, ContextOverflowError
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import (
    CalibratedTokenEstimator,
    HeuristicTokenEstimator,
    estimate_request,
    estimate_tools,
)
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import EventType
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import trace_scope
from codeagent.llm.anthropic_client import AnthropicLlmClient
from codeagent.llm.client import LlmError, LlmErrorKind
from codeagent.llm.message import ImageBlock, Message, Role, ToolResultBlock, ToolUseBlock
from codeagent.llm.observed_client import RoleLlmClient
from codeagent.llm.request_budget import RequestBudgetError, check_request
from codeagent.llm.routing import (
    ModelRoutingConfig,
    RoutingLlmClient,
    StaticModelRouter,
    attach_routing,
)
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec
from codeagent.observability import JsonTrajectoryExporter
from codeagent.orchestration.global_verifier import LlmGlobalVerifier
from codeagent.orchestration.planner import LlmPlanner
from codeagent.orchestration.run_store import NullRunStore
from codeagent.runtime.local_verifier import LlmLocalVerifier
from codeagent.session import AgentSession

MC = ModelConfig(model='a:main', context_window=1_000, max_output_tokens=50)
TOOLS = (ToolSpec('inspect', 'Read code', {'type': 'object', 'properties': {
    'path': {'type': 'string', 'description': '文件路径'}}}),)
NEAR = (Message.user('中' * 680),)
FAST = CalibrationConfig(min_interval_seconds=0, timeout_seconds=0.1)


class Provider:
    def __init__(self, exact=None, *, ratio=1.1, failure=None, delay: float = 0):
        self.exact, self.ratio, self.failure, self.delay = exact, ratio, failure, delay
        self.counts = []
        self.chats = []
        self.entered = asyncio.Event()

    async def count_tokens(self, messages, *, model_config, tools=()):
        self.counts.append((model_config, tuple(tools)))
        self.entered.set()
        await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        if self.exact is not None:
            return self.exact
        raw = HeuristicTokenEstimator().estimate(messages) + estimate_tools(tools)
        return int(raw * self.ratio)

    async def chat(self, messages, *, model_config, tools=()):
        self.chats.append(model_config)
        return LlmResponse('fixed', 'ok')


def routed(provider=None, *, policy=FAST, route=None):
    provider = provider or Provider()
    client = RoutingLlmClient({'a': provider}, StaticModelRouter(route or ModelRoutingConfig()),
                             default_provider='a', calibration=policy)
    return client, provider


@pytest.mark.parametrize('payload', ['中' * 100, 'def f():\n    return {}\n' * 40])
async def test_conservative_calibration_keeps_message_cache_raw_and_never_moves_down(payload):
    provider = Provider(ratio=2)
    bank = CalibratedTokenEstimator(provider, config=FAST)
    messages = (Message.user(payload),)
    raw = bank.estimate(messages)
    await bank.count_exact(messages, model_config=MC)
    raised = estimate_request(bank, messages, MC)
    assert raised >= raw * 2 and messages[0].token_estimate == raw
    provider.ratio = 0.5
    await bank.count_exact((Message.user(payload + '!'),), model_config=MC)
    assert estimate_request(bank, messages, MC) == raised
    assert estimate_request(bank, messages, replace(MC, model='a:other')) == raw
    assert estimate_request(bank, messages, replace(MC, model='b:main')) == raw


async def test_tool_schema_is_in_sample_basis_and_does_not_pollute_text_cohort():
    provider = Provider(ratio=1)
    bank = CalibratedTokenEstimator(provider, config=FAST)
    messages = (Message.user('inspect'),)
    raw = bank.estimate(messages)
    exact = await bank.count_exact(messages, model_config=MC, tools=TOOLS)
    assert exact == raw + estimate_tools(TOOLS)
    assert bank.scale_for(MC, messages, TOOLS) == pytest.approx(1.05)
    assert bank.scale_for(MC, messages) == 1
    assert provider.counts[0][1] == TOOLS


async def test_tool_protocol_images_and_text_have_separate_scales():
    bank = CalibratedTokenEstimator(Provider(ratio=3), config=FAST)
    text = (Message.user('hello'),)
    image = (Message(Role.USER, (ImageBlock('image/png', data='base64'),)),)
    protocol = (Message.assistant([ToolUseBlock('id', 'inspect', {})]),
                Message.tool([ToolResultBlock('id', 'contents')]))
    await bank.count_exact(image, model_config=MC)
    assert bank.scale_for(MC, image) > 3
    assert bank.scale_for(MC, text) == bank.scale_for(MC, protocol) == 1
    await bank.count_exact(protocol, model_config=MC)
    assert bank.scale_for(MC, protocol) > 3 and bank.scale_for(MC, text) == 1


async def test_short_hot_loop_does_not_count_and_near_window_duplicate_hits_cache():
    client, provider = routed()
    for _ in range(20):
        await client.chat((Message.user('hello'),), model_config=MC)
    assert not provider.counts
    for _ in range(10):
        await client.chat(NEAR, model_config=MC)
    assert len(provider.counts) == 1 and len(provider.chats) == 30


async def test_exact_and_conservative_overflow_prevent_chat_without_truncation():
    client, provider = routed(Provider(exact=2_000))
    with pytest.raises(LlmError) as error:
        await client.chat(NEAR, model_config=MC)
    assert error.value.kind == LlmErrorKind.CONTEXT_LIMIT and not provider.chats
    assert NEAR[0].text == '中' * 680
    with pytest.raises(RequestBudgetError):
        check_request(NEAR, MC, client.token_estimator(MC))
    assert not request_fits(NEAR, MC, client.token_estimator(MC))


async def test_concurrent_requests_await_sample_and_both_reject_underestimate():
    client, provider = routed(Provider(exact=2_000, delay=0.02))
    results = await asyncio.gather(*(client.chat(NEAR, model_config=MC) for _ in range(2)),
                                   return_exceptions=True)
    assert all(isinstance(r, LlmError) and r.kind == LlmErrorKind.CONTEXT_LIMIT for r in results)
    assert len(provider.counts) == 1 and not provider.chats


async def test_request_below_initial_boundary_also_waits_for_in_flight_calibration():
    client, provider = routed(Provider(exact=2_000, delay=0.02))
    first = asyncio.create_task(client.chat(NEAR, model_config=MC))
    await provider.entered.wait()
    second = asyncio.create_task(client.chat((Message.user('中' * 600),), model_config=MC))
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(r, LlmError) and r.kind == LlmErrorKind.CONTEXT_LIMIT for r in results)
    assert len(provider.counts) == 1 and not provider.chats


async def test_context_boundary_samples_pruned_request_before_overflow_decision():
    from codeagent.context.prune.image_pruner import ImagePayloadPruner

    bank = CalibratedTokenEstimator(Provider(ratio=3), config=FAST)
    history = ConversationHistory(session_id='s', agent_run_id='r')
    old = history.begin_turn()
    history.append(Message(Role.USER, (ImageBlock('image/png', data='base64', summary='old'),),
                           turn_id=old))
    history.end_turn()
    current = history.begin_turn()
    history.append(Message.user('中' * 400, turn_id=current))
    sampled = []

    async def sampler(messages):
        sampled.extend(messages)
        return await bank.count_exact(messages, model_config=MC)

    manager = ContextManager(estimator=bank, image_pruner=ImagePayloadPruner(bank))
    with pytest.raises(ContextOverflowError):
        await manager.prepare(history, ContextProfile(context_window=1_000),
                              model_config=MC, token_sampler=sampler)
    assert sampled and all(b.data is None for m in sampled for b in m.images)
    assert bank.scale_for(MC, sampled) > 3


async def test_network_failures_are_not_chat_fallback_and_have_bounded_retry():
    client, provider = routed(Provider(failure=LlmError('busy', kind=LlmErrorKind.TIMEOUT)))
    for i in range(10):
        await client.chat((Message.user('中' * (680 + i)),), model_config=MC)
    assert len(provider.counts) == 3 and len(provider.chats) == 10
    assert client.calibration.calibrations == 0


async def test_cooldown_applies_across_changed_requests():
    client, provider = routed(policy=replace(FAST, min_interval_seconds=60))
    await client.chat(NEAR, model_config=MC)
    await client.chat((Message.user('中' * 681),), model_config=MC)
    assert len(provider.counts) == 1


async def test_unsupported_is_sampled_once_and_not_treated_as_zero():
    class Unsupported(Provider):
        async def count_tokens(self, *args, **kwargs):
            await super().count_tokens(*args, **kwargs)
            return None

    client, provider = routed(Unsupported())
    for i in range(5):
        await client.chat((Message.user('中' * (680 + i)),), model_config=MC)
    assert len(provider.counts) == 1 and len(provider.chats) == 5
    assert client.calibration.calibrations == 0


@pytest.mark.parametrize('bad', [True, -1, 0, 2.5, '800'])
async def test_invalid_count_cannot_change_multiplier(bad):
    provider = Provider(exact=bad)
    bank = CalibratedTokenEstimator(provider, config=FAST)
    assert await bank.count_exact(NEAR, model_config=MC) is None
    assert bank.scale_for(MC, NEAR) == 1 and bank.calibrations == 0


async def test_timeout_retains_heuristic_and_cancellation_propagates():
    records = []
    provider = Provider(delay=10)
    bank = CalibratedTokenEstimator(provider, config=replace(FAST, timeout_seconds=0.01),
                                    record=records.append)
    assert await bank.count_exact(NEAR, model_config=MC) is None
    assert records[-1]['status'] == 'timeout' and bank.calibrations == 0
    provider.entered.clear()
    task = asyncio.create_task(bank.count_exact((Message.user('changed'),), model_config=MC))
    await provider.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records[-1]['status'] == 'cancelled'
    assert not bank._states[bank._key(MC, NEAR, ())].lock.locked()


async def test_global_limit_and_cohort_limit_cannot_reset_by_eviction():
    provider = Provider()
    bank = CalibratedTokenEstimator(
        provider, config=replace(FAST, max_cohorts=2, max_total_calls=2),
    )
    for model in ('a:one', 'a:two', 'a:three', 'a:one'):
        await bank.count_exact((Message.user(model),), model_config=replace(MC, model=model))
    assert len(provider.counts) == 2 and len(bank._states) == 2


async def test_cache_key_covers_role_tools_and_image_contents_and_cache_is_bounded():
    bank = CalibratedTokenEstimator(Provider(ratio=1), config=replace(FAST, cache_size=2))
    messages = (Message.user('same'),)
    await bank.count_exact(messages, model_config=MC)
    await bank.count_exact(messages, model_config=MC, tools=TOOLS)
    await bank.count_exact((Message.assistant(messages[0].blocks),), model_config=MC)
    assert bank.calls == 3 and len(bank._cache) == 2
    await bank.count_exact((Message(Role.USER, (ImageBlock('image/png', data='one'),)),),
                           model_config=MC)
    await bank.count_exact((Message(Role.USER, (ImageBlock('image/png', data='two'),)),),
                           model_config=MC)
    assert bank.calls == 5 and len(bank._cache) == 2


async def test_disabled_policy_performs_no_sampling():
    client, provider = routed(policy=replace(FAST, enabled=False))
    await client.chat(NEAR, model_config=MC)
    assert not provider.counts


async def test_role_resolves_provider_model_before_sampling_and_estimation():
    provider = Provider()
    client, _ = routed(provider, route=ModelRoutingConfig(planner='a:plan'))
    role = RoleLlmClient(client, 'planner')
    view = role.token_estimator(MC)
    await role.chat(NEAR, model_config=MC)
    assert provider.counts[0][0].model == 'plan'
    assert view.estimate(NEAR) > HeuristicTokenEstimator().estimate(NEAR)
    assert client.calibration.scale_for(MC, NEAR) == 1


async def test_fallback_samples_actual_provider_and_model_without_cross_contamination():
    class Busy(Provider):
        async def chat(self, *args, **kwargs):
            await super().chat(*args, **kwargs)
            raise LlmError('busy', kind=LlmErrorKind.RATE_LIMIT)

    first, second = Busy(ratio=1), Provider(ratio=1.1)
    client = RoutingLlmClient({'a': first, 'b': second}, StaticModelRouter(
        ModelRoutingConfig(fallback=('b:main',))), default_provider='a', calibration=FAST)
    with trace_scope(role='worker'):
        await client.chat(NEAR, model_config=MC)
    assert first.counts[0][0].model == second.counts[0][0].model == 'main'
    assert client.calibration.scale_for(MC, NEAR) < client.calibration.scale_for(
        replace(MC, model='b:main'), NEAR)


async def test_composition_shares_sample_budget_and_all_role_request_guards(tmp_path):
    client, provider = routed()
    cfg = AppConfig(tmp_path, tmp_path / '.state', model='a:main', calibration=FAST)
    async with AgentSession(cfg, llm_client=client) as session:
        again = attach_routing(session.llm_client, cfg.models, metrics=session.metrics,
                               events=session.event_store, session_id='s', calibration=FAST)
        assert again.calibration is session.llm_client.calibration
        assert again.calibration is not client.calibration
        for ctor, name in ((LlmPlanner, 'planner'), (LlmLocalVerifier, 'local_verifier'),
                           (LlmGlobalVerifier, 'global_verifier')):
            component = ctor(RoleLlmClient(again, name), MC)
            provider.exact = 2_000
            await again.calibration.count_exact(NEAR, model_config=MC)
            with pytest.raises(RequestBudgetError):
                check_request(NEAR, MC, component._estimator)


async def test_worker_context_uses_model_bound_multiplier_and_keeps_history():
    bank = CalibratedTokenEstimator(Provider(ratio=3), config=FAST)
    await bank.count_exact(NEAR, model_config=MC)
    history = ConversationHistory(session_id='s', agent_run_id='r')
    history.begin_turn()
    history.append(Message.user('中' * 400, turn_id=history.current_turn_id))
    original = history.snapshot()
    manager = ContextManager(estimator=bank)
    with pytest.raises(ContextOverflowError):
        await manager.prepare(history, ContextProfile(context_window=1_000), model_config=MC)
    assert history.snapshot() == original
    result = await manager.prepare(history, ContextProfile(context_window=1_000),
                                   model_config=replace(MC, model='b:main'))
    assert result.tokens_final < 500


async def test_observations_export_without_prompt_payload_or_chat_billing(tmp_path):
    events, metrics = JsonlEventStore(tmp_path), Metrics()
    raw, _ = routed(Provider(exact=2_000))
    client = attach_routing(raw, ModelRoutingConfig(), metrics=metrics, events=events,
                            session_id='s')
    with trace_scope(session_id='s', master_run_id='m', role='worker'):
        with pytest.raises(LlmError):
            await client.chat(NEAR, model_config=MC)
    report = await JsonTrajectoryExporter(tmp_path, NullRunStore(), events).build('m')
    assert report['token_calibrations'][0]['exact'] == 2_000
    assert report['llm_calls'] == [] and metrics.counters['tokens.count.attempts'] == 1
    assert '中' * 10 not in json.dumps(report)
    records = await events.query('s', types=[EventType.TOKEN_CALIBRATION])
    assert len(records) == 1
    await events.aclose()


async def test_anthropic_count_uses_same_serialized_system_tools_and_internal_context(monkeypatch):
    from codeagent.llm.message import ContextCategory

    captured = []

    async def count(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(input_tokens=800)

    adapter = object.__new__(AnthropicLlmClient)
    monkeypatch.setattr(
        adapter, '_client', SimpleNamespace(messages=SimpleNamespace(count_tokens=count)),
        raising=False,
    )
    adapter._count_params = None
    messages = (Message.system('sys'),
                Message.internal_context('state', ContextCategory.CHECKPOINT),
                Message.user('hello'))
    assert await adapter.count_tokens(messages, model_config=replace(MC, model='main'),
                                      tools=TOOLS) == 800
    assert captured[0]['system'] == [{'type': 'text', 'text': 'sys'}]
    assert captured[0]['tools'][0]['input_schema'] == TOOLS[0].input_schema
    assert '<internal_context>' in str(captured[0]['messages'])


async def test_anthropic_unavailable_count_differs_from_failed_request(monkeypatch):
    adapter = object.__new__(AnthropicLlmClient)
    monkeypatch.setattr(adapter, '_client', SimpleNamespace(messages=SimpleNamespace()),
                        raising=False)
    assert await adapter.count_tokens(NEAR, model_config=MC) is None

    async def fail(**kwargs):
        raise RuntimeError('private token / endpoint')

    adapter._client.messages.count_tokens = fail
    adapter._count_params = None
    with pytest.raises(LlmError, match='精确计数请求失败'):
        await adapter.count_tokens(NEAR, model_config=MC)


@pytest.mark.parametrize('values', [
    {'max_total_calls': 0}, {'max_calls_per_cohort': True}, {'cache_size': -1},
    {'max_cohorts': 0}, {'timeout_seconds': 0}, {'boundary_ratio': 1.1},
    {'min_interval_seconds': float('nan')}, {'safety_ratio': -1}, {'enabled': 'yes'},
])
def test_invalid_sampling_configuration_is_rejected(values):
    with pytest.raises(ValueError):
        CalibrationConfig(**values)


def test_sampling_environment_is_explicit_and_validated(monkeypatch, tmp_path):
    monkeypatch.setenv('CODEAGENT_TOKEN_CALIBRATION', '0')
    monkeypatch.setenv('CODEAGENT_TOKEN_COUNT_MAX_CALLS', '5')
    monkeypatch.setenv('CODEAGENT_TOKEN_COUNT_TIMEOUT_SECONDS', '3')
    cfg = AppConfig.from_env(tmp_path)
    assert not cfg.calibration.enabled and cfg.calibration.max_total_calls == 5
    assert cfg.calibration.timeout_seconds == 3
