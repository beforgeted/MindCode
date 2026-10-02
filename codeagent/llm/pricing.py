"""Explicit token prices and a soft run-cost threshold; no built-in market prices."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from codeagent.llm.types import Usage

_SCALE = 10**12


def amount(raw: str) -> int:
    if not isinstance(raw, str):
        raise ValueError('USD amounts must be decimal strings')
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('USD amounts must be decimal strings') from exc
    exponent = value.as_tuple().exponent
    if (not value.is_finite() or value < 0 or value > 1_000_000
            or not isinstance(exponent, int) or exponent < -6):
        raise ValueError('USD amounts require 0..1000000 with at most six decimal places')
    return int(value * _SCALE)


def usd(pico: int) -> str:
    # Integer formatting does not round when very large run totals exceed Decimal precision.
    whole, fraction = divmod(pico, _SCALE)
    return str(whole) + ('.' + f'{fraction:012d}'.rstrip('0') if fraction else '')


@dataclass(frozen=True)
class ModelPrice:
    input: str
    output: str
    cache_read: str | None = None
    cache_write: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    tools: bool | None = None

    def __post_init__(self):
        for value in (self.input, self.output, self.cache_read, self.cache_write):
            if value is not None:
                amount(value)
        for value in (self.context_window, self.max_output_tokens):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError('model limits must be positive integers')
        if self.tools is not None and type(self.tools) is not bool:
            raise ValueError('tools capability must be boolean')

    def charge(self, usage: Usage) -> int | None:
        total = 0
        for tokens, rate in ((usage.input_tokens, self.input),
                             (usage.output_tokens, self.output),
                             (usage.cache_read_tokens, self.cache_read),
                             (usage.cache_write_tokens, self.cache_write)):
            if type(tokens) is not int or not 0 <= tokens <= 10**9:
                return None
            if tokens:
                if rate is None:
                    return None
                # Rates have <=6 decimal places, so per-token picodollars are integral.
                total += tokens * (amount(rate) // 1_000_000)
        return total


@dataclass(frozen=True)
class CostConfig:
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    worker_threshold_usd: str | None = None
    economy_worker: str | None = None

    def __post_init__(self):
        for name, price in self.prices.items():
            if (':' not in name or any(c.isspace() for c in name)
                    or not all(name.split(':', 1)) or not isinstance(price, ModelPrice)):
                raise ValueError('prices require explicit provider:model keys')
        if (self.worker_threshold_usd is None) != (self.economy_worker is None):
            raise ValueError('worker threshold and economy model must be configured together')
        if self.worker_threshold_usd is not None:
            if amount(self.worker_threshold_usd) <= 0:
                raise ValueError('worker threshold must be positive')
            if self.economy_worker not in self.prices:
                raise ValueError('economy worker requires an explicit price entry')
            price = self.prices[self.economy_worker]
            if None in (price.context_window, price.max_output_tokens, price.tools):
                raise ValueError('economy worker requires context/output/tools capabilities')

    @classmethod
    def from_env(cls) -> CostConfig:
        filename = os.environ.get('CODEAGENT_MODEL_PRICES')
        prices = {}
        if filename:
            path = Path(filename)
            with path.open('rb') as stream:
                raw = stream.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError('price configuration exceeds 1MiB')
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('duplicate price configuration key')
                    value[key] = item
                return value
            data = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(data, dict) or set(data) != {'version', 'currency', 'models'}:
                raise ValueError('price file requires version/currency/models')
            if data['version'] != 1 or data['currency'] != 'USD':
                raise ValueError('only version 1 USD pricing is supported')
            if (not isinstance(data['models'], dict) or len(data['models']) > 256
                    or any(not isinstance(entry, dict) for entry in data['models'].values())):
                raise ValueError('models must contain at most 256 price objects')
            prices = {name: ModelPrice(**entry) for name, entry in data['models'].items()}
        return cls(prices, os.environ.get('CODEAGENT_WORKER_COST_THRESHOLD_USD') or None,
                   os.environ.get('CODEAGENT_MODEL_ECONOMY_WORKER') or None)

