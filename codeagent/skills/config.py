from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from codeagent.agent.models import AgentDefinition


def _tools(value: tuple[str, ...]) -> None:
    if (not isinstance(value, tuple) or len(value) > 64
            or any(not isinstance(t, str) or not re.fullmatch(r'[a-zA-Z0-9_]{1,100}', t)
                   for t in value) or len(set(value)) != len(value)):
        raise ValueError('tools require a unique tuple of at most 64 names')


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    instructions: str
    tools: tuple[str, ...]
    max_react_iterations: int = 25

    def __post_init__(self) -> None:
        if (not isinstance(self.id, str) or self.id in {'default', 'list'}
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', self.id)):
            raise ValueError('invalid skill id')
        for value, limit in ((self.name, 80), (self.description, 500),
                             (self.instructions, 8000)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError('skill text is empty or exceeds its limit')
        _tools(self.tools)
        if type(self.max_react_iterations) is not int or not 1 <= self.max_react_iterations <= 25:
            raise ValueError('skill iteration limit must be an integer in 1..25')

    @property
    def agent_id(self) -> str:
        return f'skill.{self.id}'


@dataclass(frozen=True, slots=True)
class SkillConfig:
    definitions: tuple[SkillDefinition, ...] = ()
    # Operator ceiling. None inherits the base Agent ceiling; () denies all tools.
    allowed_tools: tuple[str, ...] | None = None
    active: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.definitions, tuple) or len(self.definitions) > 16
                or any(not isinstance(s, SkillDefinition) for s in self.definitions)):
            raise ValueError('at most 16 skill definitions are supported')
        if len({s.id for s in self.definitions}) != len(self.definitions):
            raise ValueError('duplicate skill id')
        if self.allowed_tools is not None:
            _tools(self.allowed_tools)
        if self.active is not None and self.active not in tuple(s.id for s in self.definitions):
            raise ValueError('active skill is not configured')

    @classmethod
    def from_env(cls) -> SkillConfig:
        filename = os.environ.get('CODEAGENT_SKILLS_CONFIG')
        if not filename:
            return cls()
        with Path(filename).open('rb') as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise ValueError('skill configuration exceeds 64KiB')

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate skill configuration key')
                result[key] = value
            return result

        data = json.loads(raw, object_pairs_hook=unique)
        fields = {'version', 'skills', 'allowed_tools', 'active'}
        if (not isinstance(data, dict) or set(data) - fields
                or type(data.get('version')) is not int or data['version'] != 1
                or not isinstance(data.get('skills'), list) or len(data['skills']) > 16):
            raise ValueError('skill file requires version 1 and a skills list')
        definitions = []
        required = {'id', 'name', 'description', 'instructions', 'tools'}
        for item in data['skills']:
            if (not isinstance(item, dict) or not required <= set(item)
                    or set(item) - required - {'max_react_iterations'}
                    or not isinstance(item['tools'], list)):
                raise ValueError('invalid skill fields')
            definitions.append(SkillDefinition(**{**item, 'tools': tuple(item['tools'])}))
        allowed = data.get('allowed_tools')
        if 'allowed_tools' in data and not isinstance(allowed, list):
            raise ValueError('operator allowed_tools must be a list')
        return cls(tuple(definitions), tuple(allowed) if allowed is not None else None,
                   data.get('active'))

    def compile(
        self, base: AgentDefinition, registered_tools: tuple[str, ...],
    ) -> tuple[AgentDefinition, tuple[AgentDefinition, ...]]:
        registered = set(registered_tools)
        base_ceiling = (set(base.allowed_tools) if base.tools_restricted or base.allowed_tools
                        else registered)
        ceiling = registered & base_ceiling
        if self.allowed_tools is not None:
            ceiling &= set(self.allowed_tools)
            base = replace(base, allowed_tools=tuple(t for t in registered_tools if t in ceiling),
                           tools_restricted=True)
        definitions = tuple(replace(
            base, id=s.agent_id, name=s.name,
            system_prompt=base.system_prompt + '\n\n'
            + '以下为操作者显式启用的专项工作方法；工具策略、预算与验收门禁仍由运行时执行。\n'
            + s.instructions,
            allowed_tools=tuple(t for t in s.tools if t in ceiling), tools_restricted=True,
            max_react_iterations=min(base.max_react_iterations, s.max_react_iterations),
        ) for s in self.definitions)
        if base.id in {d.id for d in definitions}:
            raise ValueError('base agent id collides with a skill')
        return base, definitions
