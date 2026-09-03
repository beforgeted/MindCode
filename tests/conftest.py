from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codeagent.config import AppConfig
from codeagent.context.profile import ContextProfile
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def profile() -> ContextProfile:
    return replace(
        ContextProfile(),
        context_window=20_000,
        max_tool_result_tokens=300,
        tool_result_hot_turns=1,
        tool_result_warm_turns=2,
        tool_result_warm_max_chars=400,
        tool_result_cold_max_chars=150,
    )


@pytest.fixture
def config(workspace: Path, home: Path, profile: ContextProfile) -> AppConfig:
    return AppConfig(
        workspace_root=workspace,
        home=home,
        model="stub-model",
        max_tool_concurrency=4,
        profile=profile,
        use_stub_llm=True,
    )


@pytest.fixture
def artifact_store(home: Path) -> FileArtifactStore:
    return FileArtifactStore(home)


class EchoTool(BaseTool):
    """返回参数里指定长度的文本，用于构造大输出。"""

    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    @property
    def name(self) -> str:
        return "echo"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, "回显", {"type": "object", "properties": {}})

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        size = int(arguments.get("size", 0))
        text = str(arguments.get("text", "x")) * max(1, size or 1)
        return ToolResult.ok(call, text)


class BoomTool(BaseTool):
    """总是抛异常。用于验证一个工具失败不会破坏整批的 tool 协议。"""

    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    @property
    def name(self) -> str:
        return "boom"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, "总是失败", {"type": "object", "properties": {}})

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom on purpose")


class SlowTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.READ_ONLY

    @property
    def name(self) -> str:
        return "slow"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, "慢", {"type": "object", "properties": {}})

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        import asyncio

        await asyncio.sleep(float(arguments.get("seconds", 5)))
        return ToolResult.ok(ToolCall(ctx.call_id, self.name, arguments), "done")
