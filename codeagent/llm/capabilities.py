"""Operator supplied model limits; no vendor defaults or automatic discovery."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from codeagent.llm.types import ModelConfig


@dataclass(frozen=True)
class ModelCapability:
    context_window: int
    max_output_tokens: int
    tools: bool
    images: bool
    temperature: bool

    def __post_init__(self) -> None:
        for value in (self.context_window, self.max_output_tokens):
            if type(value) is not int or not 0 < value <= 100_000_000:
                raise ValueError('model limits require positive integers <= 100000000')
        if self.max_output_tokens >= self.context_window:
            raise ValueError('output limit must be smaller than context window')
        for value in (self.tools, self.images, self.temperature):
            if type(value) is not bool:
                raise ValueError('capabilities require explicit booleans')

    def adapt(self, config: ModelConfig) -> ModelConfig:
        return replace(config, context_window=min(config.context_window, self.context_window),
                       max_output_tokens=min(config.max_output_tokens, self.max_output_tokens),
                       temperature=config.temperature if self.temperature else None)


@dataclass(frozen=True)
class CapabilityConfig:
    models: Mapping[str, ModelCapability] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.models) > 256:
            raise ValueError('at most 256 model capabilities are supported')
        for name, entry in self.models.items():
            if (not isinstance(name, str) or ':' not in name or not all(name.split(':', 1))
                    or any(c.isspace() for c in name) or not isinstance(entry, ModelCapability)):
                raise ValueError('capabilities require explicit provider:model keys')
        object.__setattr__(self, 'models', MappingProxyType(dict(self.models)))

    @classmethod
    def from_env(cls) -> CapabilityConfig:
        filename = os.environ.get('CODEAGENT_MODEL_CAPABILITIES')
        if not filename:
            return cls()
        with Path(filename).open('rb') as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('capability configuration exceeds 1MiB')

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate capability configuration key')
                result[key] = value
            return result

        data = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(data, dict) or set(data) != {'version', 'models'}
                or type(data['version']) is not int or data['version'] != 1
                or not isinstance(data['models'], dict) or not data['models']):
            raise ValueError('capability file requires version 1 and nonempty models')
        if len(data['models']) > 256:
            raise ValueError('at most 256 model capabilities are supported')
        entries = {}
        for name, entry in data['models'].items():
            if not isinstance(entry, dict) or set(entry) != {
                'context_window', 'max_output_tokens', 'tools', 'images', 'temperature',
            }:
                raise ValueError('each model requires all five capability fields')
            entries[name] = ModelCapability(**entry)
        return cls(entries)
