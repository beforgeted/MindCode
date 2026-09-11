"""ConversationHistory：run-scoped 的 LLM 工作历史。

两条硬约束：

1. **run-scoped**。它属于某一个 AgentRun，不是全局单例。Multi-Agent 文档里
   RunContext 直接持 `list[Message]`，那样等到接 ContextManager 时 ReActEngine
   要整个重写。所以从第一行代码起就是这个形状。

2. 它是**运行工作集**，不是持久事实源。可以被 prune / offload / compact /
   换成 Checkpoint。持久事实在 RawEventStore 里，append 时同步派生事件。

派生方向永远是 History -> Events，不是两边分别写。
"""

from __future__ import annotations

from collections.abc import Sequence

from codeagent.context.compact.models import TaskCheckpoint
from codeagent.context.history.turn import TurnStatus
from codeagent.evidence.event_store import NullEventStore, RawEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.ids import new_turn_id
from codeagent.llm.message import Message, Role, ToolResultBlock, ToolUseBlock


class ToolProtocolError(AssertionError):
    pass


class ConversationHistory:
    def __init__(
        self,
        *,
        session_id: str,
        agent_run_id: str,
        event_store: RawEventStore | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent_run_id = agent_run_id
        self._events: RawEventStore = event_store or NullEventStore()
        self._messages: list[Message] = []
        self._current_turn_id: str | None = None
        self._turn_statuses: dict[str, TurnStatus] = {}
        # P2 挂载点：压缩后的任务状态。
        self.checkpoint: TaskCheckpoint | None = None
        self.compaction_count = 0

    # --- turn ---

    def begin_turn(self) -> str:
        if self._current_turn_id is not None:
            raise RuntimeError(f"turn 尚未结束: {self._current_turn_id}")
        self._current_turn_id = new_turn_id()
        self._turn_statuses[self._current_turn_id] = TurnStatus.RUNNING
        self._events.append_nowait(
            AgentEvent(
                type=EventType.TURN_STARTED,
                session_id=self.session_id,
                agent_run_id=self.agent_run_id,
                turn_id=self._current_turn_id,
            )
        )
        return self._current_turn_id

    def end_turn(self, status: str = "completed") -> None:
        if self._current_turn_id is None:
            return
        turn_id = self._current_turn_id
        turn_status = _normalize_turn_status(status)
        self._turn_statuses[turn_id] = turn_status
        self._events.append_nowait(
            AgentEvent(
                type=EventType.TURN_FINISHED,
                session_id=self.session_id,
                agent_run_id=self.agent_run_id,
                turn_id=turn_id,
                payload={"status": str(turn_status)},
            )
        )
        self._current_turn_id = None

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    @property
    def turn_statuses(self) -> dict[str, TurnStatus]:
        """返回状态快照；ConversationHistory 是 turn 状态的唯一真源。"""
        return dict(self._turn_statuses)

    # --- 读写 ---

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def append(self, message: Message) -> Message:
        if message.turn_id is None and message.role is not Role.SYSTEM:
            message.turn_id = self._current_turn_id
        self._messages.append(message)
        event = _event_for(message, self.session_id, self.agent_run_id)
        if event is not None:
            message.event_id = event.event_id
            self._events.append_nowait(event)
        return message

    def extend(self, messages: Sequence[Message]) -> None:
        for message in messages:
            self.append(message)

    def replace_messages(self, messages: Sequence[Message]) -> None:
        """裁剪器/压缩器改写历史的唯一入口。

        只允许**等价改写**（同一条消息的裁剪版本）或压缩替换，
        绝不允许在这里做 `messages[-N:]` 式的头部强删 —— 那会静默丢状态。
        """
        self._messages = list(messages)

    def apply_compaction(
        self,
        messages: Sequence[Message],
        checkpoint: TaskCheckpoint,
    ) -> None:
        """校验后一次提交 messages、checkpoint 与计数。"""
        candidate = tuple(messages)
        validate_tool_protocol(candidate)
        checkpoint_messages = [
            message for message in candidate if message.category.value == "checkpoint"
        ]
        if len(checkpoint_messages) != 1:
            raise ValueError("压缩候选必须包含且只包含一个 checkpoint 投影")
        if checkpoint_messages[0].text != checkpoint.render():
            raise ValueError("checkpoint 投影与结构化状态不一致")
        self._messages = list(candidate)
        self.checkpoint = checkpoint
        self.compaction_count += 1

    def snapshot(self) -> list[Message]:
        return list(self._messages)


def _normalize_turn_status(status: str) -> TurnStatus:
    value = status.lower()
    if value in {"completed", "success"}:
        return TurnStatus.COMPLETED
    if value == "cancelled":
        return TurnStatus.CANCELLED
    if value in {"failed", "max_iterations"}:
        return TurnStatus.FAILED
    return TurnStatus.FAILED


def _event_for(message: Message, session_id: str, agent_run_id: str) -> AgentEvent | None:
    mapping = {
        Role.USER: EventType.USER_MESSAGE,
        Role.ASSISTANT: EventType.ASSISTANT_MESSAGE,
        Role.TOOL: EventType.TOOL_RESULT,
    }
    event_type = mapping.get(message.role)
    if event_type is None:
        return None
    return AgentEvent(
        type=event_type,
        session_id=session_id,
        agent_run_id=agent_run_id,
        turn_id=message.turn_id,
        payload={
            "role": str(message.role),
            "category": str(message.category),
            "text": message.text[:4000],
            "tool_uses": [
                {"id": b.id, "name": b.name, "arguments": b.arguments} for b in message.tool_uses
            ],
            "tool_results": [
                {
                    "tool_use_id": b.tool_use_id,
                    "is_error": b.is_error,
                    "artifact_uri": b.artifact_uri,
                    "truncated": b.truncated,
                }
                for b in message.tool_results
            ],
        },
    )


def validate_tool_protocol(messages: Sequence[Message]) -> None:
    """tool 协议不变式：不存在孤立 tool_use，也不存在孤立 tool_result。

    上下文文档 §37.1 / §37.2 要求的正是这条。任何裁剪或压缩之后都应该成立，
    所以它既是测试断言，也可以在 debug 模式下当运行时断言。
    """
    pending: dict[str, str] = {}
    for message in messages:
        for block in message.blocks:
            if isinstance(block, ToolUseBlock):
                pending[block.id] = block.name
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                if block.tool_use_id not in pending:
                    raise ToolProtocolError(f"孤立 tool_result: {block.tool_use_id}")
                pending.pop(block.tool_use_id)
    if pending:
        raise ToolProtocolError(f"孤立 tool_use: {sorted(pending)}")
