"""ApprovalPolicy：外部副作用是否放行（Phase 7d）。

分工明确：
- **内部 Git 冲突/过期**由系统自愈，绝不问用户（001–008 既定）。
- **外部副作用**（部署、发消息、删库、发布、装包、网络写）性质不同——candidate 回滚不了，
  必须由人决定。默认 fail-safe：非交互环境一律拒绝。

守卫点（run_command 等）先判 `allow_external_effects`（推测执行阶段为 False → 直接禁止），
放行阶段才交 ApprovalPolicy 逐条裁决。
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from codeagent.tool.command_policy import CommandDecision


@runtime_checkable
class ApprovalPolicy(Protocol):
    async def approve(self, decision: CommandDecision, *, command: str) -> bool: ...


class DenyExternalApprovalPolicy:
    """默认：非交互环境拒绝一切需要审批的外部副作用。fail-safe。"""

    async def approve(self, decision: CommandDecision, *, command: str) -> bool:
        return False


class AllowExternalApprovalPolicy:
    """测试/受信自动化：无条件放行。谨慎使用。"""

    async def approve(self, decision: CommandDecision, *, command: str) -> bool:
        return True


class InteractiveApprovalPolicy:
    """交互式：在 REPL 里询问用户（阻塞 input 放进 to_thread，不卡事件循环）。"""

    async def approve(self, decision: CommandDecision, *, command: str) -> bool:
        prompt = (
            f"\n[需要审批] 外部副作用命令（{decision.reason}）:\n  {command}\n"
            f"允许执行? [y/N] "
        )
        answer = await asyncio.to_thread(input, prompt)
        return answer.strip().lower() in ("y", "yes")


__all__ = [
    "AllowExternalApprovalPolicy",
    "ApprovalPolicy",
    "DenyExternalApprovalPolicy",
    "InteractiveApprovalPolicy",
]
