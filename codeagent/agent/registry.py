"""AgentRegistry：按 id 拿静态 AgentDefinition。

长期共享 Definition（无状态能力描述），运行状态在 AgentRun / RunContext。
"""

from __future__ import annotations

from codeagent.agent.models import AgentDefinition


class AgentRegistry:
    def __init__(
        self,
        definitions: list[AgentDefinition] | tuple[AgentDefinition, ...] = (),
        *,
        default: AgentDefinition | None = None,
    ) -> None:
        self._by_id: dict[str, AgentDefinition] = {}
        for definition in definitions:
            self.register(definition)
        self._default = default
        if default is not None and default.id not in self._by_id:
            self._by_id[default.id] = default

    def register(self, definition: AgentDefinition) -> None:
        self._by_id[definition.id] = definition

    def get(self, agent_id: str) -> AgentDefinition:
        definition = self._by_id.get(agent_id)
        if definition is not None:
            return definition
        if self._default is not None:
            return self._default
        raise KeyError(f"未注册的 agent_id: {agent_id}")

    @property
    def default(self) -> AgentDefinition | None:
        return self._default

    def names(self) -> tuple[str, ...]:
        return tuple(self._by_id.keys())
