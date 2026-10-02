"""Actual request budgets, ordered Reduce batches and atomic failure preservation."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import cast

import pytest

from codeagent.config import AppConfig
from codeagent.context.compact.chunker import CompactionChunk
from codeagent.context.compact.history_compactor import ConversationHistoryCompactor
from codeagent.context.compact.map_summarizer import HistoryMapSummarizer, _call_json
from codeagent.context.compact.models import TaskCheckpoint, TaskDelta
from codeagent.context.compact.reducer import TaskStateReducer
from codeagent.context.compact.request_budget import CompactionBudgetError
from codeagent.context.history.conversation_history import (
    ConversationHistory,
    validate_tool_protocol,
)
from codeagent.context.history.turn import TurnIdPartitioner
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import current_trace, trace_scope
from codeagent.llm.capabilities import CapabilityConfig, ModelCapability
from codeagent.llm.client import effective_model_config
from codeagent.llm.message import Message, ToolResultBlock, ToolUseBlock
from codeagent.llm.observed_client import RoleLlmClient
from codeagent.llm.routing import ModelRoutingConfig, RoutingLlmClient, StaticModelRouter
from codeagent.llm.types import LlmResponse, ModelConfig
from codeagent.observability import JsonTrajectoryExporter
from codeagent.orchestration.run_store import NullRunStore
from codeagent.session import AgentSession


class WindowProvider:
    def __init__(self, *, failure=None, delay: float = 0, invalid_first_map=False, truncate=False):
        self.requests = []
        self.failure, self.delay = failure, delay
        self.invalid_first_map, self.truncate = invalid_first_map, truncate
        self.active = self.peak = self.count_calls = 0
        self.started = asyncio.Event()

    async def chat(self, messages, *, model_config, tools=()):
        trace = current_trace()
        role = trace.get('role', 'compact_map' if model_config.model == 'map' else 'compact_reduce')
        self.requests.append((tuple(messages), model_config, trace, role))
        self.active += 1
        self.started.set()
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            phase_count = sum(r[3] == role for r in self.requests)
            if self.failure == ('reduce', phase_count) and role == 'compact_reduce':
                raise RuntimeError('injected Reduce failure')
            if role == 'compact_map':
                if self.invalid_first_map and phase_count == 1:
                    return LlmResponse('bad', 'invalid json')
                rows = json.loads(messages[1].text.split('HISTORY_JSON:\n', 1)[1])
                ids = [int(row['text'].split(' ', 1)[0][4:])
                       for row in rows if row['role'] == 'user']
                content = {
                    'constraints': [f'c{i}' for i in ids],
                    'completed_work': [f'work{i}' for i in ids],
                    'files': [{'path': 'shared.py', 'change': 'deleted' if i % 2 else 'modified'}
                              for i in ids],
                    'tests': [{'name': 'suite', 'outcome': 'pass', 'detail': f'run{i}'}
                              for i in ids],
                    'failed_attempts': [{'attempt': f'try{i}', 'why_failed': 'old failure'}
                                        for i in ids],
                    'evidence_refs': [{'type': 'artifact', 'artifact_uri': f'artifact://{i}'}
                                      for i in ids],
                    'open_issues': ['check ' + 'x' * 1_000],
                }
            else:
                payload = json.loads(messages[1].text)
                content = dict(payload['existing_checkpoint'] or {})
                for delta in payload['deltas']:
                    for key, values in delta.items():
                        if isinstance(values, list):
                            merged = [*content.get(key, []), *values]
                            if key in ('files', 'tests'):
                                identity = 'path' if key == 'files' else 'name'
                                merged = list({v[identity]: v for v in merged}.values())
                            else:
                                merged = list({json.dumps(v, sort_keys=True): v
                                               for v in merged}.values())
                            content[key] = merged
                        elif values is not None:
                            content[key] = values
                content.update(version=payload['next_version'],
                               updated_at='2026-10-03T00:00:00+00:00')
                if self.failure == ('version', phase_count):
                    content['version'] += 1
            return LlmResponse('fixed', json.dumps(content),
                               stop_reason='max_tokens' if self.truncate else 'end_turn')
        finally:
            self.active -= 1

    async def count_tokens(self, messages, *, model_config, tools=()):
        self.count_calls += 1
        return None


def compose(provider=None, *, map_window=2_400, reduce_window=2_200, output=600):
    provider = provider or WindowProvider()
    catalog = CapabilityConfig({
        'a:map': ModelCapability(map_window, output, True, False, True),
        'b:reduce': ModelCapability(reduce_window, output, True, False, True),
    })
    client = RoutingLlmClient({'a': provider, 'b': provider}, StaticModelRouter(ModelRoutingConfig(
        compact_map='a:map', compact_reduce='b:reduce')),
        default_provider='a', capabilities=catalog)
    metrics = Metrics()
    compactor = ConversationHistoryCompactor(client, HeuristicTokenEstimator(),
                                             ModelConfig(model='unused', map_model='unused-map'),
                                             metrics=metrics)
    return compactor, provider, client, metrics


def history(count=8, *, huge=None, status=None):
    result = ConversationHistory(session_id='s', agent_run_id='r')
    result.append(Message.system('Keep the original System instruction.'))
    for i in range(count):
        tid = result.begin_turn()
        text = 'x' * (12_000 if i == huge else 500)
        result.append(Message.user(f'turn{i} {text}', turn_id=tid))
        result.append(Message.assistant([ToolUseBlock(f't{i}', 'read', {'path': 'a.py'})],
                                        turn_id=tid))
        result.append(Message.tool([ToolResultBlock(f't{i}', 'y' * 800)], turn_id=tid))
        result.end_turn('failed' if i == status else 'success')
    return result


PROFILE = replace(ContextProfile(), context_window=20_000, retain_recent_turns=0)


async def compact(compactor, source, profile=PROFILE):
    return await compactor.compact(source.messages, profile=profile,
                                  checkpoint=source.checkpoint, turn_statuses=source.turn_statuses)


async def test_map_uses_serialized_role_window_and_reduce_batches_preserve_state():
    compactor, provider, _, metrics = compose()
    source = history()
    original = source.messages
    result = await compact(compactor, source)
    assert result.compacted and result.map_chunks > 1
    maps = [r for r in provider.requests if r[3] == 'compact_map']
    reduces = [r for r in provider.requests if r[3] == 'compact_reduce']
    assert len(reduces) > 1
    assert metrics.counters['context.compaction.reduce_batches'] == len(reduces)
    # Each unsupported provider/model cohort is probed once at the window boundary.
    assert provider.count_calls == 2 and source.messages == original and source.checkpoint is None
    assert len({r[2]['compaction_id'] for r in provider.requests}) == 1
    estimator = HeuristicTokenEstimator()
    for messages, config, trace, role in provider.requests:
        window = 2_400 if role == 'compact_map' else 2_200
        assert estimator.estimate(messages) + config.max_output_tokens < window
        assert config.max_output_tokens == 600
        assert trace['compaction_phase'] in ('map', 'reduce')
    consumed = []
    for messages, _, _, _ in maps:
        rows = json.loads(messages[1].text.split('HISTORY_JSON:\n', 1)[1])
        for row in rows:
            for use in row['tool_uses']:
                assert any(r['tool_use_id'] == use['id'] for row2 in rows
                           for r in row2['tool_results'])
        consumed.extend(row['text'].split(' ', 1)[0] for row in rows if row['role'] == 'user')
    assert consumed == [f'turn{i}' for i in range(8)]
    assert result.checkpoint is not None and result.checkpoint.version == 1
    assert result.checkpoint.constraints == tuple(f'c{i}' for i in range(8))
    assert result.checkpoint.files[0].change.value == 'deleted'
    assert result.checkpoint.tests[0].detail == 'run7'
    assert len(result.checkpoint.failed_attempts) == len(result.checkpoint.evidence_refs) == 8
    assert {r[2]['checkpoint_version'] for r in reduces} == {1}
    validate_tool_protocol(result.messages)
    source.apply_compaction(result.messages, result.checkpoint)
    assert source.checkpoint is not None
    assert source.compaction_count == 1 and source.checkpoint.version == 1


async def test_large_atomic_turn_is_preserved_while_other_chunks_compact():
    compactor, provider, _, _ = compose()
    source = history(8, huge=3, status=5)
    original = source.messages
    huge_id = original[1 + 3 * 3].turn_id
    failed_id = original[1 + 3 * 5].turn_id
    result = await compact(compactor, source)
    assert result.compacted and result.map_failures == 1
    expected_kept = [m for m in original if m.turn_id in (huge_id, failed_id)]
    assert list(result.messages[2:]) == expected_kept
    assert result.messages[0] is original[0]
    assert not any('turn3 ' in r[0][1].text for r in provider.requests if r[3] == 'compact_map')
    validate_tool_protocol(result.messages)


@pytest.mark.parametrize('failure', [('reduce', 2), ('version', 2)])
async def test_later_reduce_failure_discards_all_intermediate_checkpoints(failure):
    compactor, provider, _, _ = compose(WindowProvider(failure=failure))
    source = history()
    old = TaskCheckpoint(version=7, constraints=('old constraint',))
    source.checkpoint = old
    original = source.messages
    result = await compact(compactor, source)
    assert not result.compacted and result.messages == original
    assert source.checkpoint is old and old.version == 7 and source.compaction_count == 0
    reduces = [r for r in provider.requests if r[3] == 'compact_reduce']
    assert len(reduces) == 2 and {r[2]['checkpoint_version'] for r in reduces} == {8}
    assert result.map_chunks > 0 and 'Reduce' in result.reason


async def test_reduce_batch_budget_stops_before_the_next_provider_call():
    compactor, provider, _, metrics = compose()
    source = history()
    result = await compact(compactor, source, replace(PROFILE, compaction_reduce_max_batches=1))
    assert not result.compacted and result.messages == source.messages and '预算' in result.reason
    assert sum(r[3] == 'compact_reduce' for r in provider.requests) == 1
    assert metrics.counters['context.compaction.reduce_batches'] == 1


async def test_emergency_reduce_shares_the_original_batch_budget():
    compactor, provider, _, _ = compose(reduce_window=20_000)
    source = history()
    profile = replace(PROFILE, target_ratio=0.001, checkpoint_max_tokens=1,
                      compaction_reduce_max_batches=1)
    result = await compact(compactor, source, profile)
    assert not result.compacted and '预算' in result.reason and result.messages == source.messages
    assert sum(r[3] == 'compact_reduce' for r in provider.requests) == 1


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
async def test_invalid_batch_budget_fails_before_any_provider_call(limit):
    compactor, provider, _, _ = compose()
    result = await compact(compactor, history(),
                           replace(PROFILE, compaction_reduce_max_batches=limit))
    assert not result.compacted and not provider.requests


@pytest.mark.parametrize('part', ['checkpoint', 'delta', 'focus'])
async def test_single_reduce_item_or_fixed_prompt_too_large_has_no_provider_call(part):
    _, provider, client, _ = compose()
    reducer = TaskStateReducer(RoleLlmClient(client, 'compact_reduce'), ModelConfig(model='unused'))
    checkpoint = TaskCheckpoint(constraints=('中' * 10_000,)) if part == 'checkpoint' else None
    delta = TaskDelta(constraints=('中' * 10_000,)) if part == 'delta' else TaskDelta()
    focus = '中' * 10_000 if part == 'focus' else None
    with pytest.raises(CompactionBudgetError):
        await reducer.reduce(checkpoint, (delta,), max_output_tokens=600, focus=focus)
    assert not provider.requests


@pytest.mark.parametrize('old', [None, TaskCheckpoint(version=9)])
async def test_reduce_with_no_deltas_still_advances_one_public_version(old):
    _, provider, client, _ = compose()
    reducer = TaskStateReducer(RoleLlmClient(client, 'compact_reduce'), ModelConfig())
    result = await reducer.reduce(old, (), max_output_tokens=600, focus=None)
    assert result.version == (10 if old else 1) and len(provider.requests) == 1


async def test_json_serialization_overhead_can_reject_a_raw_fitting_turn():
    source = ConversationHistory(session_id='s', agent_run_id='r')
    tid = source.begin_turn()
    source.append(Message.user('turn0 ' + '\\"' * 800, turn_id=tid))
    source.end_turn()
    estimator = HeuristicTokenEstimator()
    raw = estimator.estimate(source.messages)
    _, provider, _, _ = compose()
    compactor = ConversationHistoryCompactor(provider, estimator, ModelConfig(
        model='reduce', map_model='map', context_window=raw + 400))
    profile = replace(PROFILE, map_max_output_tokens=200)
    result = await compact(compactor, source, profile)
    assert not result.compacted and result.messages == source.messages and not provider.requests


async def test_map_invalid_json_repair_remains_within_the_reserved_window():
    compactor, provider, _, _ = compose(WindowProvider(invalid_first_map=True))
    result = await compact(compactor, history())
    assert result.compacted
    repaired = [r for r in provider.requests if len(r[0]) == 3]
    assert len(repaired) == 1 and repaired[0][3] == 'compact_map'
    assert HeuristicTokenEstimator().estimate(repaired[0][0]) + 600 < 2_400


async def test_unplanned_json_repair_is_checked_before_a_second_call():
    class InvalidProvider(WindowProvider):
        async def chat(self, messages, *, model_config, tools=()):
            self.requests.append(tuple(messages))
            return LlmResponse('bad', 'invalid json')

    provider = InvalidProvider()
    messages = (Message.user('hello'),)
    config = ModelConfig(context_window=HeuristicTokenEstimator().estimate(messages) + 11,
                         max_output_tokens=10)
    with pytest.raises(CompactionBudgetError):
        await _call_json(provider, messages, config, TaskDelta.from_json)
    assert len(provider.requests) == 1


async def test_truncated_map_outputs_never_replace_history():
    compactor, provider, _, _ = compose(WindowProvider(truncate=True))
    source = history()
    result = await compact(compactor, source)
    assert not result.compacted and result.messages == source.messages
    assert result.map_failures == result.map_chunks
    assert not any(r[3] == 'compact_reduce' for r in provider.requests)


async def test_large_focus_cannot_be_fixed_by_dropping_history():
    compactor, provider, _, _ = compose()
    source = history()
    result = await compactor.compact(source.messages, profile=PROFILE, focus='中' * 10_000,
                                     turn_statuses=source.turn_statuses)
    assert not result.compacted and result.messages == source.messages and not provider.requests


async def test_timeout_drains_all_parallel_map_requests_and_preserves_history():
    compactor, provider, _, _ = compose(WindowProvider(delay=0.2))
    source = history()
    profile = replace(PROFILE, compaction_timeout_seconds=0.02, compaction_map_concurrency=2)
    result = await compact(compactor, source, profile)
    assert not result.compacted and result.messages == source.messages and '超时' in result.reason
    assert provider.active == 0 and provider.peak == 2


async def test_external_cancellation_propagates_and_drains_parallel_maps():
    compactor, provider, _, _ = compose(WindowProvider(delay=0.2))
    source = history()
    task = asyncio.create_task(compact(compactor, source))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.active == 0 and source.checkpoint is None and source.compaction_count == 0


def test_effective_lookup_is_synchronous_role_specific_and_preserves_provider():
    _, provider, client, _ = compose()
    base = ModelConfig(model='unused', map_model='unused-map', max_output_tokens=5_000)
    wrapped = RoleLlmClient(client, 'compact_reduce')
    with trace_scope(role='worker'):
        config = effective_model_config(wrapped, base)
        assert current_trace()['role'] == 'worker'
    assert config.model == 'b:reduce' and config.context_window == 2_200
    assert config.max_output_tokens == 600 and not provider.requests and provider.count_calls == 0


def test_invalid_optional_capability_hook_is_rejected():
    class InvalidHook(WindowProvider):
        def effective_config(self, config):
            return None

    with pytest.raises(TypeError):
        effective_model_config(InvalidHook(), ModelConfig())


def test_reduce_budget_environment_setting(monkeypatch, tmp_path):
    monkeypatch.setenv('CODEAGENT_COMPACTION_REDUCE_MAX_BATCHES', '7')
    assert AppConfig.from_env(tmp_path).profile.compaction_reduce_max_batches == 7


def test_map_fits_budget_includes_the_actual_focus_and_repair_prompt():
    _, _, client, _ = compose()
    mapper = HistoryMapSummarizer(RoleLlmClient(client, 'compact_map'), ModelConfig())
    source = history(1)
    turns = TurnIdPartitioner().partition(source.messages, statuses=source.turn_statuses)
    chunk = CompactionChunk(tuple(turns), tuple(turns[0].messages), 400)
    assert mapper.fits(chunk, focus=None, max_output_tokens=600)
    assert not mapper.fits(chunk, focus='中' * 3_000, max_output_tokens=600)


async def test_checkpoint_growth_can_stop_later_batch_without_publishing_partial_state():
    class GrowingProvider(WindowProvider):
        async def chat(self, messages, *, model_config, tools=()):
            response = await super().chat(messages, model_config=model_config, tools=tools)
            if current_trace().get('role') == 'compact_reduce':
                payload = json.loads(response.content)
                payload['constraints'].append('中' * 10_000)
                return replace(response, content=json.dumps(payload))
            return response

    compactor, provider, _, _ = compose(GrowingProvider())
    source = history()
    result = await compact(compactor, source)
    assert not result.compacted and result.messages == source.messages and source.checkpoint is None
    assert sum(r[3] == 'compact_reduce' for r in provider.requests) == 1


async def test_prior_checkpoint_advances_only_one_version_after_multiple_batches():
    compactor, provider, _, _ = compose()
    source = history()
    old = TaskCheckpoint(version=7, constraints=('old constraint',))
    source.checkpoint = old
    result = await compact(compactor, source)
    assert result.compacted and result.checkpoint is not None and result.checkpoint.version == 8
    assert result.checkpoint.constraints[0] == 'old constraint'
    assert sum(r[3] == 'compact_reduce' for r in provider.requests) > 1
    assert source.checkpoint is old and old.version == 7


async def test_reduce_truncation_after_first_successful_batch_rolls_back_history():
    class TruncatedReduce(WindowProvider):
        async def chat(self, messages, *, model_config, tools=()):
            response = await super().chat(messages, model_config=model_config, tools=tools)
            reduce_count = sum(r[3] == 'compact_reduce' for r in self.requests)
            if current_trace().get('role') == 'compact_reduce' and reduce_count >= 2:
                return replace(response, stop_reason='max_tokens')
            return response

    compactor, provider, _, _ = compose(TruncatedReduce())
    source = history()
    result = await compact(compactor, source)
    assert not result.compacted and result.messages == source.messages
    assert sum(r[3] == 'compact_reduce' for r in provider.requests) == 3


async def test_reverse_map_completion_still_reduces_in_history_order():
    class ReverseMap(WindowProvider):
        async def chat(self, messages, *, model_config, tools=()):
            index = cast(int, current_trace().get('compaction_chunk', 0))
            if current_trace().get('role') == 'compact_map':
                await asyncio.sleep((4 - min(index, 3)) * 0.002)
            return await super().chat(messages, model_config=model_config, tools=tools)

    compactor, _, _, _ = compose(ReverseMap())
    result = await compact(compactor, history())
    assert result.compacted and result.checkpoint is not None
    assert result.checkpoint.constraints == tuple(f'c{i}' for i in range(8))


async def test_session_composition_exports_map_reduce_trace_and_preserves_scope(tmp_path):
    _, provider, raw, _ = compose()
    raw.capabilities = CapabilityConfig({
        **raw.capabilities.models,
        'a:worker': ModelCapability(200_000, 8_192, True, False, True),
    })
    config = AppConfig(tmp_path, tmp_path / '.state', model='a:worker', profile=PROFILE)
    source = history()
    async with AgentSession(config, llm_client=raw) as session:
        with trace_scope(master_run_id='m', session_id=session.session_id, role='worker'):
            result = await compact(session.context_manager.compactor, source)
            assert current_trace()['role'] == 'worker'
        exporter = JsonTrajectoryExporter(tmp_path, NullRunStore(), session.event_store)
        report = await exporter.build('m')
        assert result.compacted
        calls = report['llm_calls']
        assert len(calls) == len(provider.requests)
        assert {c['role'] for c in calls} == {'compact_map', 'compact_reduce'}
        assert len({c['trace']['compaction_id'] for c in calls}) == 1
        assert {c['provider'] for c in calls} == {'a', 'b'}
        assert all(c['trace']['master_run_id'] == 'm' for c in calls)
