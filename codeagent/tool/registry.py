from __future__ import annotations

from collections.abc import Iterable, Sequence

from codeagent.llm.types import ToolSpec
from codeagent.tool.base import Tool


class ToolNotFoundError(KeyError):
    pass


class ToolRegistry:
    """长期共享，不持运行状态。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self, allowed: Sequence[str] | None = None) -> tuple[ToolSpec, ...]:
        names = self._tools if allowed is None else [n for n in allowed if n in self._tools]
        return tuple(self._tools[name].spec for name in names)
