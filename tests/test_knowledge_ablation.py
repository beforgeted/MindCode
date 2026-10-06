"""Billing guard and oracle separation checks; no real model request."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from codeagent.llm.types import Usage
from scenarios.knowledge_ablation import (
    FlashTrialClient,
    TrialBudget,
    TrialStopped,
    changed_paths,
    remaining_authorization,
    selected_tasks,
    snapshot,
)
from scenarios.knowledge_tasks import tasks


def budget(tmp_path, cap='6'):
    return TrialBudget(tmp_path, Decimal('6'), cap=Decimal(cap))


def test_reservation_persisted_before_dispatch_and_settled(tmp_path):
    guard = budget(tmp_path)
    assert guard.reserve(1000, Decimal('6')) == 0
    state = json.loads((tmp_path / 'budget.json').read_text())
    assert state['entries'][0]['status'] == 'dispatched'
    assert Decimal(state['reserved_cny']) > 0
    guard.settle(Usage(input_tokens=1000, output_tokens=100), True)
    assert guard.charged == Decimal('.0028') and not guard.reserved


def test_pending_unknown_cannot_admit_or_reset(tmp_path):
    guard = budget(tmp_path)
    guard.reserve(1000, Decimal('6'))
    with pytest.raises(TrialStopped):
        guard.reserve(1000, Decimal('6'))
    guard.unknown('timeout')
    with pytest.raises(TrialStopped):
        guard.reserve(1000, Decimal('6'))
    assert guard.reserved > 0


@pytest.mark.parametrize('cap', ['0', '-1', '6.01', 'NaN', 'Infinity'])
def test_budget_cannot_expand_user_authorization(tmp_path, cap):
    with pytest.raises(ValueError):
        budget(tmp_path, cap)


def test_balance_debit_external_activity_stops_admission(tmp_path):
    guard = budget(tmp_path)
    with pytest.raises(TrialStopped):
        guard.reserve(1000, Decimal('.01'))
    assert guard.entries == []


@pytest.mark.parametrize('usage,complete', [
    (Usage(input_tokens=100), False),
    (Usage(input_tokens=-1), True),
    (Usage(cache_write_tokens=1), True),
    (Usage(output_tokens=2049), True),
])
def test_unknown_or_invalid_usage_retains_reservation(tmp_path, usage, complete):
    guard = budget(tmp_path)
    guard.reserve(1000, Decimal('6'))
    with pytest.raises(TrialStopped):
        guard.settle(usage, complete)
    assert guard.stopped and guard.reserved > 0


def test_estimate_overrun_stops_future_requests(tmp_path):
    guard = budget(tmp_path)
    guard.reserve(1000, Decimal('6'))
    with pytest.raises(TrialStopped):
        guard.settle(Usage(input_tokens=100000), True)
    assert guard.stopped == 'admission_estimate_exceeded'


def test_no_oracle_reference_or_credentials_in_candidate():
    cases = tasks()
    assert [t.level for t in cases] == ['easy', 'easy', 'medium', 'medium', 'hard', 'hard']
    for task in cases:
        before = snapshot(task.files)
        after = snapshot({**task.files, **task.reference})
        assert set(changed_paths(before, after)) <= set(task.editable)
        assert task.oracle not in '\n'.join(task.files.values())
        assert not any(p.startswith('.') or 'oracle' in p for p in task.files)
        assert all(task.files[p] != v for p, v in task.reference.items())


@pytest.mark.parametrize('reason', ['calls', 'time', 'request'])
def test_attempt_time_request_limits_before_dispatch(tmp_path, reason):
    guard = budget(tmp_path)
    if reason == 'calls':
        guard.max_calls = 0
    elif reason == 'time':
        guard.started -= guard.seconds + 1
    with pytest.raises(TrialStopped):
        guard.reserve(65537 if reason == 'request' else 1000, Decimal('6'))
    assert not guard.entries


async def test_sdk_constructor_uses_native_http_backend_without_request(tmp_path):
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
        client = FlashTrialClient('offline-non-secret-test', budget(tmp_path), http)
        assert client._client.max_retries == 0
        assert client.balance_queries == 0
        assert client.budget.entries == []
        filtered = client._filter({'model': 'deepseek-flash', 'temperature': 0},
                                  client._create_params)
        if client._create_params and 'temperature' not in client._create_params:
            assert 'temperature' not in filtered
        assert filtered['extra_body'] == {'thinking': {'type': 'disabled'}}


def test_continuation_deducts_old_reservation_and_attempt():
    amount, attempts = remaining_authorization('6', '.028926', 1)
    assert amount == Decimal('5.971074') and attempts == 287


@pytest.mark.parametrize('cap,prior,attempts', [
    ('6.1', '.2', 1), ('6', '-.1', 1), ('6', 'NaN', 1),
    ('6', '6', 1), ('6', '0', -1), ('6', '0', 288),
])
def test_continuation_cannot_expand_whole_authorization(cap, prior, attempts):
    with pytest.raises(ValueError):
        remaining_authorization(cap, prior, attempts)


def test_corrected_quote_contract_and_selected_subset():
    cases = selected_tasks('quote')
    assert len(cases) == 1 and cases[0].name == 'quote'
    assert 'base*(1-discount)' in cases[0].files['docs/current.md']
    assert '1为全部减免' in cases[0].files['docs/current.md']


@pytest.mark.parametrize('names', ['', 'unknown', 'quote,quote'])
def test_unapproved_task_name_rejected(names):
    with pytest.raises(ValueError):
        selected_tasks(names)
