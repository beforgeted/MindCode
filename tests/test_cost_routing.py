"""Token prices, durable cost recovery and conservative Worker-only threshold routing."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from codeagent.config import AppConfig
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import EventType
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import current_trace, trace_scope
from codeagent.llm.client import LlmError, LlmErrorKind
from codeagent.llm.message import Message
from codeagent.llm.pricing import CostConfig, ModelPrice, amount, usd
from codeagent.llm.routing import (
    ModelRoutingConfig,
    RoutingLlmClient,
    StaticModelRouter,
    attach_routing,
)
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec, Usage
from codeagent.observability import JsonTrajectoryExporter, summarize_calls
from codeagent.orchestration.cost_store import CostStore, cost_scope
from codeagent.orchestration.master_session import MasterSession
from tests.test_model_routing import Provider

PRICE = ModelPrice('3', '15', '0.3', '3.75', context_window=20000,
                   max_output_tokens=100, tools=True)
CHEAP_PRICE = replace(PRICE, input='1', output='2', cache_read='0.1', cache_write='1.25')


class BilledProvider(Provider):
    async def chat(self, messages, *, model_config, tools=()):
        response = await super().chat(messages, model_config=model_config, tools=tools)
        return replace(response, usage_complete=True)


def config(price=CHEAP_PRICE, threshold='0.000001'):
    return CostConfig({'a:main': PRICE, 'a:cheap': price}, threshold, 'a:cheap')


def compose(tmp_path, costs=None, provider=None, routing=None):
    events = JsonlEventStore(tmp_path)
    provider = provider or BilledProvider()
    raw = RoutingLlmClient({'a': provider}, StaticModelRouter(
        routing or ModelRoutingConfig(worker='a:main')), default_provider='a')
    store = CostStore(tmp_path / 'runs.db')
    client = attach_routing(raw, ModelRoutingConfig(), metrics=Metrics(), events=events,
                            session_id='session', costs=costs or config(), cost_store=store)
    return client, provider, events, store


@pytest.mark.parametrize('rates,expected', [
    (Usage(1000, 2000, 3000, 4000), '0.0489'),
    (Usage(0, 0), '0'), (Usage(1, 0), '0.000003'),
])
def test_separate_cache_charges_are_exact(rates, expected):
    charge = PRICE.charge(rates)
    assert charge is not None and usd(charge) == expected


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '1e-7', '1000001', 3, True])
def test_invalid_prices_never_silently_become_zero(value):
    with pytest.raises(ValueError):
        ModelPrice(value, '1')


@pytest.mark.parametrize('usage', [Usage(-1, 2), Usage(1.5, 2), Usage(True, 2),  # type: ignore
                                   Usage(10**9 + 1, 2)])
def test_invalid_usage_is_unknown(usage):
    assert PRICE.charge(usage) is None


def test_missing_cache_price_only_blocks_used_category():
    price = ModelPrice('3', '15')
    assert price.charge(Usage(1, 1)) is not None
    assert price.charge(Usage(1, 1, 1)) is None
    assert price.charge(Usage(1, 1, 0, 1)) is None


@pytest.mark.parametrize('case', ['no_price', 'no_capability', 'zero_threshold', 'one_field'])
def test_bad_budget_configuration_fails_early(case):
    with pytest.raises(ValueError):
        if case == 'no_price':
            CostConfig({}, '1', 'a:cheap')
        elif case == 'no_capability':
            CostConfig({'a:cheap': ModelPrice('1', '1')}, '1', 'a:cheap')
        elif case == 'zero_threshold':
            config(threshold='0')
        else:
            CostConfig(worker_threshold_usd='1')


def test_env_price_file_is_explicit_and_duplicates_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'prices.json'
    path.write_text(json.dumps({'version': 1, 'currency': 'USD', 'models': {
        'a:cheap': {'input': '1', 'output': '2', 'context_window': 20000,
                    'max_output_tokens': 100, 'tools': True}}}))
    monkeypatch.setenv('CODEAGENT_MODEL_PRICES', str(path))
    monkeypatch.setenv('CODEAGENT_WORKER_COST_THRESHOLD_USD', '0.1')
    monkeypatch.setenv('CODEAGENT_MODEL_ECONOMY_WORKER', 'a:cheap')
    assert CostConfig.from_env().worker_threshold_usd == '0.1'
    path.write_text('{"version":1,"version":1,"currency":"USD","models":{}}')
    with pytest.raises(ValueError, match='duplicate'):
        CostConfig.from_env()


async def test_threshold_is_run_scoped_and_verifiers_keep_primary(tmp_path):
    client, provider, events, store = compose(tmp_path)
    try:
        with cost_scope('one'), trace_scope(role='worker', master_run_id='one'):
            await client.chat([], model_config=ModelConfig(max_output_tokens=70))
            await client.chat([], model_config=ModelConfig(max_output_tokens=70))
            with trace_scope(role='global_verifier'):
                await client.chat([], model_config=ModelConfig(model='main'))
        with cost_scope('two'), trace_scope(role='worker', master_run_id='two'):
            await client.chat([], model_config=ModelConfig())
        assert [call[0].model for call in provider.calls] == ['main', 'cheap', 'main', 'main']
        assert provider.calls[1][0].max_output_tokens == 70
        total, unknown = await store.total('one')
        assert total > 0 and unknown == 0
        routes = await events.query('session', types=[EventType.MODEL_BUDGET_ROUTE])
        assert len(routes) == 1 and routes[0].payload['reason'] == 'cost_threshold'
    finally:
        await events.aclose()


async def test_resume_uses_sql_even_when_events_are_missing(tmp_path):
    client, _, events, store = compose(tmp_path)
    with cost_scope('run'), trace_scope(role='worker'):
        await client.chat([], model_config=ModelConfig())
    await events.aclose()
    fresh, fresh_provider, fresh_events, fresh_store = compose(tmp_path)
    try:
        with cost_scope('run'), trace_scope(role='worker'):
            await fresh.chat([], model_config=ModelConfig())
        assert fresh_provider.calls[0][0].model == 'cheap'
        assert (await fresh_store.total('run'))[1] == 0
        assert (await store.total('run'))[0] > 0
    finally:
        await fresh_events.aclose()


@pytest.mark.parametrize('damage', ['pending', 'missing_price', 'incomplete_usage'])
async def test_unknown_cost_conservatively_selects_economy(tmp_path, damage):
    provider = Provider() if damage == 'incomplete_usage' else BilledProvider()
    costs = config()
    if damage == 'missing_price':
        costs = replace(costs, prices={'a:cheap': PRICE})
    client, provider, events, store = compose(tmp_path, costs, provider)
    try:
        if damage == 'pending':
            await store.begin('crashed-call', 'run', 'a', 'main')
        else:
            with cost_scope('run'), trace_scope(role='planner'):
                await client.chat([], model_config=ModelConfig(model='main'))
        with cost_scope('run'), trace_scope(role='worker'):
            await client.chat([], model_config=ModelConfig())
        assert provider.calls[-1][0].model == 'cheap'
        assert (await store.total('run'))[1] >= 1
    finally:
        await events.aclose()


@pytest.mark.parametrize('reason', ['tools', 'context'])
async def test_incompatible_economy_model_preserves_worker(tmp_path, reason):
    price = replace(PRICE, tools=False) if reason == 'tools' else replace(PRICE, context_window=50)
    client, provider, events, store = compose(tmp_path, config(price))
    try:
        await store.begin('crashed-call', 'run', 'a', 'main')
        tools = [ToolSpec('write', 'write file', {})] if reason == 'tools' else []
        with cost_scope('run'), trace_scope(role='worker'):
            await client.chat([Message.user('task')],
                              model_config=ModelConfig(), tools=tools)
        assert provider.calls[-1][0].model == 'main'
        records = await events.query('session', types=[EventType.MODEL_BUDGET_ROUTE])
        assert records[-1].payload['reason'].startswith('economy_')
    finally:
        await events.aclose()


async def test_parallel_calls_and_inflight_unknown_are_not_lost(tmp_path):
    client, provider, events, store = compose(tmp_path)
    try:
        with cost_scope('run'), trace_scope(role='worker'):
            await asyncio.gather(*(client.chat([], model_config=ModelConfig()) for _ in range(8)))
        total, unknown = await store.total('run')
        charges = [(PRICE if call[0].model == 'main' else CHEAP_PRICE).charge(Usage(10, 2))
                   for call in provider.calls]
        assert all(charge is not None for charge in charges)
        assert unknown == 0 and total == sum(charge for charge in charges if charge is not None)
        assert len(provider.calls) == 8
    finally:
        await events.aclose()


async def test_failed_fallback_has_unknown_cost_and_success_is_charged(tmp_path):
    bad = BilledProvider(LlmError('rate', kind=LlmErrorKind.RATE_LIMIT))
    good = BilledProvider()
    events = JsonlEventStore(tmp_path)
    costs = CostConfig({'a:main': PRICE, 'b:backup': PRICE})
    store = CostStore(tmp_path / 'runs.db')
    raw = RoutingLlmClient({'a': bad, 'b': good}, StaticModelRouter(
        ModelRoutingConfig(worker='a:main', fallback=('b:backup',))))
    client = attach_routing(raw, ModelRoutingConfig(), metrics=Metrics(), events=events,
                            session_id='session', costs=costs, cost_store=store)
    try:
        with cost_scope('run'), trace_scope(role='worker'):
            assert (await client.chat([], model_config=ModelConfig())).content == 'ok'
        total, unknown = await store.total('run')
        assert total == PRICE.charge(Usage(10, 2)) and unknown == 1
    finally:
        await events.aclose()


async def test_sql_intent_failure_prevents_provider_call(tmp_path, monkeypatch):
    client, provider, events, store = compose(tmp_path)
    async def fail(*args):
        raise OSError('database unavailable')
    monkeypatch.setattr(store, 'begin', fail)
    try:
        with cost_scope('run'), trace_scope(role='worker'), pytest.raises(OSError):
            await client.chat([], model_config=ModelConfig())
        assert not provider.calls
    finally:
        await events.aclose()


def test_cost_summary_keeps_unknown_separate_from_zero():
    calls = [{'status': 'success', 'elapsed_ms': 1, 'usage': {},
              'cost_status': 'known', 'cost_pico_usd': str(amount('0.1'))},
             {'status': 'error', 'elapsed_ms': 1, 'usage': None, 'cost_status': 'unknown'}]
    summary = summarize_calls(calls)
    assert summary['cost'] is None and summary['cost_status'] == 'partial'
    assert summary['known_cost_usd'] == '0.1' and summary['unknown_cost_calls'] == 1
    assert Decimal(usd(10**40 + 1)) == Decimal('10000000000000000000000000000.000000000001')


async def test_legacy_resume_does_not_reset_budget_to_zero(tmp_path):
    client, provider, events, store = compose(tmp_path)
    try:
        await store.initialize_run('old-run', resumed=True)
        await store.initialize_run('old-run', resumed=False)
        assert await store.total('old-run') == (0, 1)
        with cost_scope('old-run'), trace_scope(role='worker'):
            await client.chat([], model_config=ModelConfig())
        assert provider.calls[-1][0].model == 'cheap'
    finally:
        await events.aclose()


async def test_master_composition_prices_planner_and_only_degrades_worker(tmp_path):
    class Script(BilledProvider):
        async def chat(self, messages, *, model_config, tools=()):
            role = str(current_trace().get('role'))
            self.calls.append((role, model_config.model))
            content = {
                'planner': '{"steps":[{"id":"one","instruction":"inspect","read_only":true}]}',
                'worker': 'done', 'local_verifier': '{"ok":true}',
                'global_verifier': '{"accept":true}',
            }.get(role, 'done')
            return LlmResponse('id', content, usage=Usage(10, 2), usage_complete=True)
    provider = Script()
    raw = RoutingLlmClient({'a': provider}, StaticModelRouter(ModelRoutingConfig()),
                           default_provider='a')
    settings = AppConfig(workspace_root=tmp_path, home=tmp_path / '.state', model='main',
                         costs=config(), use_stub_llm=False)
    async with MasterSession(settings, llm_client=raw) as session:
        final = await session.run_task('inspect')
        assert final.integrated and session.master is not None
        assert ('planner', 'main') in provider.calls and ('worker', 'cheap') in provider.calls
        assert ('local_verifier', 'main') in provider.calls
        assert ('global_verifier', 'main') in provider.calls
        report = await JsonTrajectoryExporter(
            settings.state_root, session.master._run_store, session.session.event_store,
        ).build(final.master_run_id)
        assert report['durable_cost']['unknown_cost_calls'] == 0
        assert report['durable_cost']['prior_coverage'] is True
        assert report['totals']['cost'] == report['durable_cost']['known_cost_usd']
        assert report['budget_routes']


async def test_cancellation_leaves_unknown_cost_and_no_live_provider(tmp_path):
    entered = asyncio.Event()
    class Blocked(BilledProvider):
        async def chat(self, *args, **kwargs) -> LlmResponse:
            entered.set()
            await asyncio.Future()
            raise AssertionError('blocked provider must be cancelled')
    client, _, events, store = compose(tmp_path, provider=Blocked())
    try:
        with cost_scope('run'), trace_scope(role='worker'):
            call = asyncio.create_task(client.chat([], model_config=ModelConfig()))
        await asyncio.wait_for(entered.wait(), 5)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert await store.total('run') == (0, 1)
    finally:
        await events.aclose()


async def test_budget_event_failure_does_not_change_durable_decision(tmp_path, monkeypatch):
    client, provider, events, store = compose(tmp_path)
    try:
        await store.begin('pending', 'run', 'a', 'main')
        def fail(*args, **kwargs):
            raise OSError('event queue unavailable')
        monkeypatch.setattr(events, 'append_nowait', fail)
        with cost_scope('run'), trace_scope(role='worker'):
            assert (await client.chat([], model_config=ModelConfig())).content == 'ok'
        assert provider.calls[-1][0].model == 'cheap'
        assert (await store.total('run'))[0] == CHEAP_PRICE.charge(Usage(10, 2))
    finally:
        await events.aclose()


async def test_terminal_cost_write_failure_retains_unknown_intent(tmp_path, monkeypatch):
    client, provider, events, store = compose(tmp_path)
    async def fail(*args):
        raise OSError('terminal database write failed')
    monkeypatch.setattr(store, 'finish', fail)
    try:
        with cost_scope('run'), trace_scope(role='worker'), pytest.raises(OSError):
            await client.chat([], model_config=ModelConfig())
        assert len(provider.calls) == 1 and await store.total('run') == (0, 1)
    finally:
        await events.aclose()

