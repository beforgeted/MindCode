"""Capability preflight, fallback safety and preparation budgets with fixed Providers."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from codeagent.config import AppConfig
from codeagent.context.profile import ContextProfile
from codeagent.evidence.event_store import RawEventStore
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import EventType
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import trace_scope
from codeagent.llm.anthropic_client import AnthropicLlmClient
from codeagent.llm.capabilities import CapabilityConfig, ModelCapability
from codeagent.llm.client import LlmError, LlmErrorKind
from codeagent.llm.message import ImageBlock, Message, Role, ToolResultBlock
from codeagent.llm.routing import (
    ModelRole,
    ModelRoutingConfig,
    RoutingLlmClient,
    StaticModelRouter,
    attach_routing,
)
from codeagent.llm.types import ModelConfig, ToolSpec
from codeagent.observability import JsonTrajectoryExporter
from codeagent.orchestration.cost_store import CostStore, cost_scope
from codeagent.orchestration.run_store import NullRunStore
from codeagent.session import AgentSession
from tests.test_cost_routing import config as cost_config
from tests.test_model_routing import Provider

MAIN = ModelCapability(20_000, 100, True, True, True)
SMALL = ModelCapability(4_000, 50, True, False, False)
TOOL = ToolSpec('read', 'read a file', {'type': 'object'})


def compose(capabilities=None, first=None, second=None, **kwargs):
    first, second = first or Provider(), second or Provider()
    client = RoutingLlmClient(
        {'a': first, 'b': second}, StaticModelRouter(ModelRoutingConfig(
            worker='a:main', fallback=('b:small',))), default_provider='a',
        capabilities=capabilities or CapabilityConfig({'a:main': MAIN, 'b:small': SMALL}),
        **kwargs,
    )
    return client, first, second


@pytest.mark.parametrize('changes', [
    {'context_window': True}, {'context_window': 0}, {'context_window': 100_000_001},
    {'max_output_tokens': -1}, {'max_output_tokens': 20_000},
    {'tools': 'true'}, {'images': 1}, {'temperature': None},
])
def test_invalid_capability_metadata_is_rejected(changes):
    with pytest.raises(ValueError):
        ModelCapability(**(asdict(MAIN) | changes))


@pytest.mark.parametrize('case', ['duplicate', 'empty', 'missing', 'extra', 'version',
                                  'unprefixed', 'oversize', 'many'])
def test_catalog_schema_fails_closed(tmp_path, monkeypatch, case):
    data = {'version': 1, 'models': {'a:main': asdict(MAIN)}}
    if case == 'empty':
        data['models'] = {}
    elif case == 'missing':
        del data['models']['a:main']['images']
    elif case == 'extra':
        data['models']['a:main']['unknown'] = True
    elif case == 'version':
        data['version'] = True
    elif case == 'unprefixed':
        data['models'] = {'main': asdict(MAIN)}
    elif case == 'many':
        data['models'] = {f'a:m{i}': asdict(MAIN) for i in range(257)}
    raw = json.dumps(data)
    if case == 'duplicate':
        raw = '{"version":1,"version":1,"models":{}}'
    elif case == 'oversize':
        raw = ' ' * 1_048_577
    path = tmp_path / 'capabilities.json'
    path.write_text(raw)
    monkeypatch.setenv('CODEAGENT_MODEL_CAPABILITIES', str(path))
    with pytest.raises(ValueError):
        AppConfig.from_env(tmp_path)


def test_catalog_is_snapshotted_and_loaded_without_prices(tmp_path, monkeypatch):
    models = {'a:main': MAIN}
    catalog = CapabilityConfig(models)
    models.clear()
    assert catalog.models['a:main'] == MAIN
    path = tmp_path / 'capabilities.json'
    path.write_text(json.dumps({'version': 1, 'models': {'a:main': asdict(MAIN)}}))
    monkeypatch.setenv('CODEAGENT_MODEL_CAPABILITIES', str(path))
    assert AppConfig.from_env(tmp_path).capabilities == catalog


async def test_missing_backup_metadata_blocks_before_primary_call():
    client, first, second = compose(CapabilityConfig({'a:main': MAIN}))
    with trace_scope(role='worker'), pytest.raises(LlmError) as error:
        await client.chat([], model_config=ModelConfig())
    assert error.value.kind == LlmErrorKind.CONFIGURATION
    assert not first.calls and not second.calls


async def test_fallback_receives_its_own_limits_and_omits_temperature():
    client, first, second = compose(first=Provider(LlmError('busy',
                                                         kind=LlmErrorKind.RATE_LIMIT)))
    base = ModelConfig(context_window=10_000, max_output_tokens=900, temperature=0.2)
    with trace_scope(role='worker'):
        await client.chat([Message.user('hello')], model_config=base)
    assert first.calls[0][0].max_output_tokens == 100
    effective = second.calls[0][0]
    assert (effective.context_window, effective.max_output_tokens, effective.temperature) == (
        4_000, 50, None)
    assert base.max_output_tokens == 900 and base.temperature == 0.2


@pytest.mark.parametrize('feature', ['tool_schema', 'tool_history', 'image'])
async def test_incompatible_primary_never_calls_any_model(feature):
    caps = CapabilityConfig({'a:main': replace(MAIN, tools=False, images=False), 'b:small': SMALL})
    client, first, second = compose(caps)
    messages, tools = [], ()
    if feature == 'tool_schema':
        tools = (TOOL,)
    elif feature == 'tool_history':
        messages = [Message.tool([ToolResultBlock('id', 'result')])]
    else:
        messages = [Message(Role.USER, (ImageBlock('image/png', 'payload'),))]
    with trace_scope(role='worker'), pytest.raises(LlmError) as error:
        await client.chat(messages, model_config=ModelConfig(), tools=tools)
    assert error.value.kind == LlmErrorKind.INVALID_REQUEST
    assert not first.calls and not second.calls


@pytest.mark.parametrize('feature', ['image', 'context', 'tools'])
async def test_incompatible_backup_is_skipped_and_recorded(tmp_path, feature):
    events = JsonlEventStore(tmp_path)
    caps = CapabilityConfig({'a:main': MAIN, 'b:small': replace(SMALL, tools=False)})
    client, first, second = compose(caps, first=Provider(
        LlmError('busy', kind=LlmErrorKind.TIMEOUT)), events=events, session_id='s')
    messages, tools = [Message.user('hello')], ()
    if feature == 'image':
        messages = [Message(Role.USER, (ImageBlock('image/png', 'payload'),))]
    elif feature == 'context':
        messages = [Message.user('中' * 5_000)]
    else:
        tools = (TOOL,)
    try:
        with trace_scope(role='worker'), pytest.raises(LlmError) as error:
            await client.chat(messages, model_config=ModelConfig(), tools=tools)
        assert error.value.kind == LlmErrorKind.TIMEOUT
        assert len(first.calls) == 1 and not second.calls
        records = await events.query('s', types=[EventType.MODEL_CAPABILITY_ROUTE])
        assert any(r.payload['reason'] == {
            'image': 'images_unsupported', 'context': 'context_too_small',
            'tools': 'tools_unsupported',
        }[feature] for r in records)
    finally:
        await events.aclose()


async def test_pruned_image_summary_can_reach_text_only_fallback():
    client, _, second = compose(first=Provider(LlmError('busy', kind=LlmErrorKind.UNAVAILABLE)))
    messages = [Message(Role.USER, (ImageBlock('image/png', summary='a cat'),))]
    with trace_scope(role='worker'):
        await client.chat(messages, model_config=ModelConfig())
    assert len(second.calls) == 1


async def test_primary_context_overflow_is_terminal_and_preserves_messages():
    client, first, second = compose()
    messages = [Message.user('中' * 20_000)]
    with trace_scope(role='worker'), pytest.raises(LlmError) as error:
        await client.chat(messages, model_config=ModelConfig())
    assert error.value.kind == LlmErrorKind.CONTEXT_LIMIT
    assert messages[0].text == '中' * 20_000 and not first.calls and not second.calls


async def test_all_roles_and_token_count_use_effective_model_limits():
    provider = Provider()
    client = RoutingLlmClient({'a': provider}, StaticModelRouter(ModelRoutingConfig()),
                             default_provider='a', capabilities=CapabilityConfig({'a:main': SMALL}))
    for role in ModelRole:
        with trace_scope(role=role.value):
            await client.chat([], model_config=ModelConfig(model='main'))
            assert await client.count_tokens([], model_config=ModelConfig(model='main')) == 17
    assert all(c[0].max_output_tokens == 50 and c[0].temperature is None for c in provider.calls)
    assert all(c.context_window == 4_000 for c in provider.counted)


async def test_worker_preparation_uses_actual_message_budget(tmp_path):
    catalog = CapabilityConfig({'anthropic:main': SMALL})
    cfg = AppConfig(tmp_path, tmp_path / '.state', model='main', capabilities=catalog)
    async with AgentSession(cfg, llm_client=Provider()) as session:
        seen = []
        prepare = session.context_manager.prepare

        async def capture(history, profile, **kwargs):
            seen.append(profile)
            return await prepare(history, profile, **kwargs)

        session.context_manager.prepare = capture
        await session.send('hello')
        assert 0 < seen[0].context_window < 4_000 - 50
        assert seen[0].hard_trigger < 4_000 - 50
        assert cfg.profile.context_window == 200_000


async def test_attachment_preserves_catalog_and_event_failure_is_nonfatal(tmp_path):
    class BrokenEvents:
        def append_nowait(self, event):
            raise RuntimeError('disk')

    raw, first, _ = compose()
    metrics = Metrics()
    client = attach_routing(raw, ModelRoutingConfig(), metrics=metrics,
                            events=cast(RawEventStore, BrokenEvents()), session_id='s')
    with trace_scope(role='worker'):
        await client.chat([], model_config=ModelConfig())
    assert first.calls and client.capabilities == raw.capabilities
    assert metrics.counters['observability.event_failures'] >= 1


def test_conflicting_price_capabilities_are_rejected():
    with pytest.raises(ValueError, match='冲突'):
        compose(CapabilityConfig({'a:main': SMALL}), costs=cost_config())


async def test_economy_model_without_image_support_preserves_primary(tmp_path):
    costs = cost_config()
    catalog = CapabilityConfig({'a:main': MAIN, 'a:cheap': replace(MAIN, images=False),
                                'b:small': SMALL})
    store = CostStore(tmp_path / 'runs.db')
    client, first, _ = compose(catalog, costs=costs, cost_store=store)
    await store.begin('old', 'run', 'a', 'main')  # Pending billing triggers conservative routing.
    with cost_scope('run'), trace_scope(role='worker'):
        await client.chat([Message(Role.USER, (ImageBlock('image/png', 'payload'),))],
                          model_config=ModelConfig())
    assert first.calls[0][0].model == 'main'


@pytest.mark.parametrize('temperature', [None, 0.0])
async def test_anthropic_adapter_omits_unsupported_temperature(temperature):
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                               stop_reason='end_turn')

    client = AnthropicLlmClient.__new__(AnthropicLlmClient)
    client._client = cast(Any, SimpleNamespace(messages=SimpleNamespace(create=create)))
    client._create_params = None
    client._metrics = Metrics()
    await client.chat([Message.user('hello')], model_config=ModelConfig(temperature=temperature))
    assert ('temperature' in captured) == (temperature is not None)


def test_context_profile_rejects_no_space_without_mutating_defaults():
    client, _, _ = compose()
    with pytest.raises(LlmError) as error:
        client.context_profile(ModelConfig(), ContextProfile(context_window=50), (TOOL,))
    assert error.value.kind == LlmErrorKind.CONTEXT_LIMIT


async def test_capability_decisions_are_exported_with_the_run(tmp_path):
    events = JsonlEventStore(tmp_path)
    client, _, _ = compose(events=events, session_id='s')
    try:
        with trace_scope(role='worker', master_run_id='run'):
            await client.chat([], model_config=ModelConfig())
        report = await JsonTrajectoryExporter(tmp_path, NullRunStore(), events).build('run')
        decision = report['capability_routes'][0]
        assert decision['reason'] == 'selected' and decision['capabilities']['images'] is True
        assert decision['max_output_tokens'] == 100
    finally:
        await events.aclose()


async def test_smaller_user_limit_is_preserved_when_cost_routing_changes_model(tmp_path):
    catalog = CapabilityConfig({'a:main': MAIN, 'a:cheap': MAIN, 'b:small': SMALL})
    store = CostStore(tmp_path / 'runs.db')
    client, first, _ = compose(catalog, costs=cost_config(), cost_store=store)
    await store.begin('old', 'run', 'a', 'main')
    with cost_scope('run'), trace_scope(role='worker'):
        await client.chat([], model_config=ModelConfig(context_window=1_000, max_output_tokens=20))
    assert first.calls[0][0].model == 'cheap'
    assert first.calls[0][0].context_window == 1_000
    assert first.calls[0][0].max_output_tokens == 20


async def test_skip_small_backup_can_reach_next_explicit_compatible_model():
    first = Provider(LlmError('busy', kind=LlmErrorKind.RATE_LIMIT))
    small, large = Provider(), Provider()
    client = RoutingLlmClient({'a': first, 'b': small, 'c': large}, StaticModelRouter(
        ModelRoutingConfig(worker='a:main', fallback=('b:small', 'c:large'))),
        capabilities=CapabilityConfig({'a:main': MAIN, 'b:small': SMALL, 'c:large': MAIN}))
    with trace_scope(role='worker'):
        await client.chat([Message.user('中' * 5_000)], model_config=ModelConfig())
    assert len(first.calls) == 1 and not small.calls and len(large.calls) == 1
