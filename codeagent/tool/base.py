"""Tool 接口。

Tool 一律无状态、可长期共享、协程安全。运行期信息全部通过显式的
ToolExecutionContext 传入，绝不从隐式全局状态里找"当前 workspace"。

输出有界化的分层：
- **主防线**：可能产生无界输出的工具（run_command）自己流式写 artifact，
  返回有界 content + artifact；
- **兜底**：ToolResultNormalizer 对任何超限结果再补一刀。

只有兜底没有主防线是不行的 —— 300K 的输出在进 Normalizer 之前
就已经把内存吃掉了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.llm.types import ToolSpec
from codeagent.tool.approval import ApprovalPolicy, DenyExternalApprovalPolicy
from codeagent.tool.command_policy import CommandPolicy
from codeagent.tool.deferred import DeferredAction
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.executor import CommandExecutor, LocalExecutor
from codeagent.tool.models import ToolConcurrencyMode, ToolResult
from codeagent.workspace.context import WorkspaceContext


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    agent_run_id: str
    session_id: str
    tool_run_id: str
    # LLM 给出的 tool_use id。工具构造 ToolResult 时必须用它，
    # 而不是 tool_run_id —— 前者是协议 id，后者是我们自己的执行记录 id。
    # 用错会导致 tool_result 对不上 tool_use，直接破坏协议。
    call_id: str
    workspace: WorkspaceContext
    cancellation: CancellationToken
    artifact_store: ArtifactStore
    max_output_bytes: int = 4 * 1024 * 1024
    timeout_seconds: float = 60.0
    # 命令分类策略（run_command 用）。默认内置规则；session 装配时可注入配置化实例。
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)
    # 命令执行器（run_command 用）。默认本机执行器；可换 SandboxExecutor。
    command_executor: CommandExecutor = field(default_factory=LocalExecutor)
    # 推测执行阶段（Worker 在 candidate 内）为 False → 外部副作用一律禁止。
    allow_external_effects: bool = False
    # 放行阶段的审批策略。默认 fail-safe 拒绝。
    approval: ApprovalPolicy = field(default_factory=DenyExternalApprovalPolicy)
    # 推测期被拦下的外部副作用记录到这里（run 级队列），供 promote 后处理。None=不收集。
    deferred: list[DeferredAction] | None = None


@runtime_checkable
class Tool(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def concurrency_mode(self) -> ToolConcurrencyMode: ...

    @property
    def effect_kind(self) -> EffectKind:
        """默认副作用类别。run_command 是动态的（由 CommandPolicy 按命令定），
        以类属性给出的是保守默认，真实判定在工具内部完成。"""
        ...

    @property
    def retry_policy(self) -> RetryPolicy:
        """默认重试语义。同上，动态工具以此为保守默认。"""
        ...

    @property
    def spec(self) -> ToolSpec: ...

    def resource_keys(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        """EXCLUSIVE_RESOURCE 模式下要锁的资源 key。

        例：("file:/repo/src/user.py",)。返回空元组表示无需资源锁。
        """
        ...

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult: ...


class BaseTool:
    """便利基类：默认 READ_ONLY 并发、READ_ONLY 副作用、SAFE 可重跑、无资源锁。"""

    concurrency_mode: ToolConcurrencyMode = ToolConcurrencyMode.READ_ONLY
    effect_kind: EffectKind = EffectKind.READ_ONLY
    retry_policy: RetryPolicy = RetryPolicy.SAFE

    def resource_keys(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        return ()
