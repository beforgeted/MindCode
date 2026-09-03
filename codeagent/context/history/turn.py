"""ConversationTurn 与 TurnPartitioner。

P2 会把这里换成真正的 Turn 模型（带 TurnStatus 状态机），现在先把**接口**
钉住，因为 Compactor、并行 ToolCall、Cancellation、SubAgent 四件事全部以
Turn 为协议边界。上下文文档把 Turn 模型排到最后一期，但它 §35 的 Compactor
伪代码第一行就是 `turnPartitioner.partition(history)` —— 所以接口必须现在有。

当前实现是启发式的（按 user 消息切分），单 Agent 下够用；进 Multi-Agent
前必须换成显式 turn_id 驱动的实现。Message 上已经带了 turn_id 字段，
换实现时不需要改调用方。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from codeagent.llm.message import Message, Role


class TurnStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(slots=True)
class ConversationTurn:
    turn_id: str
    messages: list[Message] = field(default_factory=list)
    status: TurnStatus = TurnStatus.RUNNING

    @property
    def compactable(self) -> bool:
        """RUNNING 的 turn 永远不压缩。"""
        return self.status is TurnStatus.COMPLETED


class TurnPartitioner(Protocol):
    def partition(self, messages: Sequence[Message]) -> list[ConversationTurn]: ...


class TurnIdPartitioner:
    """按 Message.turn_id 分组；没有 turn_id 的落到隐式 turn。

    system 消息永不进入任何 turn —— 这就是"System Prompt 不压"的落地方式。
    """

    def partition(self, messages: Sequence[Message]) -> list[ConversationTurn]:
        turns: list[ConversationTurn] = []
        index: dict[str, ConversationTurn] = {}
        implicit = 0
        for message in messages:
            if message.role is Role.SYSTEM:
                continue
            turn_id = message.turn_id
            if turn_id is None:
                # 没打 turn_id 的消息：以 user 消息为界自造 turn
                if message.role is Role.USER or not turns:
                    implicit += 1
                    turn_id = f"implicit_{implicit}"
                else:
                    turn_id = turns[-1].turn_id
            turn = index.get(turn_id)
            if turn is None:
                turn = ConversationTurn(turn_id=turn_id)
                index[turn_id] = turn
                turns.append(turn)
            turn.messages.append(message)
        return turns


def system_messages(messages: Sequence[Message]) -> list[Message]:
    return [m for m in messages if m.role is Role.SYSTEM]
