"""AgentRun / RunContext。

每次执行新建 AgentRun，长期共享 AgentDefinition。不做 AgentPool，
不复用运行状态。

关键点：`history` 是 ConversationHistory，不是 `list[Message]`。
Multi-Agent 文档里 RunContext 直接持 `list[Message]`、ReActEngine 直接
`llm_client.chat(messages)`，中间没有任何上下文准备 —— 那样等到接
ContextManager 时 ReActEngine 要整个重写。所以从第一行代码起就是这个形状，
每个 AgentRun 自带 ContextProfile 和（P2 起）自己的 TaskCheckpoint。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.agent.models import AgentDefinition, RunStatus
from codeagent.context.history.conversation_history import ConversationHistory
from codeagent.evidence.event_store import RawEventStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.infra.ids import new_agent_run_id
from codeagent.llm.message import Message
from codeagent.tool.models import ToolRun
from codeagent.workspace.context import WorkspaceContext


@dataclass(slots=True)
class RunContext:
    """单次运行的可变状态。

    ReAct 迭代预算说明：`react_iteration` 是 **per-run 全局**的。
    Multi-Agent 文档 §8/§9 有个 bug —— 外层 reflection 循环会重新调用
    `execute(run)`，但循环条件读的是不重置的 `react_iteration`，
    于是第一轮用完预算后每次 reflection 都立刻返回"超过最大 ReAct 次数"，
    再被 LocalVerifier 验证一个 failed 结果、reflection_count 继续加到上限。
    这里明确选择 per-run 全局语义：预算耗尽即终止，不再重进 ReAct。
    """

    react_iteration: int = 0
    retry_count: int = 0
    tool_runs: list[ToolRun] = field(default_factory=list)
    trace_id: str | None = None
    # 最近一次 ContextManager.prepare() 的结果，供 `/context` 读取，
    # 避免为了展示再跑一次准备流程。
    last_prepared: object | None = None

    def record_tool_runs(self, runs: tuple[ToolRun, ...]) -> None:
        self.tool_runs.extend(runs)


@dataclass(slots=True)
class AgentRun:
    definition: AgentDefinition
    session_id: str
    workspace: WorkspaceContext
    run_id: str = field(default_factory=new_agent_run_id)
    history: ConversationHistory = field(init=False)
    context: RunContext = field(default_factory=RunContext)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    status: RunStatus = RunStatus.CREATED
    reflection_count: int = 0
    _event_store: RawEventStore | None = None

    def __post_init__(self) -> None:
        self.history = ConversationHistory(
            session_id=self.session_id,
            agent_run_id=self.run_id,
            event_store=self._event_store,
        )
        if self.definition.system_prompt:
            self.history.append(Message.system(self.definition.system_prompt))

    @classmethod
    def create(
        cls,
        definition: AgentDefinition,
        *,
        session_id: str,
        workspace: WorkspaceContext,
        event_store: RawEventStore | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AgentRun:
        run = cls(
            definition=definition,
            session_id=session_id,
            workspace=workspace,
            _event_store=event_store,
        )
        if cancellation is not None:
            run.cancellation = cancellation
        return run

    @property
    def profile(self):
        return self.definition.context_profile
